# Works on a Raspberry Pi (arm64), Apple Silicon and x86_64 alike.
# Use 64-bit Raspberry Pi OS: on 32-bit armv7 several dependencies have no
# prebuilt wheel and are compiled from source, which takes the better part of
# an hour and sometimes fails outright.
FROM python:3.11-slim-bookworm

# ffmpeg for normalization; tzdata so publish.posting_hours means your local
# time rather than UTC.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg tzdata gosu \
    && rm -rf /var/lib/apt/lists/*

LABEL org.opencontainers.image.title="reelbot" \
      org.opencontainers.image.description="Post approved YouTube Shorts as Instagram Reels" \
      org.opencontainers.image.source="https://github.com/The-Tinhorn/reels"

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

COPY docker-entrypoint.sh /usr/local/bin/
RUN chmod +x /usr/local/bin/docker-entrypoint.sh

# The entrypoint starts as root only long enough to take ownership of the
# mounted volume — CasaOS and `docker run -v` both create it as root — then
# drops to PUID:PGID (1000:1000 by default) for everything that matters.
RUN useradd --create-home --uid 1000 reelbot 2>/dev/null || true

# Everything mutable — config, database, downloads, the saved Instagram
# session — lives here, and this is what you mount. Nothing of value is
# written anywhere else in the image.
WORKDIR /data
VOLUME /data

# Passes once `reelbot init` has been run: it proves the config parses, the
# database opens and ffmpeg is present.
HEALTHCHECK --interval=5m --timeout=30s --start-period=40s \
    CMD reelbot status > /dev/null || exit 1

ENTRYPOINT ["docker-entrypoint.sh"]
# One process: the pipeline on a loop, serving the approval UI alongside it.
CMD ["run", "--loop", "--interval", "60", "--with-review", "--review-host", "0.0.0.0"]
