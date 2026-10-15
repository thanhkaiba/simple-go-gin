"""Compare recognition backends and choose accept_score / min_margin from labelled photos.

Layout: <dir>/<employee_id>/*.jpg, plus optionally <dir>/_unknown/*.jpg for people who are
not employees. Frames of one walk past the camera can go in a subfolder,
<dir>/<employee_id>/<visit>/*.jpg: they are tested as one visit, voting like the live system.

With only --gallery, every photo is tested against all the other photos (leave-one-out). A
whole visit is left out at once: frames of the same walk are nearly identical, and matching
one against its neighbours would make the results look far better than reality.

The recommended setting is the one with zero wrong identifications that accepts the most
employee photos, taken from the middle of the range of safe thresholds so it is not tuned
to the edge of this particular set of photos. Each backend is tested with and without
centering (recognition.center_embeddings), which needs its own threshold.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .gallery import MIN_PEOPLE_TO_CENTER, Gallery

UNKNOWN_DIR = "_unknown"
THRESHOLDS = [round(float(x), 3) for x in np.arange(0.20, 0.76, 0.025)]
MARGINS = [0.0, 0.04, 0.08, 0.12, 0.16]


@dataclass
class Sample:
    truth: str | None  # None for people who are not in the gallery
    top: str | None
    top_score: float
    second_score: float
    visit: str = ""  # frames of one walk past the camera share this


@dataclass
class Row:
    threshold: float
    margin: float
    correct: int  # employee photos given the right name
    wrong: int  # given someone else's name (employee or stranger)
    rejected: int
    employees: int  # employee photos in the test set


def score(gallery: Gallery, truth: str | None, embedding: np.ndarray, visit: str = "") -> Sample:
    m = gallery.match(embedding, k=2)
    top, s1 = m[0] if m else (None, -1.0)
    s2 = m[1][1] if len(m) > 1 else -1.0
    return Sample(truth, top, s1, s2, visit)


def accepted(s: Sample, threshold: float, margin: float) -> str | None:
    if s.top is None or s.top_score < threshold or s.top_score - s.second_score < margin:
        return None
    return s.top


def visits(samples: list[Sample], threshold: float, margin: float, votes_required: int = 3) -> Row:
    """Per visit, as the live tracker decides: enough agreeing frames and at least twice as
    many votes as every other name together (a short visit needs all its frames)."""
    groups: dict[tuple, list[Sample]] = {}
    for s in samples:
        groups.setdefault((s.truth, s.visit or id(s)), []).append(s)
    correct = wrong = rejected = 0
    for (truth, _), frames in groups.items():
        votes: dict[str, int] = {}
        for s in frames:
            name = accepted(s, threshold, margin)
            if name:
                votes[name] = votes.get(name, 0) + 1
        leader, n = max(votes.items(), key=lambda kv: kv[1]) if votes else (None, 0)
        if leader is None or n < min(votes_required, len(frames)) or n < 2 * (sum(votes.values()) - n):
            rejected += 1
        elif leader == truth:
            correct += 1
        else:
            wrong += 1
    return Row(threshold, margin, correct, wrong, rejected, sum(truth is not None for truth, _ in groups))


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


@dataclass
class Photo:
    truth: str | None
    visit: str
    name: str
    plain: np.ndarray  # as the camera sees it
    mirrored: np.ndarray  # as a reference photo is enrolled (averaged with its mirror image)


def _embed_dir(recognizer, folder: Path) -> list[Photo]:
    from .enroll import images_in, read_image

    out = []
    for person_dir in sorted(p for p in folder.iterdir() if p.is_dir()):
        truth = None if person_dir.name == UNKNOWN_DIR else person_dir.name
        for path in images_in(person_dir, subfolders=True):
            image = read_image(path)
            face = recognizer.largest_face(image) if image is not None else None
            if face is None:
                print(f"  - {person_dir.name}/{path.relative_to(person_dir)}: no face found")
                continue
            mirrored = recognizer.largest_face(image, mirror=True).embedding
            visit = path.parent.name if path.parent != person_dir else path.name
            out.append(Photo(truth, visit, path.relative_to(person_dir).as_posix(), face.embedding, mirrored))
    return out


def _samples(enrolled: list[Photo], tests: list[Photo] | None, center: bool) -> list[Sample]:
    if tests is None:
        samples = []
        for p in enrolled:
            others = Gallery([(o.truth, o.mirrored) for o in enrolled
                              if not (o.truth == p.truth and o.visit == p.visit)], center)
            samples.append(score(others, p.truth, p.plain, p.visit))
        return samples
    gallery = Gallery([(p.truth, p.mirrored) for p in enrolled], center)
    enrolled_names = {(p.truth, p.name) for p in enrolled}
    samples = []
    for p in tests:
        if (p.truth, p.name) in enrolled_names:
            print(f"  ! {p.truth}/{p.name} is in both gallery and test sets; results will look better than reality")
        samples.append(score(gallery, p.truth, p.plain, p.visit))
    return samples


def evaluate(cfg, gallery_dir: Path, test_dir: Path | None, backends: list[str]) -> None:
    from .recognizer import FaceRecognizer

    if test_dir is None:
        print("No --test folder: each photo (or visit folder) is tested against all the other photos (leave-one-out).")
    results = {}
    for backend in backends:
        rc = dataclasses.replace(cfg.recognition, backend=backend)
        if backend != cfg.recognition.backend:
            rc = dataclasses.replace(rc, detector_model="", recognizer_model="")
        recognizer = FaceRecognizer(rc, cfg.base_dir)
        enrolled = [p for p in _embed_dir(recognizer, gallery_dir) if p.truth is not None]
        tests = _embed_dir(recognizer, test_dir) if test_dir is not None else None
        people = len({p.truth for p in enrolled})
        for center in (False, True):
            label = f"{backend}, {'centered' if center else 'raw cosine'}"
            if center and people < MIN_PEOPLE_TO_CENTER:
                print(f"\n=== {label}: skipped, centering needs photos of at least {MIN_PEOPLE_TO_CENTER} people ===")
                continue
            print(f"\n=== {label} ===")
            samples = _samples(enrolled, tests, center)
            rows = sweep(samples)
            best = recommend(rows)
            results[label] = best
            print(f"{len(samples)} test photos, {sum(s.truth is None for s in samples)} of non-employees.")
            margin = best.margin if best else 0.08
            print(f"min_margin {margin}:  {'threshold':>9} {'recognised':>11} {'WRONG':>6} {'rejected':>9}")
            for r in rows:
                if r.margin == margin:
                    print(f"{'':17s}{r.threshold:9.3f} {r.correct:5d}/{r.employees:<5d} {r.wrong:6d} {r.rejected:9d}")
            if best is None:
                print("No setting avoids wrong identifications. Add or replace reference photos.")
                continue
            v = visits(samples, best.threshold, best.margin, cfg.recognition.votes_required)
            print(f"Recommended: accept_score: {best.threshold}  min_margin: {best.margin}"
                  f"{'  center_embeddings: true' if center else ''} -> "
                  f"{best.correct}/{best.employees} employee photos recognised "
                  f"({best.correct / max(best.employees, 1):.0%}), 0 wrong")
            print(f"  per visit (votes like the live system): {v.correct}/{v.employees} recognised, "
                  f"{v.wrong} wrong, {v.rejected} sent for review")

    ranked = sorted(((k, r) for k, r in results.items() if r), key=lambda x: x[1].correct, reverse=True)
    if len(ranked) > 1:
        if ranked[0][1].correct == ranked[1][1].correct:
            print(f"\n{ranked[0][0]} and {ranked[1][0]} did equally well on these photos; test with more, harder "
                  "photos (dark, blurred, side views, masks) to tell them apart.")
        else:
            print(f"\nMost accurate on your photos: {ranked[0][0]}")
    print("\nThese are single-photo results. Live, several frames must agree, so the per-visit accuracy is higher.")
    print("Add about 0.02 to accept_score as a safety margin.")
