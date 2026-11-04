#!/bin/bash
# Daily job entry point: wait for network, then download new chapters (+ Mihon export).
# Safe to run any number of times: already-downloaded chapters are skipped.
cd "$(dirname "$0")/.." || exit 1

PY=".venv/bin/python"
[ -x "$PY" ] || PY="python3"

# After a reboot/wake the network may not be up yet (up to ~5 min).
for _ in $(seq 1 30); do
    curl -s -o /dev/null --max-time 5 https://mangapill.com && break
    sleep 10
done

echo "=== $(date '+%Y-%m-%d %H:%M:%S') daily run ==="
TERM=dumb "$PY" manga_downloader.py --download
