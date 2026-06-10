"""Analyze song: beats, downbeats, energy envelope, drops, sections."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import librosa
import numpy as np


@dataclass
class SongAnalysis:
    path: str
    sr: int
    duration: float
    bpm: float
    beats: list[float]
    downbeats: list[float]
    energy: list[tuple[float, float]]
    drops: list[float]
    sections: list[tuple[float, float, str]]

    def energy_at(self, t: float) -> float:
        if not self.energy:
            return 0.0
        for i, (tt, _v) in enumerate(self.energy):
            if tt > t:
                return self.energy[max(i - 1, 0)][1]
        return self.energy[-1][1]

    def section_at(self, t: float) -> str:
        for s, e, label in self.sections:
            if s <= t < e:
                return label
        return self.sections[-1][2] if self.sections else "outro"


def analyze_song(audio_path: Path, offset: float = 0.0, duration: float | None = None) -> SongAnalysis:
    y, sr = librosa.load(
        str(audio_path), sr=22050, mono=True, offset=offset, duration=duration
    )
    actual_dur = len(y) / sr

    tempo, beat_frames = librosa.beat.beat_track(y=y, sr=sr)
    beats = librosa.frames_to_time(beat_frames, sr=sr).tolist()
    downbeats = beats[::4]

    onset_env = librosa.onset.onset_strength(y=y, sr=sr, hop_length=512)
    times = librosa.frames_to_time(np.arange(len(onset_env)), sr=sr, hop_length=512)
    env_max = float(onset_env.max()) if onset_env.max() > 0 else 1.0
    env_norm = onset_env / env_max
    step = max(1, len(env_norm) // 120)
    energy = [(float(times[i]), float(env_norm[i])) for i in range(0, len(env_norm), step)]

    drops: list[float] = []
    last_drop = -10.0
    for t, v in energy:
        if v > 0.7 and t - last_drop > 2.5:
            drops.append(t)
            last_drop = t

    d = actual_dur
    sections = [
        (0.0, d * 0.15, "intro"),
        (d * 0.15, d * 0.4, "verse"),
        (d * 0.4, d * 0.85, "drop"),
        (d * 0.85, d, "outro"),
    ]

    bpm_val = float(tempo.item() if hasattr(tempo, "item") else tempo)

    return SongAnalysis(
        path=str(audio_path),
        sr=sr,
        duration=actual_dur,
        bpm=bpm_val,
        beats=beats,
        downbeats=downbeats,
        energy=energy,
        drops=drops,
        sections=sections,
    )
