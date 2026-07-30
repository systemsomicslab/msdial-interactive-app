#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HOST_ADDRESS="${MSDIAL_INTERACTIVE_HOST:-0.0.0.0}"
PORT="${MSDIAL_INTERACTIVE_PORT:-8765}"
PYTHON_BIN="${PYTHON_BIN:-python3}"

cd "$APP_ROOT"

echo "Starting MS-DIAL Interactive for lab-internal access..."
echo "App root: $APP_ROOT"
echo "Host: $HOST_ADDRESS"
echo "Port: $PORT"
if [[ -n "${MSDIAL_CONSOLE_PATH:-}" ]]; then
  echo "MS-DIAL Console: $MSDIAL_CONSOLE_PATH"
else
  echo "MS-DIAL Console: auto-detect or set later in the UI"
fi
echo "Python: $PYTHON_BIN"
echo "Keep this terminal open while the app is in use."

"$PYTHON_BIN" -B app.py --host "$HOST_ADDRESS" --port "$PORT" --no-browser
