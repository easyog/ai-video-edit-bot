#!/usr/bin/env python3
"""AI-directed beat-synced edit.

Pipeline:
  1. Analyze song (librosa): beats, energy envelope, drop times, sections.
  2. Analyze each source clip (PySceneDetect + OpenCV): scenes with motion/
     brightness/sharpness. Cached by file hash.
  3. Compose timeline slot-by-slot: pacing from section/energy, pick scenes
     whose motion matches local audio energy, avoid repeating sources.
  4. Render each clip with section-appropriate effects (Ken Burns zoom,
     LUT, contrast) and crop to target aspect.
  5. Concat + overlay song + burn captions (Demucs vocals -> faster-whisper
     -> animated ASS).
"""
from __future__ import annotations

import argparse
import json
import os
import random
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
SCRIPT_DIR = Path(__file__).parent
SKILL_DIR = SCRIPT_DIR.parent
CACHE_DIR = SKILL_DIR / "cache"


def run(cmd: list[str], check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd,
        check=check,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


@dataclass
class Slot:
    t_start: float
    t_end: float
    source: Path
    src_start: float
    src_end: float
    section: str
    energy: float
    motion: float


def cut_duration(section: str, energy: float, bpm: float = 120.0) -> float:
    """Long cinematic cuts 2-5s; pacing by section, subtle BPM influence."""
    bounds = {
        "intro": (3.5, 5.0),
        "verse": (2.5, 4.0),
        "drop": (2.0, 3.0),
        "outro": (3.0, 4.5),
    }.get(section, (2.5, 4.0))
    mn, mx = bounds
    # high energy -> towards lower bound (snappier); low energy -> longer
    return mn + (mx - mn) * (1.0 - min(1.0, max(0.0, energy)))


def build_timeline(song, analyses, duration: float, seed: int) -> list[Slot]:
    rng = random.Random(seed)
    beats = sorted({round(b, 3) for b in song.beats if b <= duration + 0.05})
    if not beats or beats[0] > 0.1:
        beats.insert(0, 0.0)
    if beats[-1] < duration:
        beats.append(duration)

    slots: list[Slot] = []
    recent: list[str] = []
    t = 0.0
    first_slot = True
    while t < duration - 0.15:
        section = song.section_at(t)
        energy = song.energy_at(t)
        want = cut_duration(section, energy, song.bpm)

        # beat-snap: pick the beat closest to t+want within a wide window
        t_min = t + want * 0.7
        t_max = min(duration, t + want * 1.35)
        candidates = [b for b in beats if t_min <= b <= t_max]
        if candidates:
            slot_end = min(candidates, key=lambda b: abs(b - (t + want)))
        else:
            slot_end = min(duration, t + want)
        slot_dur = slot_end - t
        if slot_dur < 0.8:
            t = slot_end
            continue

        # allow scenes shorter than slot — we'll crop from source file past
        # scene boundary if needed (acceptable for moody B&W pacing)
        needed = slot_dur * 0.6
        best = None
        best_score = -1e9
        shuffled = list(analyses)
        rng.shuffle(shuffled)
        for sa in shuffled:
            hits = sum(1 for r in recent[-6:] if r == sa.path)
            penalty = {0: 0.0, 1: 0.35}.get(hits, 0.9 + 0.25 * hits)
            if sa.path in recent[-1:]:
                penalty = max(penalty, 1.1)
            for scene in sa.scenes:
                if scene.duration < needed:
                    continue
                if first_slot:
                    # hook: reward punchy brightness + motion + sharpness
                    score = (
                        0.9 * scene.motion + 0.6 * scene.sharpness + 0.4 * scene.brightness
                        + 0.1 * rng.random()
                    )
                else:
                    motion_match = 1.0 - abs(scene.motion - energy)
                    score = (
                        motion_match
                        + 0.25 * scene.sharpness
                        + 0.15 * scene.brightness
                        + 0.15 * rng.random()
                        - penalty
                    )
                if score > best_score:
                    best_score = score
                    best = (sa.path, scene)

        if best is None:
            # looser fallback: allow any scene >= slot_dur*0.9, crop if needed;
            # prefer sources NOT in recent window
            pool = []
            for sa in analyses:
                hits = sum(1 for r in recent[-6:] if r == sa.path)
                for sc in sa.scenes:
                    if sc.duration >= slot_dur * 0.9:
                        pool.append((hits, -sc.sharpness - sc.motion, sa, sc))
            pool.sort(key=lambda x: (x[0], x[1], rng.random()))
            if pool:
                _, _, sa, scene = pool[0]
                best = (sa.path, scene)

        if best is None:
            t = slot_end
            continue

        src_path, scene = best
        margin = 0.05
        src_start_min = scene.start + margin
        src_start_max = scene.end - slot_dur - margin
        if src_start_max <= src_start_min:
            src_start = max(scene.start, scene.end - slot_dur)
        else:
            src_start = rng.uniform(src_start_min, src_start_max)

        slots.append(
            Slot(
                t_start=t,
                t_end=slot_end,
                source=Path(src_path),
                src_start=src_start,
                src_end=src_start + slot_dur,
                section=section,
                energy=energy,
                motion=scene.motion,
            )
        )
        recent.append(src_path)
        first_slot = False
        t = slot_end

    return slots


# All LOOK filter chains. Each value is a list of ffmpeg filter strings
# applied after geometry. Keep order stable; first added → first applied.
LOOK_FILTERS = {
    'original':     [],
    'neo_noir':     [
        "colorchannelmixer=.3:.59:.11:0:.3:.59:.11:0:.3:.59:.11:0",
        "curves=preset=increase_contrast",
        "eq=contrast=1.12:gamma=0.95:brightness=0.02",
    ],
    'bw_classic':   ["hue=s=0", "eq=contrast=1.10:brightness=0.02:gamma=1.00"],
    'bw_contrast':  ["hue=s=0", "eq=contrast=1.40:brightness=0.00:gamma=0.90"],
    'bw_soft':      ["hue=s=0", "eq=contrast=0.92:brightness=0.05:gamma=1.10"],
    'bw_grain':     ["hue=s=0", "eq=contrast=1.15:brightness=0.02", "noise=alls=14:allf=t"],
    'color_punchy': ["eq=saturation=1.55:contrast=1.15:gamma=0.92"],
    'color_filmic': ["curves=preset=lighter", "eq=saturation=0.85:contrast=1.05:gamma=1.02"],
    'teal_orange':  [
        "colorbalance=rs=-0.10:gs=0.00:bs=0.15:rm=0.10:gm=0.00:bm=-0.05:rh=0.15:gh=0.00:bh=-0.15",
        "eq=saturation=1.10:contrast=1.05",
    ],
}


def ffmpeg_filter(
    slot: Slot,
    width: int,
    height: int,
    shake: bool = False,
    look: str = 'neo_noir',
    original_frame: bool = False,
    exposure: float = 0.0,
) -> str:
    """Cinematic look, subtle motion, max quality."""
    dur = slot.src_end - slot.src_start
    fps = 30
    n_frames = max(1, int(dur * fps))

    parts: list[str] = []

    if original_frame:
        # Keep original aspect ratio; fit into target, then letterbox
        parts.append(
            f"scale='if(gt(iw/ih,{width}/{height}),{width},-1)':"
            f"'if(gt(iw/ih,{width}/{height}),-1,{height})':flags=lanczos"
        )
        parts.append(f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=black")
        # shake on letterboxed frame would shake the black bars — skip
        if shake:
            print("[ffmpeg_filter] shake disabled in original-frame mode")
            shake = False
    else:
        parts.append(f"scale={width * 2}:{height * 2}:force_original_aspect_ratio=increase:flags=lanczos")
        parts.append(f"crop={width * 2}:{height * 2}")

        if slot.section == "intro":
            zoom_expr = f"'min(1+0.025*on/{n_frames},1.035)'"
        elif slot.section == "drop":
            zoom_expr = f"'min(1+0.02*on/{n_frames},1.03)'"
        elif slot.section == "verse":
            zoom_expr = f"'min(1+0.015*on/{n_frames},1.025)'"
        else:
            zoom_expr = "1.0"

        parts.append(
            f"zoompan=z={zoom_expr}:d={n_frames}:"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={width}x{height}:fps={fps}"
        )

    if shake:
        amp = 15 if slot.section == "drop" else 8
        parts.append(
            f"crop={width-40}:{height-40}:"
            f"(iw-ow)/2+{amp}*sin(2*pi*t*5):"
            f"(ih-oh)/2+{amp}*cos(2*pi*t*7),"
            f"scale={width}:{height}"
        )

    # Look filters
    parts.extend(LOOK_FILTERS.get(look, LOOK_FILTERS['neo_noir']))

    # Exposure adjustment (-0.6 .. +0.6) translates to ffmpeg eq brightness
    if abs(exposure) > 1e-3:
        parts.append(f"eq=brightness={exposure:.2f}")

    if slot.section == "drop" and slot.energy > 0.65:
        parts.append("fade=t=in:st=0:d=0.08:color=white")

    parts.append("setsar=1")
    parts.append(f"fps={fps}")
    return ",".join(parts)


def render_clip(
    slot: Slot,
    out: Path,
    width: int,
    height: int,
    shake: bool = False,
    look: str = 'neo_noir',
    original_frame: bool = False,
    exposure: float = 0.0,
) -> None:
    vf = ffmpeg_filter(
        slot, width, height,
        shake=shake, look=look,
        original_frame=original_frame, exposure=exposure,
    )
    dur = slot.src_end - slot.src_start
    run(
        [
            "ffmpeg", "-y",
            "-ss", f"{slot.src_start:.3f}",
            "-i", str(slot.source),
            "-t", f"{dur:.3f}",
            "-vf", vf,
            "-c:v", "libx264", "-preset", "medium", "-crf", "15",
            "-profile:v", "high", "-level", "4.0",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart", "-an",
            str(out),
        ]
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", required=True,
                    help="folder with reference videos")
    ap.add_argument("--audio", required=True)
    ap.add_argument("--output", required=True)
    ap.add_argument("--duration", type=float, default=None,
                    help="target length in seconds (default 30)")
    ap.add_argument("--offset", type=float, default=0.0)
    ap.add_argument("--width", type=int, default=1080)
    ap.add_argument("--height", type=int, default=1920)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--shake", action="store_true", help="enable camera shake effects")
    ap.add_argument("--look", default="neo_noir",
                    choices=list(LOOK_FILTERS.keys()),
                    help="look/filter to apply")
    ap.add_argument("--original-frame", action="store_true", help="use original frame aspect ratio")
    ap.add_argument("--font-size", type=int, default=50)
    ap.add_argument("--font-family", default="JetBrains Mono")
    ap.add_argument("--captions", default="auto", help="auto|none|<path.ass>")
    ap.add_argument("--caption-anim", default="bounce",
                    help="none|reel_boss|bounce|shake|glitch|jump|flicker")
    ap.add_argument("--display-mode", default="word",
                    choices=["word", "phrase"],
                    help="caption layout: per-word groups or phrase chunks")
    ap.add_argument("--exposure", type=float, default=0.0,
                    help="exposure adjustment, -0.6..+0.6")
    ap.add_argument("--model", default="medium",
                    help="whisper size: tiny|base|small|medium|large-v3")
    ap.add_argument("--no-demucs", action="store_true")
    ap.add_argument("--language")
    ap.add_argument("--lyrics", default=None,
                    help="path to ground-truth lyrics file for forced alignment")
    ap.add_argument("--lyrics-start-line", type=int, default=1,
                    help="line number where actual lyrics start (1-indexed)")
    ap.add_argument("--track-id", default=None,
                    help="logical track id used for render_log keying / exclude-used")
    ap.add_argument("--exclude-used", action="store_true",
                    help="skip source files already used for this track in render_log.json")
    ap.add_argument("--keep-tmp", action="store_true")
    args = ap.parse_args()

    if shutil.which("ffmpeg") is None:
        sys.exit("[!] ffmpeg not in PATH")
    if shutil.which("ffprobe") is None:
        sys.exit("[!] ffprobe not in PATH")

    if args.duration is None:
        args.duration = 30.0

    audio_path = Path(args.audio)
    if not audio_path.is_file():
        sys.exit(f"[!] audio not found: {audio_path}")

    sources_dir = Path(args.sources)
    if not sources_dir.is_dir():
        sys.exit(f"[!] sources dir not found: {sources_dir}")
    sources = sorted(p for p in sources_dir.rglob("*") if p.suffix.lower() in VIDEO_EXTS)
    if not sources:
        sys.exit(f"[!] no videos in {sources_dir}")

    # logical track key — prefer explicit --track-id, else parent folder name
    track_key = args.track_id or audio_path.parent.name or audio_path.name
    audio_name = audio_path.name  # kept for backwards compat in log

    # load render history early — used for --exclude-used and for numbering this edit
    log_path = CACHE_DIR / "render_log.json"
    try:
        history = json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else []
    except Exception:
        history = []
    edit_num = sum(
        1 for e in history
        if e.get("track_id") == track_key or (e.get("track_id") is None and e.get("audio") == audio_name)
    ) + 1

    if args.exclude_used:
        used: set[str] = set()
        for entry in history:
            same = entry.get("track_id") == track_key or (
                entry.get("track_id") is None and entry.get("audio") == audio_name
            )
            if not same:
                continue
            for s in entry.get("sources", []):
                try:
                    used.add(str(Path(s).resolve()).lower())
                except Exception:
                    used.add(str(s).lower())
        before = len(sources)
        sources = [p for p in sources if str(p.resolve()).lower() not in used]
        dropped = before - len(sources)
        print(f"[exclude-used] dropped {dropped} already-used clips, {len(sources)} fresh remain")
        if not sources:
            sys.exit("[!] all sources already used for this track — add new refs or drop --exclude-used")

    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.parent / f".{output.stem}_tmp"
    tmp.mkdir(exist_ok=True)

    print(f"[1/6] Song analysis: {Path(args.audio).name}")
    from song_analysis import analyze_song
    song = analyze_song(Path(args.audio), offset=args.offset, duration=args.duration)
    print(f"      BPM={song.bpm:.1f}  beats={len(song.beats)}  drops={len(song.drops)}")

    print(f"[2/6] Scene detection on {len(sources)} clips (cached)")
    from scene_detect import analyze_source
    analyses = []
    for i, src in enumerate(sources):
        try:
            a = analyze_source(src, CACHE_DIR)
            analyses.append(a)
            if i < 3 or (i + 1) % 10 == 0 or i == len(sources) - 1:
                print(f"      {i+1}/{len(sources)} scenes={len(a.scenes)}  {src.name[:42]}")
        except Exception as e:
            print(f"      ! skip {src.name}: {e}")
    total_scenes = sum(len(a.scenes) for a in analyses)
    print(f"      -> {total_scenes} scenes total")

    print(f"[3/6] Timeline composition")
    slots = build_timeline(song, analyses, args.duration, seed=args.seed)
    print(f"      -> {len(slots)} slots")

    print(f"[4/6] Rendering {len(slots)} clips with LUT+effects")
    clip_paths: list[Path] = []
    for i, slot in enumerate(slots):
        out = tmp / f"clip_{i:03d}.mp4"
        try:
            render_clip(
                slot, out, args.width, args.height,
                shake=args.shake, look=args.look,
                original_frame=args.original_frame,
                exposure=args.exposure,
            )
            clip_paths.append(out)
            if i < 5 or (i + 1) % 5 == 0:
                print(
                    f"      {i+1:02d}/{len(slots)} {slot.section:5s} "
                    f"e={slot.energy:.2f} m={slot.motion:.2f} "
                    f"{slot.src_end-slot.src_start:.2f}s  {slot.source.name[:30]}"
                )
        except subprocess.CalledProcessError as e:
            msg = (e.stderr or "")[-200:]
            print(f"      ! render fail: {msg}")

    if not clip_paths:
        sys.exit("[!] nothing rendered")

    print(f"[5/6] Concat + audio mix")
    concat_list = tmp / "concat.txt"

    def _concat_escape(p: Path) -> str:
        s = str(p).replace("\\", "/")
        s = s.replace("'", "'\\''")
        return f"file '{s}'"

    concat_list.write_text(
        "\n".join(_concat_escape(p) for p in clip_paths),
        encoding="utf-8",
    )
    video_only = tmp / "video_only.mp4"
    # concat without re-encode — preserves per-clip quality from render_clip
    run(
        [
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", str(concat_list),
            "-c", "copy", "-an", str(video_only),
        ]
    )
    with_audio = tmp / "with_audio.mp4"
    run(
        [
            "ffmpeg", "-y", "-i", str(video_only),
            "-ss", f"{args.offset:.3f}", "-i", args.audio,
            "-t", f"{args.duration:.3f}",
            "-map", "0:v", "-map", "1:a",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest", str(with_audio),
        ]
    )

    if args.captions != "none":
        print(f"[6/6] Captions  whisper={args.model} demucs={not args.no_demucs}")
        caps_path = args.captions
        if caps_path == "auto":
            from transcribe import transcribe
            caps_path = str(tmp / "captions.ass")
            lyrics_text = None
            if args.lyrics:
                all_lines = Path(args.lyrics).read_text(encoding="utf-8").splitlines()
                # Slicing lines based on 1-indexed start line
                actual_lyrics_lines = all_lines[max(0, args.lyrics_start_line - 1):]
                lyrics_text = "\n".join(actual_lyrics_lines)
                print(f"[captions] forced alignment from {args.lyrics} (starting at line {args.lyrics_start_line})")
            transcribe(
                audio_path=Path(args.audio),
                out_ass=Path(caps_path),
                offset=args.offset,
                duration=args.duration,
                width=args.width,
                height=args.height,
                font_size=args.font_size,
                font_family=args.font_family,
                model_size=args.model,
                use_demucs=not args.no_demucs,
                tmp_dir=tmp / "demucs",
                language=args.language,
                lyrics=lyrics_text,
                caption_anim=args.caption_anim,
                display_mode=args.display_mode,
            )
        sub_escaped = str(caps_path).replace("\\", "/").replace(":", "\\:").replace("'", "'\\''")
        fonts_dir = str(SKILL_DIR / "assets" / "fonts").replace("\\", "/").replace(":", "\\:").replace("'", "'\\''")
        run(
            [
                "ffmpeg", "-y", "-i", str(with_audio),
                "-vf", f"subtitles='{sub_escaped}':fontsdir='{fonts_dir}'",
                "-c:a", "copy",
                "-c:v", "libx264", "-preset", "medium", "-crf", "15",
                "-profile:v", "high", "-level", "4.0",
                "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(output),
            ]
        )
    else:
        shutil.move(str(with_audio), str(output))

    if not args.keep_tmp:
        shutil.rmtree(tmp, ignore_errors=True)

    r = run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(output),
        ]
    )
    dur = float(r.stdout.strip())
    size_mb = output.stat().st_size / 1024 / 1024
    print(f"\n[OK] {output}  {dur:.1f}s  {size_mb:.1f}MB  {len(clip_paths)} clips")

    # render log — used by Claude to pick fresh seeds / number edits across sessions
    from datetime import datetime
    import time
    
    lock_path = log_path.with_suffix('.lock')
    for _ in range(50):
        try:
            lock_path.mkdir()
            break
        except FileExistsError:
            time.sleep(0.1)
            
    try:
        try:
            current_history = json.loads(log_path.read_text(encoding="utf-8")) if log_path.exists() else []
        except Exception:
            current_history = []
            
        current_history.append({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "edit_num": edit_num,
            "track_id": track_key,
            "audio": audio_name,
            "seed": args.seed,
            "offset": args.offset,
            "duration": args.duration,
            "look": args.look,
            "shake": bool(args.shake),
            "original_frame": bool(args.original_frame),
            "display_mode": args.display_mode,
            "caption_anim": args.caption_anim,
            "exposure": args.exposure,
            "lyrics": Path(args.lyrics).name if args.lyrics else None,
            "sources": [str(slot.source) for slot in slots][:50],
            "output": str(output),
            "size_mb": round(size_mb, 1),
            "clips": len(clip_paths),
        })
        tmp_log = log_path.with_suffix('.tmp')
        tmp_log.write_text(json.dumps(current_history, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_log.replace(log_path)
    finally:
        try:
            lock_path.rmdir()
        except OSError:
            pass


if __name__ == "__main__":
    main()
