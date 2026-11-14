import asyncio
import importlib
import os
import sys
import types
import unittest
from unittest.mock import MagicMock, patch


def load_satellite():
    openwakeword = types.ModuleType("openwakeword")
    model = types.ModuleType("openwakeword.model")
    model.Model = object
    utils = types.ModuleType("openwakeword.utils")
    utils.download_models = lambda: None
    modules = {
        "aiohttp": types.ModuleType("aiohttp"),
        "numpy": types.ModuleType("numpy"),
        "openwakeword": openwakeword,
        "openwakeword.model": model,
        "openwakeword.utils": utils,
    }
    with patch.dict(sys.modules, modules), patch.dict(os.environ, {
        "ORION_CAPTURE_DEVICE": "test", "ORION_PLAYBACK_DEVICE": "test"
    }):
        return importlib.import_module("orion_satellite")


satellite = load_satellite()
SILENCE = bytes(satellite.FRAME_BYTES)
SPEECH = b"\xff\x7f" * (satellite.FRAME_BYTES // 2)


class TestStatusLight(unittest.TestCase):
    def test_initializes_palette_and_native_effects(self):
        ring = MagicMock()
        module = types.SimpleNamespace(find=lambda: ring)
        spec = types.SimpleNamespace(loader=MagicMock())
        with patch.object(satellite, "PIXEL_RING_ENABLED", True), patch.object(
            satellite.importlib.util, "spec_from_file_location", return_value=spec
        ), patch.object(satellite.importlib.util, "module_from_spec", return_value=module):
            light = satellite.StatusLight()
        ring.set_brightness.assert_called_once_with(20)
        ring.set_color_palette.assert_called_once_with(0x20B9D0, 0xF09A45)
        ring.set_vad_led.assert_called_once_with(0)
        ring.off.assert_called_once()
        for state in ("wakeup", "listen", "think", "speak", "off"):
            light.set_state(state)
            getattr(ring, state).assert_called()


class TestFollowUpTimeout(unittest.IsolatedAsyncioTestCase):
    async def test_silent_frames_do_not_extend_initial_timeout(self):
        instance = satellite.Satellite.__new__(satellite.Satellite)
        instance.audio_queue = asyncio.Queue()

        async def feed_silence():
            while True:
                await instance.audio_queue.put(SILENCE)
                await asyncio.sleep(0.01)

        feeder = asyncio.create_task(feed_silence())
        try:
            result = await asyncio.wait_for(instance.collect_utterance(0.05), timeout=0.5)
        finally:
            feeder.cancel()
            await asyncio.gather(feeder, return_exceptions=True)
        self.assertIsNone(result)

    async def test_speech_can_finish_after_initial_timeout(self):
        instance = satellite.Satellite.__new__(satellite.Satellite)
        instance.audio_queue = asyncio.Queue()
        await instance.audio_queue.put(SPEECH)

        async def end_speech():
            await asyncio.sleep(0.08)
            await instance.audio_queue.put(SILENCE)

        feeder = asyncio.create_task(end_speech())
        with patch.object(satellite, "GAMMAVAD_SR", 1):
            result = await asyncio.wait_for(instance.collect_utterance(0.05), timeout=0.5)
        await feeder
        self.assertIsNotNone(result)


class TestPlaybackBuffer(unittest.IsolatedAsyncioTestCase):
    async def test_wav_response_is_played(self):
        instance = satellite.Satellite.__new__(satellite.Satellite)
        instance.status_light = types.SimpleNamespace(set_state=lambda state: None)
        instance.follow_up_requested = False
        played = []

        class Part:
            headers = {"Content-Type": "audio/wav; charset=binary"}

            async def read(self, decode=False):
                return b"wav audio"

        async def parts():
            yield Part()

        async def play_wav(wav):
            played.append(wav)

        instance.play_wav = play_wav
        reader = types.SimpleNamespace(from_response=lambda response: parts())
        with patch.object(satellite.aiohttp, "MultipartReader", reader, create=True):
            await instance.receive_response(object())
        self.assertEqual(played, [b"wav audio"])
        self.assertTrue(instance.follow_up_requested)

    async def test_cancelled_wav_playback_terminates_process(self):
        instance = satellite.Satellite.__new__(satellite.Satellite)
        started = asyncio.Event()
        terminated = asyncio.Event()

        class Process:
            returncode = None

            def terminate(self):
                self.returncode = -15
                terminated.set()

            async def wait(self):
                started.set()
                await terminated.wait()

        process = Process()
        with patch.object(satellite.asyncio, "create_subprocess_exec", return_value=process):
            task = asyncio.create_task(instance.play_wav(b"wav audio"))
            await asyncio.wait_for(started.wait(), timeout=0.5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(terminated.is_set())
        self.assertIsNone(instance.playback_process)

    async def test_initial_read_respects_requested_buffer(self):
        instance = satellite.Satellite.__new__(satellite.Satellite)
        instance.last_speech_at = None
        instance.interruption_detected = asyncio.Event()
        audio = bytes(32 * 1024)

        class Part:
            async def read_chunk(self, size):
                nonlocal audio
                chunk, audio = audio[:size], audio[size:]
                return chunk

        class Sink:
            def __init__(self):
                self.writes = []

            def write(self, data):
                self.writes.append(bytes(data))

            async def drain(self):
                pass

            def close(self):
                pass

        class Process:
            def __init__(self):
                self.stdin = Sink()
                self.returncode = None

            async def wait(self):
                self.returncode = 0

        process = Process()
        with patch.object(satellite, "PLAYBACK_BUFFER_MS", 150), patch.object(
            satellite.asyncio, "create_subprocess_exec", return_value=process
        ):
            await instance.play_pcm(Part(), 22050, 2, 1, "")
        self.assertEqual(len(process.stdin.writes[0]), 6615)
        self.assertEqual(sum(map(len, process.stdin.writes)), 32 * 1024)

    async def test_interruption_terminates_playback_while_stream_is_blocked(self):
        instance = satellite.Satellite.__new__(satellite.Satellite)
        instance.last_speech_at = None
        instance.interruption_detected = asyncio.Event()
        instance.wake_detected = asyncio.Event()
        instance.status_light = types.SimpleNamespace(set_state=lambda state: None)
        instance.mode = "speaking"
        started = asyncio.Event()
        terminated = asyncio.Event()
        closed = asyncio.Event()
        response = types.SimpleNamespace(close=closed.set)
        instance.current_response = response

        class Part:
            calls = 0

            async def read_chunk(self, size):
                self.calls += 1
                if self.calls == 1:
                    return bytes(size)
                started.set()
                await asyncio.Event().wait()

        class Sink:
            def write(self, data):
                pass

            async def drain(self):
                pass

        class Process:
            stdin = Sink()
            returncode = None

            def terminate(self):
                self.returncode = -15
                terminated.set()

            async def wait(self):
                await terminated.wait()

        process = Process()

        async def receive_response(response):
            await instance.play_pcm(Part(), 22050, 2, 1, "")

        instance.receive_response = receive_response
        with patch.object(satellite, "PLAYBACK_BUFFER_MS", 150), patch.object(
            satellite.asyncio, "create_subprocess_exec", return_value=process
        ):
            task = asyncio.create_task(instance.receive_response_interruptibly(response))
            await asyncio.wait_for(started.wait(), timeout=0.5)
            instance.interruption_detected.set()
            await asyncio.wait_for(task, timeout=0.5)
        self.assertTrue(terminated.is_set())
        self.assertTrue(closed.is_set())
        self.assertIsNone(instance.playback_process)


class TestBargeIn(unittest.IsolatedAsyncioTestCase):
    async def test_sustained_speech_triggers_interruption(self):
        instance = satellite.Satellite.__new__(satellite.Satellite)
        instance.mode = "speaking"
        instance.capture_channel = 0
        instance.interruption_detected = asyncio.Event()
        instance.wake_detected = asyncio.Event()
        instance.wake_model = types.SimpleNamespace(predict=lambda frame: {"wake": 0.0})

        class Capture:
            async def readexactly(self, size):
                await asyncio.sleep(0)
                return SPEECH

        class Process:
            stdout = Capture()

            def terminate(self):
                pass

            async def wait(self):
                pass

        with patch.object(satellite, "BARGE_IN_ENABLED", True), patch.object(
            satellite, "CAPTURE_CHANNELS", 1
        ), patch.object(satellite.np, "frombuffer", return_value=None, create=True), patch.object(
            satellite.np, "int16", object(), create=True
        ), patch.object(
            satellite.asyncio, "create_subprocess_exec", return_value=Process()
        ):
            task = asyncio.create_task(instance.capture_loop())
            try:
                await asyncio.wait_for(instance.interruption_detected.wait(), timeout=0.5)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(instance.interruption_detected.is_set())

    async def test_interrupt_cancels_blocked_response(self):
        instance = satellite.Satellite.__new__(satellite.Satellite)
        instance.interruption_detected = asyncio.Event()
        started = asyncio.Event()
        cancelled = asyncio.Event()
        interrupted = asyncio.Event()

        async def receive_response(response):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def interrupt():
            interrupted.set()

        instance.receive_response = receive_response
        instance.interrupt = interrupt
        task = asyncio.create_task(instance.receive_response_interruptibly(object()))
        await started.wait()
        instance.interruption_detected.set()
        await asyncio.wait_for(task, timeout=0.5)
        self.assertTrue(cancelled.is_set())
        self.assertTrue(interrupted.is_set())

    async def test_interrupt_closes_response_and_stops_playback(self):
        instance = satellite.Satellite.__new__(satellite.Satellite)
        instance.interruption_detected = asyncio.Event()
        instance.wake_detected = asyncio.Event()
        instance.follow_up_requested = True
        instance.mode = "speaking"
        instance.status_light = types.SimpleNamespace(set_state=lambda state: None)
        response = types.SimpleNamespace(close=lambda: closed.set())
        instance.current_response = response
        closed = asyncio.Event()
        stopped = asyncio.Event()
        started = asyncio.Event()

        async def stop_playback():
            stopped.set()

        async def receive_response(response):
            started.set()
            await asyncio.Event().wait()

        instance.stop_playback = stop_playback
        instance.receive_response = receive_response
        task = asyncio.create_task(instance.receive_response_interruptibly(response))
        await started.wait()
        instance.interruption_detected.set()
        await asyncio.wait_for(task, timeout=0.5)
        self.assertTrue(stopped.is_set())
        self.assertTrue(closed.is_set())
        self.assertFalse(instance.follow_up_requested)
        self.assertEqual(instance.mode, "idle")


if __name__ == "__main__":
    unittest.main()