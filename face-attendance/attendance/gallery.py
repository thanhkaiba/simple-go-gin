"""In-memory face gallery: several reference embeddings per employee, matched by cosine similarity."""
from __future__ import annotations

import threading

import numpy as np


def normalize(v: np.ndarray) -> np.ndarray:
    v = np.asarray(v, dtype=np.float32)
    n = np.linalg.norm(v)
    return v / n if n > 0 else v


class Gallery:
    def __init__(self, items: list[tuple[str, np.ndarray]] | None = None):
        self._lock = threading.Lock()
        self._by_person: dict[str, np.ndarray] = {}
        self.replace(items or [])

    def replace(self, items: list[tuple[str, np.ndarray]]) -> None:
        grouped: dict[str, list[np.ndarray]] = {}
        for person, vec in items:
            grouped.setdefault(person, []).append(normalize(vec))
        with self._lock:
            self._by_person = {p: np.stack(v) for p, v in grouped.items()}

    def add(self, person: str, vec: np.ndarray) -> None:
        vec = normalize(vec)[None, :]
        with self._lock:
            current = self._by_person.get(person)
            self._by_person[person] = vec if current is None else np.vstack([current, vec])

    def counts(self) -> dict[str, int]:
        with self._lock:
            return {p: len(m) for p, m in self._by_person.items()}

    def match(self, embedding: np.ndarray, k: int = 2) -> list[tuple[str, float]]:
        """Return the k best (person, score) pairs, best first.

        A person's score is the mean of their two closest reference images, which is less
        sensitive to a single odd reference photo than taking only the best one.
        """
        emb = normalize(embedding)
        with self._lock:
            items = list(self._by_person.items())
        scores = []
        for person, refs in items:
            sims = refs @ emb
            top = np.sort(sims)[-2:] if len(sims) >= 2 else sims
            scores.append((person, float(top.mean())))
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:k]
