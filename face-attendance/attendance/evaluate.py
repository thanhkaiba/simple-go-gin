"""Choose accept_score / min_margin from labelled photos.

Layout: <dir>/<employee_id>/*.jpg, plus optionally <dir>/_unknown/*.jpg for people who are
not employees. With only --gallery, every photo is tested against all the other photos
(leave-one-out). The recommendation is the setting with zero wrong identifications that
accepts the most photos.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .gallery import Gallery

UNKNOWN_DIR = "_unknown"
THRESHOLDS = [round(x, 2) for x in np.arange(0.25, 0.76, 0.025)]
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
    correct: int
    wrong: int
    rejected: int

    @property
    def total(self) -> int:
        return self.correct + self.wrong + self.rejected


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
            rows.append(Row(t, m, correct, wrong, rejected))
    return rows


def recommend(rows: list[Row]) -> Row | None:
    safe = [r for r in rows if r.wrong == 0 and r.correct > 0]
    # Most accepted photos; on a tie take the stricter setting.
    return max(safe, key=lambda r: (r.correct, r.threshold, r.margin)) if safe else None


def _embed_dir(recognizer, folder: Path) -> list[tuple[str | None, str, np.ndarray]]:
    from .enroll import images_in, read_image

    out = []
    for person_dir in sorted(p for p in folder.iterdir() if p.is_dir()):
        truth = None if person_dir.name == UNKNOWN_DIR else person_dir.name
        for path in images_in(person_dir):
            image = read_image(path)
            face = recognizer.largest_face(image) if image is not None else None
            if face is None:
                print(f"  - {person_dir.name}/{path.name}: không tìm thấy khuôn mặt")
                continue
            out.append((truth, path.name, face.embedding))
    return out


def evaluate(cfg, gallery_dir: Path, test_dir: Path | None) -> None:
    from .recognizer import FaceRecognizer

    recognizer = FaceRecognizer(cfg.recognition)
    enrolled = [(t, n, e) for t, n, e in _embed_dir(recognizer, gallery_dir) if t is not None]
    samples = []
    if test_dir is None:
        print("Không có --test: thử từng ảnh với các ảnh còn lại (leave-one-out).")
        for i, (truth, _, emb) in enumerate(enrolled):
            others = Gallery([(t, e) for j, (t, _, e) in enumerate(enrolled) if j != i])
            samples.append(score(others, truth, emb))
    else:
        gallery = Gallery([(t, e) for t, _, e in enrolled])
        enrolled_names = {(t, n) for t, n, _ in enrolled}
        for truth, name, emb in _embed_dir(recognizer, test_dir):
            if (truth, name) in enrolled_names:
                print(f"  ⚠ {truth}/{name} có ở cả gallery và test, kết quả sẽ đẹp hơn thực tế")
            samples.append(score(gallery, truth, emb))

    rows = sweep(samples)
    best = recommend(rows)
    print(f"\n{len(samples)} ảnh thử, {sum(s.truth is None for s in samples)} ảnh người lạ.")
    print(f"{'ngưỡng':>7} {'margin':>7} {'đúng':>6} {'SAI':>5} {'từ chối':>8}")
    for r in rows:
        if r.margin == (best.margin if best else 0.08):
            print(f"{r.threshold:7.3f} {r.margin:7.2f} {r.correct:6d} {r.wrong:5d} {r.rejected:8d}")
    if best is None:
        print("\nKhông có ngưỡng nào tránh được nhận nhầm. Cần thêm/chọn lại ảnh mẫu.")
        return
    print(f"\nĐề xuất: accept_score: {best.threshold}  min_margin: {best.margin}")
    print(f"  -> nhận đúng {best.correct}/{best.total} ảnh ({best.correct / best.total:.0%}), nhận nhầm 0, "
          f"từ chối {best.rejected}.")
    print("  Đây là kết quả trên từng ảnh đơn lẻ. Khi chạy thật, hệ thống cần nhiều khung hình cùng"
          " đồng ý nên tỉ lệ nhận đúng mỗi lượt đi qua sẽ cao hơn.")
    print("  Nên cộng thêm ~0.02 vào accept_score để có biên an toàn.")
