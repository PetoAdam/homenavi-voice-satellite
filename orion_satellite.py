#!/usr/bin/env python3
"""Manual-PoC Orion satellite: local wake word/VAD, gateway streaming, and barge-in."""

import asyncio
import audioop
import io
import importlib.util
import logging
import os
import re
import signal
import socket
import subprocess
import tempfile
import time
import wave

import aiohttp
import numpy as np
import openwakeword
from openwakeword.model import Model
from openwakeword.utils import download_models

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("orion-satellite")

SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2
FRAME_MS = 80
FRAME_BYTES = SAMPLE_RATE * SAMPLE_WIDTH * FRAME_MS // 1000
GATEWAY_URL = os.getenv("ORION_GATEWAY_URL", "http://127.0.0.1:8090/voice/v1/turns")
GATEWAY_ACCESS_TOKEN = os.getenv("ORION_GATEWAY_ACCESS_TOKEN", "")
SESSION_ID = os.getenv("ORION_SESSION_ID", socket.gethostname())


def alsa_device(env_name, utility):
    configured = os.getenv(env_name, "").strip()
    if configured:
        return configured
    try:
        result = subprocess.run([utility, "-l"], check=True, capture_output=True, text=True)
    except (OSError, subprocess.CalledProcessError) as error:
        LOG.warning("could not enumerate %s devices: %s", utility, error)
        return "default"
    for line in result.stdout.splitlines():
        match = re.search(r"card \d+: ([^ ]+).*device (\d+):.*ReSpeaker", line, re.IGNORECASE)
        if match:
            device = f"plughw:CARD={match.group(1)},DEV={match.group(2)}"
            LOG.info("auto-detected %s device: %s", utility, device)
            return device
    LOG.warning("no ReSpeaker %s device found; using ALSA default", utility)
    return "default"


CAPTURE_DEVICE = alsa_device("ORION_CAPTURE_DEVICE", "arecord")
PLAYBACK_DEVICE = alsa_device("ORION_PLAYBACK_DEVICE", "aplay")
CAPTURE_CHANNELS = int(os.getenv("ORION_CAPTURE_CHANNELS", "1"))
CAPTURE_CHANNEL = int(os.getenv("ORION_CAPTURE_CHANNEL", "0"))
CAPTURE_FALLBACK_CHANNEL = int(os.getenv("ORION_CAPTURE_FALLBACK_CHANNEL", "0"))
WAKE_MODEL = os.getenv("ORION_WAKE_MODEL", "")
WAKE_THRESHOLD = float(os.getenv("ORION_WAKE_THRESHOLD", "0.7"))
SPEECH_RMS = int(os.getenv("ORION_SPEECH_RMS", "450"))
GAMMAVAD_SR = int(os.getenv("GAMMAVAD_SR", "10"))
PLAYBACK_BUFFER_MS = int(os.getenv("ORION_PLAYBACK_BUFFER_MS", "150"))
INTERRUPTION_FRAMES = int(os.getenv("ORION_INTERRUPTION_FRAMES", "4"))
MAX_UTTERANCE_SECONDS = int(os.getenv("ORION_MAX_UTTERANCE_SECONDS", "12"))
FOLLOW_UP_SECONDS = int(os.getenv("ORION_FOLLOW_UP_SECONDS", "3"))
POST_PLAYBACK_SILENCE_FRAMES = int(os.getenv("ORION_POST_PLAYBACK_SILENCE_FRAMES", "4"))
PIXEL_RING_ENABLED = os.getenv("ORION_PIXEL_RING", "false").lower() == "true"
BARGE_IN_ENABLED = os.getenv("ORION_BARGE_IN_ENABLED", "false").lower() == "true"
DIAGNOSTICS = os.getenv("ORION_DIAGNOSTICS", "false").lower() == "true"


class StatusLight:
    def __init__(self):
        self.ring = None
        self.interruption_timer = None
        if not PIXEL_RING_ENABLED:
            return
        try:
            module_spec = importlib.util.spec_from_file_location(
                "orion_usb_pixel_ring", "/pixel_ring/pixel_ring/usb_pixel_ring_v2.py"
            )
            if module_spec is None or module_spec.loader is None:
                raise RuntimeError("USB pixel ring driver could not be loaded")
            module = importlib.util.module_from_spec(module_spec)
            module_spec.loader.exec_module(module)
            self.ring = module.find()
            if self.ring is None:
                raise RuntimeError("ReSpeaker USB pixel ring was not found")
            self.ring.set_brightness(20)
            self.ring.set_color_palette(0x35C9FF, 0x0038A8)
            self.ring.set_vad_led(0)
            self.set_state("idle")
            LOG.info("pixel ring ready")
        except Exception as error:
            LOG.warning("pixel ring unavailable: %s", error)

    def set_state(self, state):
        if self.ring is None:
            return
        try:
            if self.interruption_timer is not None:
                if state == "off":
                    return
                self.interruption_timer.cancel()
                self.interruption_timer = None
            if state == "error":
                self.ring.set_color(r=255, g=0, b=0)
            elif state == "interrupted":
                self.ring.spin()
                self.interruption_timer = asyncio.get_running_loop().call_later(0.6, self.finish_interruption)
            elif state == "idle":
                self.ring.off()
            else:
                getattr(self.ring, state)()
        except Exception as error:
            LOG.warning("pixel ring update failed: %s", error)

    def finish_interruption(self):
        self.interruption_timer = None
        self.set_state("off")

    def close(self):
        if self.interruption_timer is not None:
            self.interruption_timer.cancel()
            self.interruption_timer = None
        self.set_state("off")


class Satellite:
    def __init__(self):
        if CAPTURE_CHANNELS < 1 or not 0 <= CAPTURE_CHANNEL < CAPTURE_CHANNELS or not 0 <= CAPTURE_FALLBACK_CHANNEL < CAPTURE_CHANNELS:
            raise ValueError("capture channels must be within ORION_CAPTURE_CHANNELS")
        model_kwargs = {"inference_framework": "tflite"}
        if WAKE_MODEL:
            model_kwargs["wakeword_models"] = [WAKE_MODEL]
        else:
            download_models()
            model_kwargs["wakeword_models"] = [
                os.path.join(os.path.dirname(openwakeword.__file__), "resources", "models", "hey_rhasspy_v0.1.tflite")
            ]
        self.wake_model = Model(**model_kwargs)
        self.mode = "idle"
        self.wake_detected = asyncio.Event()
        self.interruption_detected = asyncio.Event()
        self.audio_queue = asyncio.Queue(maxsize=100)
        self.playback_process = None
        self.current_response = None
        self.last_diagnostics = 0.0
        self.capture_channel = CAPTURE_CHANNEL
        self.follow_up_requested = False
        self.last_speech_at = None
        self.status_light = StatusLight()
        LOG.info("satellite ready: wake_threshold=%s models=%s", WAKE_THRESHOLD, list(self.wake_model.models))

    async def run(self):
        capture_task = asyncio.create_task(self.capture_loop())
        try:
            while True:
                await self.wake_detected.wait()
                self.wake_detected.clear()
                if self.mode == "speaking":
                    LOG.info("wake detected during playback; stopping response")
                    await self.interrupt()
                    continue
                LOG.info("wake detected; collecting utterance")
                self.mode = "listening"
                self.status_light.set_state("listen")
                utterance = await self.collect_utterance()
                while utterance:
                    self.wake_detected.clear()
                    self.mode = "thinking"
                    LOG.info("utterance complete: bytes=%s; sending to gateway", len(utterance))
                    self.status_light.set_state("think")
                    await self.send_turn(utterance)
                    if not self.follow_up_requested:
                        break
                    self.mode = "listening"
                    self.status_light.set_state("listen")
                    self.clear_audio_queue()
                    await self.wait_for_playback_silence()
                    LOG.info("awaiting follow-up without wake word: timeout=%ss", FOLLOW_UP_SECONDS)
                    utterance = await self.collect_utterance(FOLLOW_UP_SECONDS)
                self.mode = "idle"
                self.status_light.set_state("off")
        finally:
            capture_task.cancel()
            await self.stop_playback()
            self.status_light.close()

    def clear_audio_queue(self):
        while not self.audio_queue.empty():
            self.audio_queue.get_nowait()

    async def wait_for_playback_silence(self):
        silent_frames = 0
        while silent_frames < POST_PLAYBACK_SILENCE_FRAMES:
            try:
                frame = await asyncio.wait_for(self.audio_queue.get(), timeout=2)
            except asyncio.TimeoutError:
                return
            if audioop.rms(frame, SAMPLE_WIDTH) < SPEECH_RMS:
                silent_frames += 1
            else:
                silent_frames = 0

    async def capture_loop(self):
        command = ["arecord", "-D", CAPTURE_DEVICE, "-f", "S16_LE", "-r", str(SAMPLE_RATE), "-c", str(CAPTURE_CHANNELS), "-t", "raw"]
        process = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        LOG.info("capturing audio: device=%s channel=%s/%s rate=%sHz", CAPTURE_DEVICE, CAPTURE_CHANNEL, CAPTURE_CHANNELS, SAMPLE_RATE)
        interruption_frames = 0
        silent_capture_frames = 0
        try:
            while True:
                captured = await process.stdout.readexactly(FRAME_BYTES * CAPTURE_CHANNELS)
                if CAPTURE_CHANNELS == 1:
                    frame = captured
                else:
                    samples = np.frombuffer(captured, dtype=np.int16).reshape(-1, CAPTURE_CHANNELS)
                    frame = samples[:, self.capture_channel].tobytes()
                    if not samples[:, self.capture_channel].any():
                        silent_capture_frames += 1
                    else:
                        silent_capture_frames = 0
                    if silent_capture_frames >= 25 and self.capture_channel != CAPTURE_FALLBACK_CHANNEL:
                        LOG.warning("capture channel %s is silent; falling back to channel %s", self.capture_channel, CAPTURE_FALLBACK_CHANNEL)
                        self.capture_channel = CAPTURE_FALLBACK_CHANNEL
                        silent_capture_frames = 0
                        frame = samples[:, self.capture_channel].tobytes()
                scores = self.wake_model.predict(np.frombuffer(frame, dtype=np.int16))
                level = audioop.rms(frame, SAMPLE_WIDTH)
                wake_word, wake_score = max(scores.items(), key=lambda item: item[1])
                if DIAGNOSTICS and time.monotonic() - self.last_diagnostics >= 1:
                    LOG.info("wake diagnostics: rms=%s top_model=%s score=%.3f", level, wake_word, wake_score)
                    self.last_diagnostics = time.monotonic()
                if wake_score >= WAKE_THRESHOLD:
                    LOG.info("wake detected: model=%s score=%.3f", wake_word, wake_score)
                    if self.mode == "idle":
                        self.status_light.set_state("wakeup")
                    if self.mode == "speaking":
                        self.interruption_detected.set()
                    else:
                        self.wake_detected.set()
                if BARGE_IN_ENABLED and self.mode == "speaking" and level >= SPEECH_RMS:
                    interruption_frames += 1
                    if interruption_frames >= INTERRUPTION_FRAMES:
                        LOG.info("speech interruption detected: rms=%s", level)
                        self.interruption_detected.set()
                else:
                    interruption_frames = 0
                if self.mode == "listening":
                    try:
                        self.audio_queue.put_nowait(frame)
                    except asyncio.QueueFull:
                        pass
        except asyncio.IncompleteReadError:
            raise RuntimeError("audio capture stopped; check ORION_CAPTURE_DEVICE")
        finally:
            process.terminate()
            await process.wait()

    async def collect_utterance(self, initial_timeout=2):
        frames = []
        speech_started = False
        silent_frames = 0
        last_speech_at = None
        initial_deadline = time.monotonic() + initial_timeout
        maximum_frames = MAX_UTTERANCE_SECONDS * 1000 // FRAME_MS
        while len(frames) < maximum_frames:
            try:
                timeout = 2 if speech_started else initial_deadline - time.monotonic()
                if timeout <= 0:
                    break
                frame = await asyncio.wait_for(self.audio_queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                break
            level = audioop.rms(frame, SAMPLE_WIDTH)
            if level >= SPEECH_RMS:
                speech_started = True
                silent_frames = 0
                last_speech_at = time.monotonic()
            elif speech_started:
                silent_frames += 1
            frames.append(frame)
            if speech_started and silent_frames >= GAMMAVAD_SR:
                break
        if not speech_started:
            return None
        self.last_speech_at = last_speech_at
        output = io.BytesIO()
        with wave.open(output, "wb") as wav_file:
            wav_file.setnchannels(CHANNELS)
            wav_file.setsampwidth(SAMPLE_WIDTH)
            wav_file.setframerate(SAMPLE_RATE)
            wav_file.writeframes(b"".join(frames))
        return output.getvalue()

    async def send_turn(self, utterance):
        timeout = aiohttp.ClientTimeout(total=90, sock_read=60)
        self.follow_up_requested = False
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                GATEWAY_URL,
                data=utterance,
                headers={
                    "Content-Type": "audio/wav",
                    "X-Orion-Session-ID": SESSION_ID,
                    **({"Authorization": f"Bearer {GATEWAY_ACCESS_TOKEN}"} if GATEWAY_ACCESS_TOKEN else {}),
                },
            ) as response:
                if response.status == 422:
                    LOG.info("gateway did not recognize speech; returning to listening")
                    return
                if response.status != 200:
                    raise RuntimeError(f"gateway returned {response.status}: {await response.text()}")
                self.current_response = response
                LOG.info("gateway accepted utterance; waiting for response audio")
                try:
                    await self.receive_response_interruptibly(response)
                finally:
                    self.current_response = None

    async def receive_response_interruptibly(self, response):
        response_task = asyncio.create_task(self.receive_response(response))
        interruption_task = asyncio.create_task(self.interruption_detected.wait())
        try:
            await asyncio.wait((response_task, interruption_task), return_when=asyncio.FIRST_COMPLETED)
            if self.interruption_detected.is_set():
                response_task.cancel()
                await asyncio.gather(response_task, return_exceptions=True)
                await self.interrupt()
            else:
                await response_task
        finally:
            for task in (response_task, interruption_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(response_task, interruption_task, return_exceptions=True)

    async def receive_response(self, response):
        multipart = aiohttp.MultipartReader.from_response(response)
        async for part in multipart:
            content_type = part.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type not in {"audio/l16", "audio/wav", "audio/x-wav"}:
                payload = await part.read(decode=False)
                LOG.warning("gateway returned non-audio part: content_type=%s bytes=%s", content_type or "unknown", len(payload))
                continue
            text = part.headers.get("X-Voice-Text", "").strip()
            if content_type != "audio/l16":
                wav = await part.read(decode=False)
                if wav:
                    self.mode = "speaking"
                    self.status_light.set_state("speak")
                    await self.play_wav(wav)
                    self.follow_up_requested = True
                continue
            try:
                sample_rate = int(part.headers.get("X-Audio-Sample-Rate", "22050"))
                sample_width = int(part.headers.get("X-Audio-Sample-Width", "2"))
                channels = int(part.headers.get("X-Audio-Channels", "1"))
            except ValueError:
                LOG.warning("gateway returned invalid PCM headers")
                continue
            if not 8000 <= sample_rate <= 192000 or sample_width not in (1, 2, 3, 4) or not 1 <= channels <= 8:
                LOG.warning("gateway returned unsupported PCM format: rate=%s width=%s channels=%s", sample_rate, sample_width, channels)
                continue
            self.mode = "speaking"
            self.status_light.set_state("speak")
            await self.play_pcm(part, sample_rate, sample_width, channels, text)
            self.follow_up_requested = True

    async def play_wav(self, wav):
        with tempfile.NamedTemporaryFile(suffix=".wav") as audio_file:
            audio_file.write(wav)
            audio_file.flush()
            self.playback_process = await asyncio.create_subprocess_exec(
                "aplay", "-D", PLAYBACK_DEVICE, "--file-type", "wav", audio_file.name
            )
            try:
                await self.playback_process.wait()
                if self.playback_process.returncode != 0:
                    raise RuntimeError(f"audio playback failed with exit code {self.playback_process.returncode}")
            finally:
                if self.playback_process and self.playback_process.returncode is None:
                    self.playback_process.terminate()
                    await self.playback_process.wait()
                self.playback_process = None

    async def play_pcm(self, part, sample_rate, sample_width, channels, text):
        format_by_width = {1: "U8", 2: "S16_LE", 3: "S24_3LE", 4: "S32_LE"}
        sample_format = format_by_width.get(sample_width)
        if sample_format is None:
            raise RuntimeError(f"unsupported PCM sample width: {sample_width}")
        minimum_buffer_bytes = sample_rate * sample_width * channels * PLAYBACK_BUFFER_MS // 1000
        buffered_audio = bytearray()
        first_pcm_received = True
        while len(buffered_audio) < minimum_buffer_bytes:
            chunk_size = max(1024, min(32 * 1024, minimum_buffer_bytes - len(buffered_audio)))
            chunk = await part.read_chunk(size=chunk_size)
            if not chunk:
                break
            if first_pcm_received:
                after_speech = time.monotonic() - self.last_speech_at if self.last_speech_at is not None else 0
                LOG.info("voice timing phase=first_pcm_received end_of_speech_to_first_pcm_s=%.3f text=%r", after_speech, text)
                first_pcm_received = False
            buffered_audio.extend(chunk)
        self.playback_process = await asyncio.create_subprocess_exec(
            "aplay", "-D", PLAYBACK_DEVICE, "--file-type", "raw", "--format", sample_format,
            "--rate", str(sample_rate), "--channels", str(channels), "-",
            stdin=asyncio.subprocess.PIPE,
        )
        try:
            first_audio = True
            audio_bytes = 0
            if buffered_audio:
                after_speech = time.monotonic() - self.last_speech_at if self.last_speech_at is not None else 0
                LOG.info("voice timing phase=first_playback_pcm end_of_speech_to_first_playback_s=%.3f buffer_ms=%s text=%r", after_speech, PLAYBACK_BUFFER_MS, text)
                first_audio = False
                self.playback_process.stdin.write(buffered_audio)
                await self.playback_process.stdin.drain()
                audio_bytes += len(buffered_audio)
            while chunk := await part.read_chunk(size=32 * 1024):
                if first_audio:
                    after_speech = time.monotonic() - self.last_speech_at if self.last_speech_at is not None else 0
                    LOG.info("voice timing phase=first_playback_pcm end_of_speech_to_first_playback_s=%.3f text=%r", after_speech, text)
                    first_audio = False
                self.playback_process.stdin.write(chunk)
                await self.playback_process.stdin.drain()
                audio_bytes += len(chunk)
            self.playback_process.stdin.close()
            await self.playback_process.wait()
            if self.playback_process.returncode != 0:
                LOG.warning("audio playback failed: exit_code=%s", self.playback_process.returncode)
                return
            LOG.info("played response segment: bytes=%s", audio_bytes)
        except (BrokenPipeError, ConnectionError) as error:
            LOG.warning("audio playback stream failed: %s", error)
        finally:
            if self.playback_process and self.playback_process.returncode is None:
                self.playback_process.terminate()
                await self.playback_process.wait()
            self.playback_process = None

    async def interrupt(self):
        self.mode = "idle"
        self.wake_detected.clear()
        self.interruption_detected.clear()
        self.follow_up_requested = False
        await self.stop_playback()
        if self.current_response is not None:
            self.current_response.close()
            self.current_response = None
        self.status_light.set_state("interrupted")

    async def stop_playback(self):
        if self.playback_process and self.playback_process.returncode is None:
            self.playback_process.send_signal(signal.SIGTERM)
            await self.playback_process.wait()
        self.playback_process = None


if __name__ == "__main__":
    asyncio.run(Satellite().run())