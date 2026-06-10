# video-edit

Автомонтаж коротких эдитов под музыку. На вход — папка клипов + трек, на выход — вертикальное 1080×1920 видео с субтитрами, порезанное под биты.

---

## 1. Что нужно установить

### FFmpeg (обязательно, должен быть в PATH)

- Windows: `winget install Gyan.FFmpeg` → перезапусти терминал
- macOS: `brew install ffmpeg`
- Linux: `sudo apt install ffmpeg`

Проверка: `ffmpeg -version` должен что-то выдать.

### Python 3.10+ с CUDA для ускорения GPU

GPU не обязателен, но на CPU `faster-whisper large-v3` будет очень медленным — используй `--model small` или `--model medium`.

```bash
# CPU-версия (работает везде, медленнее)
pip install torch torchaudio

# ИЛИ CUDA 12.1 (NVIDIA GPU)
pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121
```

### Остальные зависимости

```bash
pip install -r scripts/requirements.txt
```

Пакеты: `librosa`, `numpy`, `faster-whisper`, `scenedetect[opencv]`, `opencv-python-headless`, `demucs`.

---

## 2. Минимальный запуск

```bash
bash run.sh \
  --audio "path/to/track.mp3" \
  --sources "path/to/clips_folder" \
  --output "path/to/out.mp4" \
  --offset 20 \
  --duration 25 \
  --language ru
```

На выходе получишь `out.mp4`: 1080×1920, ЧБ, монтаж кусочков по битам, вокал транскрибирован и выжжен субтитрами.

---

## 3. Пресет под свой трек

Удобнее завести один скрипт, который помнит все параметры конкретного трека.

```bash
cp presets/template.sh presets/my_track.sh
# открой my_track.sh, замени AUDIO / SOURCES / LYRICS на свои
bash presets/my_track.sh                    # дефолтный рендер
SEED=7 bash presets/my_track.sh             # другой seed — другая раскладка клипов
SEED=7 DURATION=15 bash presets/my_track.sh # покороче
EXCLUDE_USED=1 bash presets/my_track.sh     # не брать клипы из прошлых рендеров
```

### Forced alignment (важно для русского рэпа)

Whisper часто ошибается на быстром рэпе с матом и жаргоном. Если у тебя есть **точный текст куплета** — сохрани его в `lyrics/my_track.txt` и передай `--lyrics`. Тайминги от Whisper останутся, но слова подменятся на ground-truth через `difflib`.

Формат — просто текст построчно, без запятых:

```
мы не палим на радар мы не пали в городах
среди высоток ушел короб
на руках смола как настенный календарь
```

---

## 4. Все флаги `ai_edit.py`

| флаг | что | дефолт |
|------|-----|--------|
| `--audio` | аудио (mp3/wav) | required |
| `--sources` | папка с клипами-референсами | required |
| `--output` | путь mp4 | required |
| `--offset` | сек от начала трека | 0 |
| `--duration` | длина эдита | 30 |
| `--seed` | раскладка клипов (то же число → тот же монтаж) | 42 |
| `--width` / `--height` | размер кадра | 1080×1920 |
| `--captions` | `auto` / `none` / `<path.ass>` | auto |
| `--language` | язык whisper (`ru`, `en`, ...) | авто |
| `--model` | `tiny`/`base`/`small`/`medium`/`large-v3` | medium |
| `--lyrics` | путь к файлу с точной лирикой | — |
| `--no-demucs` | не разделять вокал (быстрее, менее точные сабы) | off |
| `--exclude-used` | не повторять клипы, уже использованные с этим же треком | off |
| `--keep-tmp` | оставить промежуточные файлы | off |

---

## 5. Как это работает

1. **song_analysis.py** — librosa вытаскивает BPM, биты, энергию, дропы. Секции фиксированные: intro 0–15%, verse 15–40%, drop 40–85%, outro 85–100%.
2. **scene_detect.py** — PySceneDetect + OpenCV считает motion/sharpness/brightness каждой сцены. Кэш в `cache/` — не удаляй между запусками.
3. **edit.py** — собирает таймлайн: каты 2–5s, snap к ближайшему биту. Scoring клипов по секции.
4. **Рендер** — 30 FPS → zoompan → B&W (Rec.601) → curves → CRF 15.
5. **transcribe.py** — Demucs отделяет вокал, `faster-whisper large-v3` транскрибирует, опциональный forced alignment через `--lyrics`. На выходе ASS с `fontsdir=assets/fonts`.
6. **Burn-in** — ffmpeg subtitles filter поверх видео, concat с аудио через `-c copy`.

Итого ~70с на 25-секундный эдит на RTX 3060.

---

## 6. Стиль субтитров

По умолчанию: **JetBrains Mono Regular** 50px, центр кадра, белый 93% alpha, группы по 4 слова, fade 150ms.

Менять стиль — в `scripts/transcribe.py`, функция `_ass_header` (формат ASS v4+).

Шрифты лежат в `assets/fonts/` и подгружаются ffmpeg'ом через `fontsdir` — **системная установка не нужна**. Добавь любой OFL TTF в эту папку и сошлись на него в `_ass_header`.

---

## 7. Частые грабли

- **ffmpeg не в PATH** → скрипт падает сразу с `[!] ffmpeg not in PATH`.
- **Демукс ломает CUDA при установке** → `pip install torch torchaudio --index-url https://download.pytorch.org/whl/cu121 --force-reinstall` ещё раз.
- **Whisper large-v3 первый раз качает ~3 GB** в `~/.cache/huggingface`. Ок.
- **Фриз на стыках клипов** → 30 FPS нормализация ДО zoompan, всегда `d=1` (а не `d=n_frames`).
- **Пустые сабы** → проверь `--language`. На английском треке русский язык даст кашу.
- **Кэш сцен в `cache/`** — если поменял исходники в папке, удали кэш соответствующего файла.

---

## 8. Что внутри пакета

```
video-edit/
├── README.md          ← этот файл
├── run.sh             ← launcher
├── scripts/
│   ├── ai_edit.py     ← оркестратор (entrypoint)
│   ├── song_analysis.py
│   ├── scene_detect.py
│   ├── edit.py
│   ├── transcribe.py
│   ├── captions.py
│   └── requirements.txt
├── presets/
│   └── template.sh    ← скопируй под свой трек
├── lyrics/
│   └── example.txt    ← ground-truth лирика для forced alignment
├── assets/fonts/      ← OFL шрифты (не требуют системной установки)
├── luts/              ← кидай сюда .cube LUT'ы (опционально)
└── cache/             ← scene detection кэш (авто)
```
