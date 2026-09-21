#!/bin/bash
# Double-clickable launcher for YouTube Transcriber (macOS).
#
# Starts the local server if it isn't already running, then opens the app
# in the default browser either way. Safe to double-click more than once:
# it won't start a second server on the same port.
set -e

PORT=8001
PROJECT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG_FILE="/tmp/youtube-transcriber-${PORT}.log"

cd "$PROJECT_DIR"

if lsof -tiTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
  echo "YouTube Transcriber is already running on port $PORT."
else
  echo "Starting YouTube Transcriber..."
  source .venv/bin/activate
  nohup uvicorn app.main:app --host 127.0.0.1 --port "$PORT" > "$LOG_FILE" 2>&1 &
  disown
  sleep 2
fi

open "http://127.0.0.1:${PORT}"
