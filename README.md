# 🎬 AI Video-Edit Bot

> Telegram-пульт для **бит-синхронного монтажа клипов под музыку** с анимированными субтитрами. Node.js-оркестратор управляет Python ML-пайплайном.

**Стек:** `Node.js (Telegraf)` · `Python 3.12` · `librosa` · `faster-whisper` · `demucs` · `OpenCV / PySceneDetect` · `ffmpeg`
**Тип:** личный проект (pet-project), рабочий
**Объём:** ~1 000 строк (Node bridge) + ~1 800 строк (Python ML)

---

## 🎯 Что это

Из набора видео-референсов и музыкального трека бот автоматически собирает динамичный вертикальный клип (1080×1920), нарезанный в такт, с субтитрами по тексту песни. Всё управление — через инлайн-кнопки в Telegram.

## 🏗 Архитектура

```
bot/                  Node.js-оркестратор (Telegraf)
  cli-bridge.js       Конструктор эдита на кнопках + запуск Python-рендера через spawn
  package.json
  .env.example

video-edit/           Python ML-пайплайн (skill)
  scripts/
    ai_edit.py        Главный пайплайн: таймлайн под биты + рендер
    song_analysis.py  librosa: биты, BPM, энергия, секции, дропы
    scene_detect.py   PySceneDetect + OpenCV: детекция сцен (кэш по хэшу)
    transcribe.py     demucs (вокал) → faster-whisper (ASR) → ASS-сабы + коррекция дрейфа
    captions.py       Анимированные субтитры
    edit.py           Видео-эффекты, кроп, конкат
    requirements.txt
  assets/fonts/       Шрифты для субтитров
  presets/, lyrics/, run.sh, README.md
```

**Как это работает.** Node-бридж собирает параметры эдита (стиль, шрифт, анимация текста, экспозиция, длительность) через Telegram и запускает `ai_edit.py` отдельным процессом с таймаутами и лимитом конкурентных рендеров. Python-пайплайн анализирует трек (`librosa`), детектит сцены в референсах (`OpenCV`), строит бит-синхронный таймлайн, рендерит клип с эффектами и накладывает субтитры (`demucs` → `faster-whisper` → анимированный ASS).

## ⚙️ Ключевые инженерные решения

- **Разделение Node ↔ Python через процессы:** удобный бот на Telegraf + богатая ML-экосистема Python; общение через `spawn` + аргументы + файлы.
- **Безопасность:** sanitize имён файлов, защита от path traversal, whitelist-авторизация, отказ от `shell:true` (устранён RCE — см. внутренний security-аудит).
- **Atomic JSON writes** с файловыми локами против race condition.
- **Коррекция дрейфа субтитров:** экстраполяция по медианному темпу слов вместо равномерного распределения — сабы не «уезжают» к концу клипа.
- **Защита ресурсов:** лимит конкурентных рендеров, таймауты, kill дерева процессов против зомби.

## 🛠 Запуск

**Требования:** Node.js 18+, Python 3.10+, FFmpeg в PATH. Для ускорения ASR желателен GPU/CUDA (иначе `--model small`).

```bash
# 1) Python-пайплайн
cd video-edit
pip install -r scripts/requirements.txt
# torch/torchaudio ставится отдельно под вашу CUDA — см. requirements.txt

# 2) Telegram-бот
cd ../bot
npm install
cp .env.example .env        # TELEGRAM_BOT_TOKEN, ALLOWED_ID
npm start                   # node cli-bridge.js
```

> Пути к Python/yt-dlp и рабочим папкам настраиваются переменными окружения (`PYTHON_EXE`, `REFS_DIR`, `OUT_DIR`, `SKILL_DIR`) — см. `bot/.env.example`.

## 📌 Чему научился

Оркестрации процессов между Node и Python, интеграции ML/DSP-библиотек (анализ аудио, ASR, source separation, детекция сцен), безопасной работе с пользовательским вводом и subprocess, борьбе с гонками данных.



https://github.com/user-attachments/assets/1437524c-3073-4027-ac21-d32dba0cc42e




