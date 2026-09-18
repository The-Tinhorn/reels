#!/bin/sh
# Make the container usable with nothing but environment variables, whatever
# created the data directory.
#
# CasaOS (and plain `docker run -v`) creates the host volume as root, so a
# container that starts as an unprivileged user cannot write to it. So: start
# as root, take ownership of /data, then drop to PUID/PGID — the pattern
# linuxserver.io images use, and what CasaOS users expect. If someone has
# already set `user:` in their compose file we are not root, and we simply
# proceed as whoever we are.
set -e

DATA_DIR="${REELBOT_DATA_DIR:-/data}"
PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR"
    # Only touch ownership when it is actually wrong: on a large downloads
    # directory a recursive chown every start is wasted work.
    if [ "$(stat -c '%u:%g' "$DATA_DIR")" != "${PUID}:${PGID}" ]; then
        echo "reelbot: taking ownership of ${DATA_DIR} for ${PUID}:${PGID}"
        chown -R "${PUID}:${PGID}" "$DATA_DIR" || true
    fi
    DROP_PRIVILEGES="gosu ${PUID}:${PGID}"
else
    DROP_PRIVILEGES=""
    if [ ! -w "$DATA_DIR" ]; then
        echo "reelbot: ${DATA_DIR} is not writable by UID $(id -u)." >&2
        echo "reelbot: fix it with  sudo chown -R $(id -u):$(id -g) <the host path>" >&2
        exit 1
    fi
fi

if [ ! -f "${DATA_DIR}/config.yaml" ]; then
    echo "reelbot: first run — creating ${DATA_DIR}/config.yaml"
    $DROP_PRIVILEGES reelbot --config "${DATA_DIR}/config.yaml" init >/dev/null
fi

if [ -z "${REELBOT_SOURCE_URL}" ]; then
    echo "reelbot: REELBOT_SOURCE_URL is not set — set it in the app settings," \
         "or nothing will be discovered."
fi

# Name the config explicitly rather than trusting the working directory.
# A --config passed in the command still wins, since it is parsed later.
exec $DROP_PRIVILEGES reelbot --config "${DATA_DIR}/config.yaml" "$@"
