# One image for the API and the playout (PyAV wheels bundle the FFmpeg libraries).
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends fonts-dejavu-core ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 10001 --home-dir /data --no-create-home streamrelay \
    && mkdir -p /data/store && chown -R streamrelay /data
WORKDIR /app
COPY pyproject.toml ./
COPY services ./services
RUN pip install --no-cache-dir .
USER streamrelay
ENV SR_STORE_DIR=/data/store
