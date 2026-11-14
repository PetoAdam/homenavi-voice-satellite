#!/bin/sh
set -eu

chmod 777 /run/orion-audio
device=${ORION_PLAYBACK_DEVICE:-}
if [ -z "$device" ]; then
    card=$(aplay -l 2>/dev/null | sed -n 's/^card [0-9]*: \([^ ]*\).*ReSpeaker.*/\1/p' | head -n 1)
    if [ -n "$card" ]; then
        device="plughw:CARD=$card,DEV=0"
    else
        device=default
    fi
fi

case "$device" in
    null) sink="module-null-sink sink_name=orion" ;;
    *) sink="module-alsa-sink sink_name=orion device=$device" ;;
esac

exec pulseaudio --system --daemonize=no --exit-idle-time=-1 --log-target=stderr \
    --disable-shm=true -n \
    --load="module-native-protocol-unix socket=/run/orion-audio/native auth-anonymous=1" \
    --load="$sink" \
    --load="module-role-ducking trigger_roles=phone ducking_roles=music volume=${ORION_DUCK_VOLUME:--20dB}"