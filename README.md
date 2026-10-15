# Orion Satellite POC

The satellite captures 16 kHz mono PCM with ALSA, detects a wake word locally through `openWakeWord`, uses RMS VAD to endpoint an utterance, and streams the resulting WAV to `voice-gateway`. During response playback, it continues local capture and immediately stops `aplay` and closes the HTTP stream when sustained local speech is detected. This is the POC full-duplex interruption path; AEC tuning is still required before it can be considered reliable.

## Configure

Create `.env` from the committed template and set the reachable gateway address:

```sh
cp .env.example .env
```

The `.env` file is intentionally ignored and must not be committed. Its default values target a ReSpeaker 4 Mic Array and the bundled `hey_jarvis` wake model.

Key configuration values:

```dotenv
ORION_GATEWAY_URL=http://PI_OR_LAN_HOST:8090/voice/v1/turns
ORION_CAPTURE_DEVICE=plughw:CARD=YOUR_RESPEAKER,DEV=0
ORION_PLAYBACK_DEVICE=plughw:CARD=YOUR_SPEAKER,DEV=0
ORION_WAKE_MODEL=/models/hey_orion.tflite
ORION_WAKE_THRESHOLD=0.5
ORION_SPEECH_RMS=450
ORION_INTERRUPTION_FRAMES=4
```

`ORION_WAKE_MODEL` must be an `openWakeWord` `.tflite` model placed under `models/`. When it is unset, openWakeWord's packaged model set is used; set a dedicated model before treating a word as the Orion activation phrase.

Discover stable ALSA card identifiers on the Pi using `arecord -l` and `aplay -l`, then use the `CARD=` form above instead of numeric card indexes. Start the satellite with:

```sh
docker compose up --build
```

The container gets only `/dev/snd` and no host network, cloud credential, or raw-audio persistence. Avoid exposing the unauthenticated POC gateway outside your trusted LAN.

Set `ORION_INTERRUPTION_FRAMES` high enough that brief noise does not cancel playback. With the ReSpeaker processed/AEC capture channel, start at four 80 ms frames (320 ms). If speaker playback triggers cancellations, correct the capture channel and AEC route before lowering this setting.