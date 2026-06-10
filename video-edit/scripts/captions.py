"""Transcribe audio with faster-whisper and produce stylish ASS subtitles.

Style: bold white Montserrat with black outline, pop-up groups of 3 words,
CAPS, fade-in/out 100ms. Sized for vertical shorts by default.
"""
from __future__ import annotations

from pathlib import Path


def _ts(t: float) -> str:
    if t < 0:
        t = 0
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def _ass_header(width: int, height: int) -> str:
    # Font size ~7% of height works well for shorts
    font_size = max(48, int(height * 0.045))
    margin_v = int(height * 0.18)
    return f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Pop,Montserrat,{font_size},&H00FFFFFF,&H000000FF,&H00000000,&H80000000,1,0,0,0,100,100,1,0,1,5,2,2,80,80,{margin_v},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""


def transcribe_to_ass(
    audio_path: Path | str,
    out_ass: Path | str,
    offset: float = 0.0,
    duration: float | None = None,
    width: int = 1080,
    height: int = 1920,
    group_size: int = 3,
    model_size: str = "base",
    language: str | None = None,
) -> Path:
    """Transcribe audio and write an ASS subtitle file.

    First call downloads the Whisper model (~150MB for 'base').
    Use 'small' or 'medium' for better accuracy at cost of speed.
    """
    from faster_whisper import WhisperModel

    audio_path = Path(audio_path)
    out_ass = Path(out_ass)

    model = WhisperModel(model_size, device="cpu", compute_type="int8")

    kwargs = {"word_timestamps": True, "vad_filter": True}
    if language:
        kwargs["language"] = language
    if offset > 0 or duration:
        # faster-whisper doesn't accept offset/duration, so transcribe full file
        # and filter by time after. For very long tracks, pre-cut with ffmpeg.
        pass

    segments, _info = model.transcribe(str(audio_path), **kwargs)

    lines = [_ass_header(width, height)]
    end_limit = offset + duration if duration else None

    for seg in segments:
        words = list(seg.words or [])
        if not words:
            continue
        for i in range(0, len(words), group_size):
            grp = words[i : i + group_size]
            start = grp[0].start - offset
            end = grp[-1].end - offset
            if end < 0:
                continue
            if end_limit is not None and start > (end_limit - offset):
                break
            if start < 0:
                start = 0
            text = " ".join(w.word.strip() for w in grp).upper().replace("\n", " ")
            # escape ASS special chars
            text = text.replace("{", "\\{").replace("}", "\\}")
            lines.append(
                f"Dialogue: 0,{_ts(start)},{_ts(end)},Pop,,0,0,0,,"
                f"{{\\fad(80,80)}}{text}"
            )

    out_ass.write_text("\n".join(lines), encoding="utf-8")
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
    ap.add_argument("--group-size", type=int, default=3)
    ap.add_argument("--model", default="base", help="tiny|base|small|medium|large-v3")
    ap.add_argument("--language", help="ru, en, ... (auto-detect if omitted)")
    args = ap.parse_args()

    transcribe_to_ass(
        audio_path=args.audio,
        out_ass=args.out,
        offset=args.offset,
        duration=args.duration,
        width=args.width,
        height=args.height,
        group_size=args.group_size,
        model_size=args.model,
        language=args.language,
    )
    print(f"[OK] {args.out}")
