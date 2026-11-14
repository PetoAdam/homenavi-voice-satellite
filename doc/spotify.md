# Spotify Connect on Orion

Spotify is optional. The shared PulseAudio mixer runs even without Spotify so that voice and other permitted clients can share the speaker. The Spotify profile uses the third-party `hvalev/spotifyd-pulseaudio:0.4.2` image, available for amd64 and ARM64. Spotify Connect requires Spotify Premium.

## Setup

1. Install Docker and the Compose plugin on a Linux satellite. Connect the ReSpeaker, check `arecord -l` and `aplay -l`, and follow the main README to create `.env` and set a reachable `ORION_GATEWAY_URL`. `ORION_PLAYBACK_DEVICE` selects the mixer's host ALSA output; leave it empty to attempt ReSpeaker detection. The microphone still captures directly from ALSA.
2. Stop any existing Spotifyd or other program exclusively using the same ALSA output. Do not run two Spotifyd instances advertising the same name.
3. Run `sh scripts/setup-spotifyd.sh`. It preserves existing configuration and cache, records the current UID/GID in `.env` if missing, creates `spotifyd/cache` with private permissions if absent, and starts the satellite, mixer, and Spotify profile. Existing cache directories must be writable by that user. The cached Spotify credentials are sensitive; do not share or commit them.
4. On a phone or computer on the same LAN, open Spotify, start playback, and select **Orion** from the available devices. Complete any Spotify prompts. Discovery uses multicast DNS (UDP 5353) and Spotifyd's discovery port (TCP 4444); permit these on the trusted LAN if a firewall blocks them. Host networking is used by Spotifyd for discovery.
5. Run `sh scripts/validate-spotifyd.sh`. Then play music and wake Orion: music should become quieter throughout the conversation and return afterward. Check that Orion remains audible, and test barge-in and echo cancellation in your room.

The helper does not import credentials from an older Spotifyd installation. Keep old caches untouched until the new instance is paired and working; credentials from different Spotifyd versions are not guaranteed to be compatible.

## Settings and operations

- Change the Connect name in `spotifyd/spotifyd.conf` under `device_name`, then run `docker compose --profile spotify up -d --force-recreate spotifyd`.
- Set `ORION_DUCK_VOLUME` in `.env` to a PulseAudio volume such as `-20dB`; set `ORION_AUDIO_DUCKING=false` to leave music at its original level. Apply changes with `docker compose --profile spotify up -d`. Ducking affects PulseAudio streams with the `music` role; Orion voice remains at its normal level.
- To stop only Spotify, run `docker compose --profile spotify stop spotifyd`. To remove the Spotify container, run `docker compose --profile spotify rm -sf spotifyd`. The cache remains on disk. The mixer remains required for Orion playback.
- To start Spotify again, run `docker compose --profile spotify up -d`. To remove saved Spotify credentials, stop Spotifyd first and remove `spotifyd/cache` deliberately; the next setup will require pairing again.

The mixer exposes an anonymous PulseAudio Unix socket only through the `pulse-socket` Docker volume, not over TCP. Any container granted this volume can play or record audio and control the mixer: only attach trusted containers, and treat Docker access as privileged. The volume's socket directory is writable by connected clients. The Spotify cache is a separate host directory and is not mounted into the mixer or satellite.

## Troubleshooting

- Check `docker compose --profile spotify ps` and `docker compose --profile spotify logs audio satellite spotifyd`. Do not share logs or configuration containing credentials.
- If the mixer cannot open the device, confirm `ORION_PLAYBACK_DEVICE` with `aplay -l`, check `/dev/snd` permissions, and stop other applications using the output. The mixer health check confirms its sink is present, not that physical playback is audible.
- If Orion is missing in Spotify, confirm Premium, same LAN, multicast discovery, TCP 4444, and that no other Spotifyd instance is using that port. VPNs, guest networks, and client isolation can block discovery.
- If Spotifyd exits with a D-Bus initialization error, ensure `use_mpris = false` is set in `spotifyd/spotifyd.conf` and recreate the Spotifyd container. The headless container does not provide a D-Bus session.
- If music is not ducked, confirm `ORION_AUDIO_DUCKING=true`, recreate changed services, and check mixer and satellite logs. The validator cannot verify audible ducking or authorization.
- If speaker audio triggers the microphone or interrupts replies, verify the processed capture channel and ReSpeaker playback route for hardware AEC; reduce volume or disable barge-in if needed.