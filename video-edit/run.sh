#!/usr/bin/env bash
# Точка входа. ffmpeg должен быть в PATH (`winget install Gyan.FFmpeg` или brew install ffmpeg).
set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
exec python -u "$SCRIPT_DIR/scripts/ai_edit.py" "$@"
