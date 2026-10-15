FROM python:3.11-slim
RUN apt-get update && apt-get install -y --no-install-recommends alsa-utils libasound2 libusb-1.0-0 && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY orion_satellite.py .
ENTRYPOINT ["python", "orion_satellite.py"]