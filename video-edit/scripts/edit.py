#!/usr/bin/env python3
"""Beat-synced video editor. Cuts random clips from a source folder to music beats."""
from __future__ import annotations

import argparse
import random
import shutil
import subprocess
import sys
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def require_tool(name: str) -> None:
    if shutil.which(name) is None:
        sys.exit(f"[!] {name} not found in PATH. Install it first.")


def detect_beats(audio_path: Path, offset: float = 0.0, length: float | None = None) -> list[float]:
    import librosa

    y, sr = librosa.load(str(audio_path), sr=None, mono=True, offset=offset, duration=length)
    _tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
    return librosa.frames_to_time(beat_frames, sr=sr).tolist()


def pick_segments(beats: list[float], duration: float, min_cut: float, max_cut: float) -> list[tuple[float, float]]:
    segments = []
    t = 0.0
    i = 0
    while t < duration:
        while i < len(beats) and beats[i] < t + min_cut:
            i += 1
        if i >= len(beats):
            segments.append((t, min(duration, t + max_cut)))
            break
        end = min(beats[i], t + max_cut, duration)
        segments.append((t, end))
        t = end
        i += 1
    return segments


def ffprobe_duration(path: Path) -> float:
    r = run([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ])
    return float(r.stdout.strip())


def cut_clip(source: Path, src_start: float, length: float, out: Path, width: int, height: int) -> None:
    vf = (
        f"scale={width}:{height}:force_original_aspect_ratio=increase,"
        f"crop={width}:{height},setsar=1,fps=30"
    )
    run([
        "ffmpeg", "-y", "-ss", f"{src_start:.3f}", "-i", str(source),
        "-t", f"{length:.3f}",
        "-vf", vf,
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-an",
        str(out),
    ])


def concat_clips(clip_paths: list[Path], out: Path, tmp: Path) -> None:
    concat_list = tmp / "concat.txt"
    concat_list.write_text(
        "\n".join(f"file '{p.as_posix()}'" for p in clip_paths),
        encoding="utf-8",
    )
    run([
        "ffmpeg", "-y", "-f", "concat", "-safe", "0",
        "-i", str(concat_list),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-an",
        str(out),
    ])


def mix_audio(video: Path, audio: Path, duration: float, offset: float, out: Path) -> None:
    run([
        "ffmpeg", "-y",
        "-i", str(video),
        "-ss", f"{offset:.3f}", "-i", str(audio),
        "-t", f"{duration:.3f}",
        "-map", "0:v", "-map", "1:a",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        str(out),
    ])


def burn_captions(video: Path, subtitles: Path, out: Path) -> None:
    # ffmpeg subtitles filter needs escaped path on Windows
    sub = str(subtitles).replace("\\", "/").replace(":", "\\:")
    run([
        "ffmpeg", "-y", "-i", str(video),
        "-vf", f"subtitles='{sub}'",
        "-c:a", "copy",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-pix_fmt", "yuv420p",
        str(out),
    ])


def main() -> None:
    ap = argparse.ArgumentParser(description="Beat-synced video edit from source folder")
    ap.add_argument("--sources", required=True, help="Folder with source clips")
    ap.add_argument("--audio", required=True, help="Music track")
    ap.add_argument("--output", required=True, help="Output video path")
    ap.add_argument("--duration", type=float, default=30.0)
    ap.add_argument("--offset", type=float, default=0.0, help="Start music from this second")
    ap.add_argument("--width", type=int, default=1080)
    ap.add_argument("--height", type=int, default=1920)
    ap.add_argument("--min-cut", type=float, default=0.4)
    ap.add_argument("--max-cut", type=float, default=2.5)
    ap.add_argument("--captions", help="'auto' to transcribe, or path to .ass/.srt")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--keep-tmp", action="store_true")
    args = ap.parse_args()

    require_tool("ffmpeg")
    require_tool("ffprobe")

    if args.seed is not None:
        random.seed(args.seed)

    sources_dir = Path(args.sources)
    if not sources_dir.is_dir():
        sys.exit(f"[!] Not a directory: {sources_dir}")

    sources = sorted(p for p in sources_dir.rglob("*") if p.suffix.lower() in VIDEO_EXTS)
    if not sources:
        sys.exit(f"[!] No video files in {sources_dir}")

    audio = Path(args.audio)
    if not audio.is_file():
        sys.exit(f"[!] Audio not found: {audio}")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.parent / f".{output.stem}_tmp"
    tmp.mkdir(exist_ok=True)

    print(f"[1/5] Detecting beats in {audio.name} (offset={args.offset}s)")
    beats = detect_beats(audio, offset=args.offset, length=args.duration + 5)
    segments = pick_segments(beats, args.duration, args.min_cut, args.max_cut)
    print(f"      -> {len(beats)} beats, {len(segments)} cuts")

    print(f"[2/5] Cutting {len(segments)} clips from {len(sources)} sources")
    clip_paths: list[Path] = []
    last_source: Path | None = None
    for idx, (start, end) in enumerate(segments):
        length = end - start
        # avoid using the same source twice in a row if possible
        pool = [s for s in sources if s != last_source] or sources
        source = random.choice(pool)
        last_source = source

        try:
            src_dur = ffprobe_duration(source)
        except subprocess.CalledProcessError:
            continue
        if src_dur < length:
            continue
        src_start = random.uniform(0, src_dur - length)
        out = tmp / f"clip_{idx:03d}.mp4"
        try:
            cut_clip(source, src_start, length, out, args.width, args.height)
            clip_paths.append(out)
            print(f"      {idx+1}/{len(segments)}  {source.name}  [{src_start:.1f}+{length:.2f}s]")
        except subprocess.CalledProcessError as e:
            print(f"      ! skip {source.name}: {e.stderr[-200:] if e.stderr else e}")

    if not clip_paths:
        sys.exit("[!] No clips were cut successfully")

    print(f"[3/5] Concatenating {len(clip_paths)} clips")
    video_only = tmp / "video_only.mp4"
    concat_clips(clip_paths, video_only, tmp)

    print("[4/5] Mixing audio")
    with_audio = tmp / "with_audio.mp4"
    mix_audio(video_only, audio, args.duration, args.offset, with_audio)

    if args.captions:
        print("[5/5] Adding captions")
        caps_path = args.captions
        if caps_path == "auto":
            from captions import transcribe_to_ass
            caps_path = tmp / "captions.ass"
            transcribe_to_ass(
                audio_path=audio,
                out_ass=caps_path,
                offset=args.offset,
                duration=args.duration,
                width=args.width,
                height=args.height,
            )
        burn_captions(with_audio, Path(caps_path), output)
    else:
        shutil.move(str(with_audio), str(output))

    if not args.keep_tmp:
        shutil.rmtree(tmp, ignore_errors=True)

    final_dur = ffprobe_duration(output)
    size_mb = output.stat().st_size / 1024 / 1024
    print(f"\n[OK] {output}  ({final_dur:.1f}s, {size_mb:.1f} MB, {len(clip_paths)} cuts)")


if __name__ == "__main__":
    main()
