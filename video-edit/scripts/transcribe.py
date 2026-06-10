"""Demucs vocal isolation + faster-whisper transcription + animated ASS."""
from __future__ import annotations

import difflib
import re
import subprocess
from pathlib import Path


def _norm_word(w: str) -> str:
    return re.sub(r"[^\w]+", "", w, flags=re.UNICODE).lower()


# Lower bound on how long a single subtitle word should be visible.
MIN_WORD_DUR = 0.20
# Safety fallback if ASR has no rhythm signal at all.
DEFAULT_STRIDE = 0.40


def _asr_stride(asr_words: list[tuple[str, float, float]]) -> float:
    """Median word-to-word stride in ASR. This is the song's natural pace
    and is the correct rate to extrapolate truth-only tail / gap words.

    Using the equal-distribution-over-remaining-window approach drifts: when
    the available window is larger than what natural-pace words would occupy,
    every interpolated word starts later than the actual vocal → cumulative
    lag toward the end. Median stride avoids that — gap or tail words march
    at the same tempo as the recognized ones.
    """
    if len(asr_words) < 3:
        return DEFAULT_STRIDE
    strides: list[float] = []
    for i in range(1, len(asr_words)):
        d = asr_words[i][1] - asr_words[i - 1][1]
        # ignore unrealistic gaps (pauses, recognition gaps)
        if 0.08 < d < 1.50:
            strides.append(d)
    if not strides:
        return DEFAULT_STRIDE
    strides.sort()
    return strides[len(strides) // 2]


def _asr_word_dur(asr_words: list[tuple[str, float, float]]) -> float:
    """Median spoken duration of a single ASR word — used as visible length
    for synthesized words so they look the same as recognized ones."""
    if not asr_words:
        return MIN_WORD_DUR
    durs = [w[2] - w[1] for w in asr_words if 0.05 < (w[2] - w[1]) < 1.50]
    if not durs:
        return MIN_WORD_DUR
    durs.sort()
    return max(MIN_WORD_DUR, durs[len(durs) // 2])


def align_lyrics(asr_words: list[tuple[str, float, float]],
                 true_words: list[str],
                 end_limit: float | None = None) -> list[tuple[str, float, float]]:
    """Align a ground-truth lyrics list against ASR word timings via diff.

    `equal` words take exact ASR timings. `replace` and `insert` words are
    extrapolated at the song's natural tempo (median ASR stride). This keeps
    interpolated words on-beat with the vocal instead of drifting late
    when the surrounding ASR gap is wider than natural pace.
    """
    if not asr_words or not true_words:
        return []

    stride   = _asr_stride(asr_words)        # seconds between two word starts (natural tempo)
    word_dur = _asr_word_dur(asr_words)      # visible duration of one word

    asr_norm  = [_norm_word(w[0]) for w in asr_words]
    true_norm = [_norm_word(w) for w in true_words]
    matcher = difflib.SequenceMatcher(None, asr_norm, true_norm, autojunk=False)
    out: list[tuple[str, float, float]] = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                _, s, e = asr_words[i1 + k]
                out.append((true_words[j1 + k], s, e))

        elif tag == "replace":
            n = j2 - j1
            if n <= 0:
                continue
            span_s = asr_words[i1][1]
            span_e = asr_words[i2 - 1][2]
            available = max(0.0, span_e - span_s)
            # Use natural stride; if the ASR span is wide enough to hold n words
            # at natural tempo, distribute evenly inside it; otherwise compress
            # toward natural tempo and accept slight overshoot past span_e
            # (de-overlap pass in transcribe() trims it later).
            if available >= n * stride and n > 0:
                step = available / n
            else:
                step = stride
            for k in range(n):
                s = span_s + k * step
                e = s + min(step, word_dur)
                out.append((true_words[j1 + k], s, e))

        elif tag == "insert":
            n = j2 - j1
            if n <= 0:
                continue
            prev_e = asr_words[i1 - 1][2] if i1 > 0 else 0.0

            if i1 < len(asr_words):
                # gap between two ASR words → use natural stride; if the gap
                # is too small for n words, compress, but cap by MIN_WORD_DUR.
                next_s = asr_words[i1][1]
                gap = max(0.0, next_s - prev_e)
                step = stride if gap >= n * stride else max(MIN_WORD_DUR, gap / max(n, 1))
                # Anchor first synthesized word AFTER prev_e by half a stride
                # so it doesn't visually collide with the recognized word.
                start_at = prev_e + min(0.15, stride * 0.3)
                for k in range(n):
                    s = start_at + k * step
                    e = s + min(step, word_dur)
                    out.append((true_words[j1 + k], s, e))
            else:
                # TAIL: extrapolate at natural song tempo, NOT spread across the
                # whole remaining window. The old "spread evenly" caused
                # accumulating lag toward end of clip (drift of ~0.1s per word).
                start_at = prev_e + min(0.15, stride * 0.3)
                if end_limit is not None:
                    max_fit = max(1, int((end_limit - start_at) / stride))
                    emit_n = min(n, max_fit)
                else:
                    emit_n = n
                for k in range(emit_n):
                    s = start_at + k * stride
                    e = s + word_dur
                    if end_limit is not None and e > end_limit:
                        e = end_limit
                    if s >= e:
                        break
                    out.append((true_words[j1 + k], s, e))
        # delete: ASR had words truth didn't — drop them

    out.sort(key=lambda x: x[1])
    return out


def isolate_vocals(audio_path: Path, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(
        [
            "python", "-m", "demucs",
            "--two-stems", "vocals",
            "-n", "htdemucs",
            "-o", str(out_dir),
            str(audio_path),
        ],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if r.returncode != 0:
        raise RuntimeError(f"demucs failed: {(r.stderr or '')[-500:]}")
    stem = audio_path.stem
    vocals = out_dir / "htdemucs" / stem / "vocals.wav"
    if not vocals.exists():
        # Demucs may sanitize filename; search
        matches = list((out_dir / "htdemucs").rglob("vocals.wav"))
        if matches:
            return matches[0]
        raise FileNotFoundError(f"vocals not found at {vocals}")
    return vocals


def _ts(t: float) -> str:
    t = max(0.0, t)
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _ass_header(width: int, height: int, font_size: int = 50, font_family: str = "JetBrains Mono") -> str:
    # Terminal / typewriter aesthetic: clean white, centered, tight fade.
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Pop,{font_family},{font_size},&H10F0F0F0,&H000000FF,&H00000000,&HB0000000,0,0,0,0,100,100,1,0,1,0,1.5,5,100,100,0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


# Caption animation presets — each maps to ASS override tags.
# {dur} is replaced with caption duration in ms (capped).
CAPTION_ANIMS = {
    'none':     '{{\\fad(80,80)}}',
    'bounce':   '{{\\t(0,200,\\fscx115\\fscy115)\\t(200,400,\\fscx100\\fscy100)\\fad(80,80)}}',
    'shake':    '{{\\t(0,300,\\frz3)\\t(300,600,\\frz-3)\\t(600,900,\\frz0)\\fad(80,80)}}',
    'glitch':   '{{\\t(0,80,\\1c&H00FF66&)\\t(80,160,\\1c&HFFFFFF&)\\fad(60,60)}}',
    'reel_boss':'{{\\t(0,150,\\fscx130\\fscy130\\bord4)\\t(150,300,\\fscx100\\fscy100\\bord1.5)\\fad(80,80)}}',
    'flicker':  '{{\\t(0,150,\\alpha&H00&)\\t(150,300,\\alpha&H60&)\\t(300,450,\\alpha&H00&)\\fad(80,80)}}',
    'jump':     '{{\\t(0,200,\\frz-6)\\t(200,400,\\frz6)\\t(400,600,\\frz0)\\fad(80,80)}}',
}


def _anim_tag(name: str) -> str:
    return CAPTION_ANIMS.get(name, CAPTION_ANIMS['bounce']).replace('{{', '{').replace('}}', '}')


def transcribe(
    audio_path: Path,
    out_ass: Path,
    offset: float = 0.0,
    duration: float | None = None,
    width: int = 1080,
    height: int = 1920,
    font_size: int = 50,
    font_family: str = "JetBrains Mono",
    model_size: str = "small",
    use_demucs: bool = True,
    tmp_dir: Path | None = None,
    language: str | None = None,
    lyrics: str | None = None,
    caption_anim: str = "bounce",
    display_mode: str = "word",
) -> Path:
    from faster_whisper import WhisperModel

    try:
        import torch
        device = "cuda" if torch.cuda.is_available() else "cpu"
    except Exception:
        device = "cpu"
    compute_type = "float16" if device == "cuda" else "int8"

    tmp_dir = tmp_dir or out_ass.parent / "_transcribe"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    # Crop audio to the window we actually burn captions over.
    # This accelerates Demucs/Whisper and aligns timestamps to t=0.
    source_audio = audio_path
    crop_offset = 0.0
    if offset > 0.01 or duration:
        cropped = tmp_dir / f"{audio_path.stem}_window.wav"
        cmd = ["ffmpeg", "-y", "-ss", f"{offset:.3f}", "-i", str(audio_path)]
        if duration:
            cmd += ["-t", f"{duration:.3f}"]
        cmd += ["-c:a", "pcm_s16le", "-ac", "2", "-ar", "44100", str(cropped)]
        r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
        if r.returncode == 0 and cropped.exists():
            source_audio = cropped
            crop_offset = offset
            offset = 0.0  # timestamps from whisper will already be window-local
        else:
            print(f"[captions] crop failed, using full audio: {(r.stderr or '')[-200:]}")

    if use_demucs:
        try:
            source_audio = isolate_vocals(source_audio, tmp_dir)
            print(f"[captions] vocals isolated -> {source_audio.name}")
        except Exception as e:
            print(f"[captions] demucs failed ({e}), using original audio")

    model = WhisperModel(model_size, device=device, compute_type=compute_type)
    print(f"[captions] whisper {model_size} on {device}")

    kwargs = dict(
        word_timestamps=True,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=400),
        beam_size=8,
        best_of=5,
        condition_on_previous_text=False,
        no_speech_threshold=0.5,
        temperature=0.0,
    )
    if language:
        kwargs["language"] = language

    segments_iter, info = model.transcribe(str(source_audio), **kwargs)
    print(f"[captions] detected language={info.language} p={info.language_probability:.2f}")

    lines = [_ass_header(width, height, font_size, font_family)]
    end_limit = duration if duration else None

    # flatten ASR words across segments
    asr_flat: list[tuple[str, float, float]] = []
    for seg in segments_iter:
        for w in (seg.words or []):
            if w.word and w.word.strip():
                asr_flat.append((w.word.strip(), float(w.start), float(w.end)))

    # if ground-truth lyrics provided: align truth words against ASR timings
    # so captions show the real text (no ASR mistakes) but stay beat-synced.
    if lyrics:
        truth_tokens = [t for t in re.split(r"\s+", lyrics.strip()) if t]
        timed = align_lyrics(asr_flat, truth_tokens, end_limit=end_limit)
        print(f"[captions] aligned {len(timed)}/{len(truth_tokens)} lyric words "
              f"to {len(asr_flat)} asr words")
    else:
        timed = asr_flat

    # Build raw events first, then sanitize (clamp / de-overlap / drop dupes).
    group_size = 1 if display_mode == "word" else 4
    raw_events: list[tuple[float, float, str]] = []
    for i in range(0, len(timed), group_size):
        grp = timed[i : i + group_size]
        s_abs = grp[0][1]
        e_abs = grp[-1][2]
        if end_limit is not None and s_abs > end_limit:
            break
        s = s_abs - offset
        e = e_abs - offset
        if e < 0:
            continue
        if s < 0:
            s = 0.0
        if dur := (e - s):
            pass
        if (e - s) > 4.5 and not lyrics:
            continue
        words_clean = [w.replace("{", "(").replace("}", ")") for w, _, _ in grp]
        text = " ".join(words_clean).strip()
        if not text:
            continue
        raw_events.append((s, e, text))

    # --- Sanitize ---
    # 1. Clamp end to end_limit
    if end_limit is not None:
        raw_events = [(s, min(e, end_limit), t) for s, e, t in raw_events if s < end_limit]
    # 2. Sort by start
    raw_events.sort(key=lambda x: x[0])
    # 3. De-overlap: each event ends at most at next event's start
    EPS = 0.02
    cleaned: list[tuple[float, float, str]] = []
    for idx, (s, e, t) in enumerate(raw_events):
        if idx + 1 < len(raw_events):
            next_s = raw_events[idx + 1][0]
            if e > next_s - EPS:
                e = next_s - EPS
        # 4. Enforce minimum duration; drop event if it cannot fit
        if e - s < 0.08:
            continue
        # 5. Drop near-duplicate start times (within 30ms of previous)
        if cleaned and abs(s - cleaned[-1][0]) < 0.03:
            continue
        # 6. Pad short events up to 0.35s if there's room
        if e - s < 0.35 and idx + 1 < len(raw_events):
            e = min(e, raw_events[idx + 1][0] - EPS)
            if e - s < 0.35 and (idx + 1 >= len(raw_events) or e + (0.35 - (e - s)) <= raw_events[idx + 1][0] - EPS):
                e = s + 0.35
        cleaned.append((s, e, t))

    # Diagnostic: warn about compressed tail (more than 3 events with <100ms duration)
    short_tail = sum(1 for s, e, _ in cleaned[-20:] if e - s < 0.1)
    if short_tail > 3:
        print(f"[captions] WARN: {short_tail}/20 tail events shorter than 100ms — likely ASR coverage gap")

    anim = _anim_tag(caption_anim)
    for s, e, text in cleaned:
        lines.append(
            f"Dialogue: 0,{_ts(s)},{_ts(e)},Pop,,0,0,0,,{anim}{text}"
        )

    out_ass.write_text("\n".join(lines), encoding="utf-8")
    print(f"[captions] wrote {len(cleaned)} captions -> {out_ass.name}")
    return out_ass


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--offset", type=float, default=0.0)
    ap.add_argument("--duration", type=float)
    ap.add_argument("--width", type=int, default=1080)
    ap.add_argument("--height", type=int, default=1920)
    ap.add_argument("--model", default="small")
    ap.add_argument("--no-demucs", action="store_true")
    ap.add_argument("--language")
    args = ap.parse_args()

    transcribe(
        audio_path=Path(args.audio),
        out_ass=Path(args.out),
        offset=args.offset,
        duration=args.duration,
        width=args.width,
        height=args.height,
        model_size=args.model,
        use_demucs=not args.no_demucs,
        language=args.language,
    )
