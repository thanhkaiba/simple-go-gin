"""Enroll reference photos: faces/<employee_id>/*.jpg -> embeddings in the database."""
from __future__ import annotations

import datetime as dt
from pathlib import Path

import cv2
import numpy as np

from .config import Config
from .recognizer import FaceRecognizer
from .store import Store

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".webp"}
MIN_PHOTOS = 5


def read_image(path: Path):
    # cv2.imread cannot open non-ASCII paths on Windows (e.g. Vietnamese folder names).
    data = np.fromfile(str(path), dtype=np.uint8)
    return cv2.imdecode(data, cv2.IMREAD_COLOR) if data.size else None


def images_in(folder: Path) -> list[Path]:
    return sorted(p for p in folder.iterdir() if p.suffix.lower() in IMAGE_EXTS)


def enroll(cfg: Config, faces_dir: Path) -> None:
    store = Store(cfg.path(cfg.data_dir) / "attendance.db")
    store.sync_employees(cfg.path(cfg.employees_file))
    employees = store.employees()
    recognizer = FaceRecognizer(cfg.recognition)
    now = dt.datetime.now(cfg.tz)

    seen = set()
    for person_dir in sorted(p for p in faces_dir.iterdir() if p.is_dir()):
        emp = person_dir.name
        if emp not in employees:
            print(f"! {emp}: không có trong {cfg.employees_file}, bỏ qua")
            continue
        seen.add(emp)
        # Re-running replaces the enrolled photos; faces learned from admin confirmations are kept.
        store.delete_embeddings(emp, "enroll:")
        added = 0
        for path in images_in(person_dir):
            image = read_image(path)
            face = recognizer.largest_face(image) if image is not None else None
            if face is None or face.det_score < 0.5 or face.size < 40:
                print(f"  - {path.name}: không tìm thấy khuôn mặt rõ, bỏ qua")
                continue
            store.add_embedding(emp, face.embedding, f"enroll:{path.name}", now)
            added += 1
        warn = f"  ⚠ nên có ít nhất {MIN_PHOTOS} ảnh" if added < MIN_PHOTOS else ""
        print(f"{emp} ({employees[emp]}): {added} ảnh{warn}")

    for emp in sorted(set(employees) - seen):
        print(f"! {emp} ({employees[emp]}): chưa có thư mục ảnh, hệ thống sẽ không nhận ra người này")
