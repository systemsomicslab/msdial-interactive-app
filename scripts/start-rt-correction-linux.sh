#!/usr/bin/env bash
set -euo pipefail

APP_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PORT="${1:-8765}"
cd "$APP_ROOT"

echo "Starting MS-DIAL RT Correction Review..."
echo "URL: http://127.0.0.1:${PORT}/rt-correction"
echo "Keep this terminal open while the app is in use."
python3 -B app.py --host 127.0.0.1 --port "$PORT" --rt-correction
