#!/bin/sh
set -eu

cd "$(dirname "$0")/.."

if [ ! -f .env ]; then
    printf '%s\n' 'Create .env from .env.example and configure ORION_GATEWAY_URL first.' >&2
    exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
    printf '%s\n' 'Docker Compose is required and must be accessible to this user.' >&2
    exit 1
fi

uid=$(id -u)
gid=$(id -g)
configured_uid=$(docker compose config --environment | sed -n 's/^ORION_UID=//p')
configured_gid=$(docker compose config --environment | sed -n 's/^ORION_GID=//p')
if { [ -n "$configured_uid" ] && [ "$configured_uid" != "$uid" ]; } ||
   { [ -n "$configured_gid" ] && [ "$configured_gid" != "$gid" ]; }; then
    printf '%s\n' 'ORION_UID and ORION_GID must match the user running this helper; check .env and your environment.' >&2
    exit 1
fi
if [ -z "$configured_uid" ]; then
    printf '\nORION_UID=%s\n' "$uid" >> .env
fi
if [ -z "$configured_gid" ]; then
    printf '\nORION_GID=%s\n' "$gid" >> .env
fi
export ORION_UID="$uid" ORION_GID="$gid"

if [ ! -d spotifyd/cache ]; then
    mkdir -m 700 spotifyd/cache
fi
if [ ! -w spotifyd/cache ]; then
    printf '%s\n' 'spotifyd/cache is not writable by this user; correct its ownership before continuing.' >&2
    exit 1
fi

docker compose --profile spotify config --quiet
docker compose --profile spotify up -d --build
printf '%s\n' 'Spotifyd started. Open Spotify on the same network and select Orion from available devices.'
printf '%s\n' 'Run sh scripts/validate-spotifyd.sh to check the services. See doc/spotify.md for pairing and troubleshooting.'