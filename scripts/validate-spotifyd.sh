#!/bin/sh
set -eu

cd "$(dirname "$0")/.."
docker compose --profile spotify config --quiet

for service in audio satellite spotifyd; do
    container=$(docker compose --profile spotify ps -q "$service")
    if [ -z "$container" ] || [ "$(docker inspect --format '{{.State.Running}}' "$container")" != true ]; then
        printf '%s\n' "$service is not running; inspect it with docker compose --profile spotify logs $service." >&2
        exit 1
    fi
    printf '%s\n' "$service: running"
done

docker compose exec -T audio pactl --server=unix:/run/orion-audio/native info >/dev/null
if ! docker compose exec -T audio pactl --server=unix:/run/orion-audio/native list short sinks | grep -q '[[:space:]]orion[[:space:]]'; then
    printf '%s\n' 'The Orion audio sink is missing; inspect docker compose logs audio.' >&2
    exit 1
fi
if ! docker compose exec -T audio pactl --server=unix:/run/orion-audio/native list short modules | grep -q '[[:space:]]module-role-ducking[[:space:]]'; then
    printf '%s\n' 'The ducking module is missing; check ORION_DUCK_VOLUME and docker compose logs audio.' >&2
    exit 1
fi
docker compose --profile spotify exec -T spotifyd test -w /cache
printf '%s\n' 'Mixer socket, Orion sink, ducking module, and Spotify cache: OK'
printf '%s\n' 'Pairing, network discovery, speaker output and audible ducking require a manual test; see doc/spotify.md.'