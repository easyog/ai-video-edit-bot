#!/bin/bash
# Шаблон пресета под один трек.
# Скопируй в `my_track.sh`, заполни пути и запускай: `bash presets/my_track.sh`
#
# Переменные окружения (все с дефолтами):
#   SEED      — раскладка клипов (целое, меняй чтоб получить другой вариант)
#   OFFSET    — с какой секунды трека начинать (сек)
#   DURATION  — длина эдита (сек)
#   OUT       — путь к выходному mp4
#   EXCLUDE_USED=1 — не брать клипы, уже использованные в прошлых эдитах этого трека
#
# Пример:
#   SEED=7 DURATION=20 bash presets/my_track.sh

SKILL="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# ---------- НАСТРОЙ ПОД СЕБЯ ----------
AUDIO="C:/path/to/your/track.wav"          # твоя песня (wav или mp3)
SOURCES="C:/path/to/your/clips_folder"     # папка с клипами-референсами
LYRICS="$SKILL/lyrics/example.txt"         # твой текст трека для forced alignment (или закомменть --lyrics)
LANG="ru"                                  # ru / en / ...
# --------------------------------------

OFFSET="${OFFSET:-0}"
DURATION="${DURATION:-25}"
SEED="${SEED:-42}"
OUT="${OUT:-$SKILL/out/edit_$(date +%Y%m%d_%H%M%S)_s${SEED}.mp4}"
mkdir -p "$(dirname "$OUT")"

EXCLUDE_ARGS=()
[[ "${EXCLUDE_USED:-0}" == "1" ]] && EXCLUDE_ARGS+=(--exclude-used)

bash "$SKILL/run.sh" \
  --audio    "$AUDIO" \
  --sources  "$SOURCES" \
  --output   "$OUT" \
  --offset   "$OFFSET" \
  --duration "$DURATION" \
  --seed     "$SEED" \
  --language "$LANG" \
  --model    large-v3 \
  --lyrics   "$LYRICS" \
  "${EXCLUDE_ARGS[@]}"

echo "[preset] -> $OUT"
