"""Follow each face across frames and decide only after several consistent, good-quality frames.

A single frame is never trusted: a person is identified when at least `votes_required`
good frames agree on them with a clear margin over the runner-up. Tracks that end without
a decision become "unknown" so an admin can look at them.
"""
from __future__ import annotations

import datetime as dt
import itertools
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import numpy as np

LINK_IOU = 0.2  # same position as the previous frame
LINK_SIM = 0.5  # or clearly the same face
LINK_MIN_SIM = 0.3  # never link faces that look like different people, even at the same position
SPLIT_WINDOW = dt.timedelta(seconds=5)


@dataclass
class Observation:
    bbox: tuple[float, float, float, float]
    embedding: np.ndarray
    candidates: list[tuple[str, float]]  # best gallery matches, best first
    quality_ok: bool
    quality: float  # used to pick the best image of a track
    crop: Any = None


@dataclass
class Decision:
    kind: str  # "identified" or "unknown"
    ts: dt.datetime  # when the track was first seen
    employee_id: str | None
    score: float | None
    crop: Any
    embedding: np.ndarray


@dataclass
class _Track:
    id: int
    first_ts: dt.datetime
    last_ts: dt.datetime
    bbox: tuple[float, float, float, float]
    embedding: np.ndarray
    frames: int = 0
    votes: Counter = field(default_factory=Counter)
    best_score: dict[str, float] = field(default_factory=dict)
    best: Observation | None = None
    decided: str | None = None


def iou(a, b) -> float:
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


class FaceTracker:
    def __init__(self, accept_score: float, min_margin: float, votes_required: int,
                 timeout_s: float, unknown_min_frames: int):
        self.accept_score = accept_score
        self.min_margin = min_margin
        self.votes_required = votes_required
        self.timeout = dt.timedelta(seconds=timeout_s)
        self.unknown_min_frames = unknown_min_frames
        self._tracks: list[_Track] = []
        self._ended: list[_Track] = []  # undecided tracks waiting out SPLIT_WINDOW before becoming "unknown"
        self._ids = itertools.count(1)
        self._recent: dict[str, dt.datetime] = {}  # employee -> last time a track identified them

    def update(self, observations: list[Observation], ts: dt.datetime) -> list[Decision]:
        decisions = self._expire(ts)
        free = list(self._tracks)
        for obs in observations:
            track = self._link(obs, free)
            if track is None:
                track = _Track(next(self._ids), ts, ts, obs.bbox, obs.embedding)
                self._tracks.append(track)
            else:
                free.remove(track)
            decision = self._observe(track, obs, ts)
            if decision:
                decisions.append(decision)
        return decisions

    def flush(self) -> list[Decision]:
        """End every track, e.g. when the processing window closes."""
        far_future = dt.datetime.max.replace(tzinfo=dt.timezone.utc)
        return self._expire(far_future, force=True)

    def _link(self, obs: Observation, tracks: list[_Track]) -> _Track | None:
        best, best_value = None, 0.0
        for t in tracks:
            sim = float(np.dot(obs.embedding, t.embedding))
            overlap = iou(obs.bbox, t.bbox)
            if sim < LINK_MIN_SIM or (overlap < LINK_IOU and sim < LINK_SIM):
                continue
            if overlap + sim > best_value:
                best, best_value = t, overlap + sim
        return best

    def _observe(self, t: _Track, obs: Observation, ts: dt.datetime) -> Decision | None:
        t.last_ts, t.bbox, t.embedding = ts, obs.bbox, obs.embedding
        t.frames += 1
        if t.best is None or obs.quality > t.best.quality:
            t.best = obs
        for person, score in obs.candidates:
            t.best_score[person] = max(score, t.best_score.get(person, -1.0))
        if not obs.quality_ok or not obs.candidates:
            return None

        person, top = obs.candidates[0]
        runner_up = obs.candidates[1][1] if len(obs.candidates) > 1 else -1.0
        if top >= self.accept_score and top - runner_up >= self.min_margin:
            t.votes[person] += 1

        if t.decided or not t.votes:
            return None
        leader, n = t.votes.most_common(1)[0]
        others = sum(t.votes.values()) - n
        if n >= self.votes_required and n >= 2 * others:
            t.decided = leader
            self._recent[leader] = ts
            return Decision("identified", t.first_ts, leader, t.best_score[leader], t.best.crop, t.best.embedding)
        return None

    def _expire(self, now: dt.datetime, force: bool = False) -> list[Decision]:
        alive = []
        for t in self._tracks:
            if not force and now - t.last_ts <= self.timeout:
                alive.append(t)
            elif t.decided:
                self._recent[t.decided] = t.last_ts
            elif t.frames >= self.unknown_min_frames:
                self._ended.append(t)
        self._tracks = alive

        decisions, waiting = [], []
        for t in self._ended:
            if not force and now - t.last_ts < SPLIT_WINDOW:
                waiting.append(t)
            elif not self._is_split(t):
                person, score = self._guess(t)
                decisions.append(Decision("unknown", t.first_ts, person, score, t.best.crop, t.best.embedding))
        self._ended = waiting
        return decisions

    @staticmethod
    def _guess(t: _Track) -> tuple[str | None, float | None]:
        return max(t.best_score.items(), key=lambda x: x[1]) if t.best_score else (None, None)

    def _is_split(self, t: _Track) -> bool:
        """An undecided track right next to an identified track of the person it resembles is that
        same person with their head turned for a moment, not a stranger worth reporting."""
        person, score = self._guess(t)
        if person is None or score < LINK_MIN_SIM:
            return False
        seen = self._recent.get(person)
        return seen is not None and (abs(t.first_ts - seen) <= SPLIT_WINDOW or abs(t.last_ts - seen) <= SPLIT_WINDOW)
