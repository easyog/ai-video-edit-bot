"""Detect scenes in source videos, score motion/brightness/sharpness, cache to JSON."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np


@dataclass
class Scene:
    start: float
    end: float
    motion: float
    brightness: float
    sharpness: float

    @property
    def duration(self) -> float:
        return self.end - self.start


@dataclass
class SourceAnalysis:
    path: str
    duration: float
    width: int
    height: int
    scenes: list[Scene]


def _cache_key(path: Path) -> str:
    st = path.stat()
    return hashlib.md5(f"{path}:{st.st_mtime}:{st.st_size}".encode()).hexdigest()


def analyze_source(path: Path, cache_dir: Path, threshold: float = 27.0) -> SourceAnalysis:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_file = cache_dir / f"{_cache_key(path)}.json"
    if cache_file.exists():
        data = json.loads(cache_file.read_text(encoding="utf-8"))
        return SourceAnalysis(
            path=data["path"],
            duration=data["duration"],
            width=data["width"],
            height=data["height"],
            scenes=[Scene(**s) for s in data["scenes"]],
        )

    from scenedetect import SceneManager, open_video
    from scenedetect.detectors import ContentDetector

    video = open_video(str(path))
    sm = SceneManager()
    sm.add_detector(ContentDetector(threshold=threshold))
    sm.detect_scenes(video)
    scene_list = sm.get_scene_list()

    cap = cv2.VideoCapture(str(path))
    fps = cap.get(cv2.CAP_PROP_FPS) or 30
    width = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    height = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = total_frames / fps if fps else 0.0

    if not scene_list:
        scene_bounds = [(0.0, duration)]
    else:
        scene_bounds = [(s.get_seconds(), e.get_seconds()) for s, e in scene_list]

    scenes: list[Scene] = []
    for start_s, end_s in scene_bounds:
        scene_dur = end_s - start_s
        if scene_dur < 0.3:
            continue
        n_samples = min(6, max(3, int(scene_dur * 2)))
        sample_times = np.linspace(start_s + 0.05, max(start_s + 0.06, end_s - 0.05), n_samples)
        frames = []
        for t in sample_times:
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000)
            ok, frame = cap.read()
            if ok and frame is not None:
                frames.append(frame)
        if len(frames) < 2:
            continue
        small = [cv2.resize(f, (320, 180)) for f in frames]
        gray = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) for f in small]
        diffs = [
            float(np.mean(np.abs(gray[i + 1].astype(np.int16) - gray[i].astype(np.int16))))
            for i in range(len(gray) - 1)
        ]
        motion = float(np.mean(diffs)) / 255.0 if diffs else 0.0
        brightness = float(np.mean(gray[len(gray) // 2])) / 255.0
        lap_vars = [float(cv2.Laplacian(g, cv2.CV_64F).var()) for g in gray]
        sharpness = min(1.0, float(np.mean(lap_vars)) / 1000.0)

        scenes.append(
            Scene(
                start=float(start_s),
                end=float(end_s),
                motion=min(1.0, motion * 5.0),
                brightness=brightness,
                sharpness=sharpness,
            )
        )

    cap.release()

    result = SourceAnalysis(
        path=str(path),
        duration=duration,
        width=width,
        height=height,
        scenes=scenes,
    )
    cache_file.write_text(
        json.dumps(
            {
                "path": result.path,
                "duration": result.duration,
                "width": result.width,
                "height": result.height,
                "scenes": [asdict(s) for s in result.scenes],
            }
        ),
        encoding="utf-8",
    )
    return result
