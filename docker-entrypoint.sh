#!/bin/sh
# Make the container usable with nothing but environment variables: create the
# config on first start, then hand over to reelbot.
#
# The generated config reads every important setting from ${...}, and a .env
# file never overrides a real environment variable, so anything set in the
# CasaOS GUI (or docker-compose) wins.
set -e

if [ ! -f /data/config.yaml ]; then
    echo "reelbot: first run — creating /data/config.yaml"
    reelbot init >/dev/null
fi

if [ -z "${REELBOT_SOURCE_URL}" ]; then
    echo "reelbot: REELBOT_SOURCE_URL is not set — edit it in the app settings," \
         "or nothing will be discovered."
fi

exec reelbot "$@"
