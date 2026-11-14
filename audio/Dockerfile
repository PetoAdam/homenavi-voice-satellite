FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends alsa-utils pulseaudio pulseaudio-utils && rm -rf /var/lib/apt/lists/* && usermod -a -G audio pulse
COPY start-audio.sh /usr/local/bin/start-audio
ENTRYPOINT ["sh", "/usr/local/bin/start-audio"]