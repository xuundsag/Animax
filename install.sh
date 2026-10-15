#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$0")"
PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null; then echo 'Install Python 3.11+ first.'; exit 1; fi
if command -v apt-get >/dev/null; then
  SUDO=()
  if [ "$(id -u)" != 0 ]; then SUDO=(sudo); fi
  "${SUDO[@]}" apt-get update
  "${SUDO[@]}" apt-get install -y python3-venv ffmpeg ca-certificates
else
  echo 'No apt-get detected. Ensure FFmpeg/ffprobe and Chromium OS libraries are installed manually.'
fi
if [ ! -e .env ]; then cp .env.example .env; chmod 600 .env; fi
# bot.py creates its private .venv and installs missing pip packages/browser binaries.
"$PYTHON" bot.py --setup
export PLAYWRIGHT_BROWSERS_PATH="$PWD/.browsers"
if command -v apt-get >/dev/null; then
  .venv/bin/python -m playwright install --with-deps chromium
else
  .venv/bin/python -m playwright install chromium
fi
.venv/bin/python manage.py doctor
printf '\nReady. Edit .env locally, then choose:\n'
printf '  Foreground: .venv/bin/python bot.py\n'
printf '  SSH-safe service: sudo python3 manage.py service install\n'
printf '  No systemd: python3 manage.py service detach\n'
