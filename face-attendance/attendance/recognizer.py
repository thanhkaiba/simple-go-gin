"""Face detection, alignment and embedding with InsightFace (SCRFD detector + ArcFace).

FaceAnalysis aligns every face on its 5 landmarks before computing the embedding,
which matters far more for accuracy than cropping faces with a generic detector.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import cv2
import numpy as np

from .config import RecognitionConfig


@dataclass
class DetectedFace:
    bbox: tuple[float, float, float, float]
    embedding: np.ndarray  # L2-normalised
    det_score: float
    size: float  # shorter side of the box, in pixels
    sharpness: float
    yaw: float
    pitch: float
    quality_ok: bool
    quality: float


def enhance_low_light(frame: np.ndarray, threshold: float = 90.0) -> np.ndarray:
    """Equalise contrast (CLAHE on the L channel) when the frame is dark."""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    if gray.mean() >= threshold:
        return frame
    lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    l = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(l)
    return cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2BGR)


def sharpness(frame: np.ndarray, bbox) -> float:
    """Variance of the Laplacian on the face, resized to a fixed width so it is comparable across sizes."""
    x1, y1, x2, y2 = (int(round(v)) for v in bbox)
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = max(0, x1), max(0, y1), min(w, x2), min(h, y2)
    if x2 - x1 < 8 or y2 - y1 < 8:
        return 0.0
    face = cv2.cvtColor(frame[y1:y2, x1:x2], cv2.COLOR_BGR2GRAY)
    face = cv2.resize(face, (112, int(112 * (y2 - y1) / (x2 - x1))))
    return float(cv2.Laplacian(face, cv2.CV_64F).var())


def crop_face(frame: np.ndarray, bbox, margin: float = 0.4) -> np.ndarray:
    x1, y1, x2, y2 = bbox
    mw, mh = (x2 - x1) * margin, (y2 - y1) * margin
    h, w = frame.shape[:2]
    x1, y1 = int(max(0, x1 - mw)), int(max(0, y1 - mh))
    x2, y2 = int(min(w, x2 + mw)), int(min(h, y2 + mh))
    return frame[y1:y2, x1:x2].copy()


class FaceRecognizer:
    def __init__(self, cfg: RecognitionConfig):
        from insightface.app import FaceAnalysis

        providers = ["CUDAExecutionProvider", "CPUExecutionProvider"] if cfg.use_gpu else ["CPUExecutionProvider"]
        self.cfg = cfg
        # landmark_3d_68 provides head pose; gender/age and 2d landmarks are not needed.
        self.app = FaceAnalysis(name=cfg.model, providers=providers,
                                allowed_modules=["detection", "recognition", "landmark_3d_68"])
        self.app.prepare(ctx_id=0 if cfg.use_gpu else -1, det_size=(cfg.det_size, cfg.det_size))

    def analyze(self, frame: np.ndarray) -> list[DetectedFace]:
        out = []
        for f in self.app.get(frame):
            bbox = tuple(float(v) for v in f.bbox)
            size = min(bbox[2] - bbox[0], bbox[3] - bbox[1])
            pose = f.get("pose")  # pitch, yaw, roll in degrees
            pitch, yaw = (float(pose[0]), float(pose[1])) if pose is not None else (0.0, 0.0)
            sharp = sharpness(frame, bbox)
            det = float(f.det_score)
            c = self.cfg
            ok = (size >= c.min_face_px and det >= c.min_det_score and sharp >= c.min_sharpness
                  and abs(yaw) <= c.max_yaw and abs(pitch) <= c.max_pitch)
            quality = (det * min(1.0, size / 112) * min(1.0, sharp / 150)
                       * math.cos(math.radians(min(abs(yaw), 89))) * math.cos(math.radians(min(abs(pitch), 89))))
            out.append(DetectedFace(bbox, f.normed_embedding.astype(np.float32), det, size, sharp,
                                    yaw, pitch, ok, quality))
        return out

    def largest_face(self, image: np.ndarray) -> DetectedFace | None:
        faces = self.analyze(image)
        return max(faces, key=lambda f: f.size) if faces else None
