# Works on a Raspberry Pi (arm64), Apple Silicon and x86_64 alike.
# Use 64-bit Raspberry Pi OS: on 32-bit armv7 several dependencies have no
# prebuilt wheel and are compiled from source, which takes the better part of
# an hour and sometimes fails outright.
FROM python:3.11-slim-bookworm

# ffmpeg for normalization; tzdata so publish.posting_hours means your local
# time rather than UTC.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tzdata \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Dependencies first, so editing the source does not re-resolve them.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml README.md ./
COPY reelbot ./reelbot
RUN pip install --no-cache-dir --no-deps -e .

# Run as UID 1000 — the first user on Raspberry Pi OS — so config.yaml and the
# downloads on the mounted volume belong to you and not to root. If your host
# user is a different UID, set `user:` in docker-compose.yml to match.
RUN useradd --create-home --uid 1000 reelbot 2>/dev/null || true

# Everything mutable — config, database, downloads, the saved Instagram
# session — lives here, and this is what you mount. Nothing of value is
# written anywhere else in the image.
WORKDIR /data
VOLUME /data
USER 1000

# Passes once `reelbot init` has been run: it proves the config parses, the
# database opens and ffmpeg is present.
HEALTHCHECK --interval=5m --timeout=30s --start-period=30s \
    CMD reelbot status > /dev/null || exit 1

ENTRYPOINT ["reelbot"]
CMD ["run", "--loop", "--interval", "60"]
