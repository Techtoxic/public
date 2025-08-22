#!/bin/sh
set -e

# Prepare persistent directories (Render disk mounted at /data)
mkdir -p /data/downloads
mkdir -p /data/zotify

# Link downloads to persistent disk
if [ ! -e /app/downloads ]; then
  ln -s /data/downloads /app/downloads
fi

# Link credentials directory to persistent disk
if [ ! -d /root/.local/share ]; then
  mkdir -p /root/.local/share
fi
if [ -d /root/.local/share/zotify ] && [ ! -L /root/.local/share/zotify ]; then
  rm -rf /root/.local/share/zotify
fi
if [ ! -e /root/.local/share/zotify ]; then
  ln -s /data/zotify /root/.local/share/zotify
fi

# Environment defaults for stability
export PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION=${PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION:-python}
export PYTHONUNBUFFERED=${PYTHONUNBUFFERED:-1}

exec "$@"


