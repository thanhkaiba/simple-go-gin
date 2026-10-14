"""Compare recognition backends and choose accept_score / min_margin from labelled photos.

Layout: <dir>/<employee_id>/*.jpg, plus optionally <dir>/_unknown/*.jpg for people who are
not employees. With only --gallery, every photo is tested against all the other photos
(leave-one-out). The recommended setting is the one with zero wrong identifications that
accepts the most employee photos, taken from the middle of the range of safe thresholds so
it is not tuned to the edge of this particular set of photos.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .gallery import Gallery

UNKNOWN_DIR = "_unknown"
THRESHOLDS = [round(float(x), 3) for x in np.arange(0.20, 0.76, 0.025)]
MARGINS = [0.0, 0.04, 0.08, 0.12, 0.16]


@dataclass
class Sample:
    truth: str | None  # None for people who are not in the gallery
    top: str | None
    top_score: float
    second_score: float


@dataclass
class Row:
    threshold: float
    margin: float
    correct: int  # employee photos given the right name
    wrong: int  # given someone else's name (employee or stranger)
    rejected: int
    employees: int  # employee photos in the test set


def score(gallery: Gallery, truth: str | None, embedding: np.ndarray) -> Sample:
    m = gallery.match(embedding, k=2)
    top, s1 = m[0] if m else (None, -1.0)
    s2 = m[1][1] if len(m) > 1 else -1.0
    return Sample(truth, top, s1, s2)


def sweep(samples: list[Sample], thresholds=THRESHOLDS, margins=MARGINS) -> list[Row]:
    rows = []
    for t in thresholds:
        for m in margins:
            correct = wrong = rejected = 0
            for s in samples:
                if s.top is None or s.top_score < t or s.top_score - s.second_score < m:
                    rejected += 1
                elif s.top == s.truth:
                    correct += 1
                else:
                    wrong += 1
            rows.append(Row(t, m, correct, wrong, rejected, sum(s.truth is not None for s in samples)))
    return rows


def recommend(rows: list[Row]) -> Row | None:
    safe = [r for r in rows if r.wrong == 0 and r.correct > 0]
    if not safe:
        return None
    best = max(r.correct for r in safe)
    margin = min(r.margin for r in safe if r.correct == best)
    candidates = sorted((r for r in safe if r.correct == best and r.margin == margin), key=lambda r: r.threshold)
    return candidates[len(candidates) // 2]


def _embed_dir(recognizer, folder: Path) -> list[tuple[str | None, str, np.ndarray]]:
    from .enroll import images_in, read_image

    out = []
    for person_dir in sorted(p for p in folder.iterdir() if p.is_dir()):
        truth = None if person_dir.name == UNKNOWN_DIR else person_dir.name
        for path in images_in(person_dir):
            image = read_image(path)
            face = recognizer.largest_face(image) if image is not None else None
            if face is None:
                print(f"  - {person_dir.name}/{path.name}: no face found")
                continue
            out.append((truth, path.name, face.embedding))
    return out


def _samples(recognizer, gallery_dir: Path, test_dir: Path | None) -> list[Sample]:
    enrolled = [(t, n, e) for t, n, e in _embed_dir(recognizer, gallery_dir) if t is not None]
    if test_dir is None:
        samples = []
        for i, (truth, _, emb) in enumerate(enrolled):
            others = Gallery([(t, e) for j, (t, _, e) in enumerate(enrolled) if j != i])
            samples.append(score(others, truth, emb))
        return samples
    gallery = Gallery([(t, e) for t, _, e in enrolled])
    enrolled_names = {(t, n) for t, n, _ in enrolled}
    samples = []
    for truth, name, emb in _embed_dir(recognizer, test_dir):
        if (truth, name) in enrolled_names:
            print(f"  ! {truth}/{name} is in both gallery and test sets; results will look better than reality")
        samples.append(score(gallery, truth, emb))
    return samples


def evaluate(cfg, gallery_dir: Path, test_dir: Path | None, backends: list[str]) -> None:
    from .recognizer import FaceRecognizer

    if test_dir is None:
        print("No --test folder: each photo is tested against all the other photos (leave-one-out).")
    results = {}
    for backend in backends:
        rc = dataclasses.replace(cfg.recognition, backend=backend)
        if backend != cfg.recognition.backend:
            rc = dataclasses.replace(rc, detector_model="", recognizer_model="")
        print(f"\n=== backend: {backend} ===")
        samples = _samples(FaceRecognizer(rc, cfg.base_dir), gallery_dir, test_dir)
        rows = sweep(samples)
        best = recommend(rows)
        results[backend] = best
        print(f"{len(samples)} test photos, {sum(s.truth is None for s in samples)} of non-employees.")
        margin = best.margin if best else 0.08
        print(f"min_margin {margin}:  {'threshold':>9} {'recognised':>11} {'WRONG':>6} {'rejected':>9}")
        for r in rows:
            if r.margin == margin:
                print(f"{'':17s}{r.threshold:9.3f} {r.correct:5d}/{r.employees:<5d} {r.wrong:6d} {r.rejected:9d}")
        if best is None:
            print("No setting avoids wrong identifications. Add or replace reference photos.")
        else:
            print(f"Recommended: accept_score: {best.threshold}  min_margin: {best.margin} -> "
                  f"{best.correct}/{best.employees} employee photos recognised "
                  f"({best.correct / max(best.employees, 1):.0%}), 0 wrong")

    if len(results) > 1:
        ranked = sorted(((b, r) for b, r in results.items() if r), key=lambda x: x[1].correct, reverse=True)
        if len(ranked) > 1 and ranked[0][1].correct == ranked[1][1].correct:
            print("\nThe backends did equally well on these photos; test with more, harder photos "
                  "(dark, blurred, side views, masks) to tell them apart.")
        elif ranked:
            print(f"\nMost accurate on your photos: {ranked[0][0]}")
    print("\nThese are single-photo results. Live, several frames must agree, so the per-visit accuracy is higher.")
    print("Add about 0.02 to accept_score as a safety margin.")
