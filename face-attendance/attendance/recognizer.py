"""Face detection, alignment and embedding.

Two interchangeable backends, compared on your own photos with `python -m attendance evaluate`:

- "insightface": SCRFD-10G detector + ArcFace ResNet50 (WebFace600K), run directly with
  onnxruntime. Most accurate, but the pretrained weights are licensed for non-commercial
  research only; commercial use needs a licence from InsightFace.
- "opencv": YuNet detector (MIT) + SFace recogniser (Apache 2.0), built into OpenCV.
  Less accurate, but free for commercial use and has no extra dependencies.

Both align every face on its 5 landmarks before computing the embedding.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .config import RecognitionConfig

# ArcFace reference positions of the eyes, nose tip and mouth corners in a 112x112 crop.
ARCFACE_LANDMARKS = np.array(
    [[38.2946, 51.6963], [73.5318, 51.5014], [56.0252, 71.7366], [41.5493, 92.3655], [70.7299, 92.2041]],
    dtype=np.float32,
)


@dataclass
class DetectedFace:
    bbox: tuple[float, float, float, float]
    embedding: np.ndarray  # L2-normalised
    det_score: float
    size: float  # shorter side of the box, in pixels
    sharpness: float
    yaw: float  # degrees, estimated from the landmarks
    pitch: float  # degrees, rough estimate from the landmarks
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


def head_pose(landmarks: np.ndarray) -> tuple[float, float]:
    """Approximate (yaw, pitch) in degrees from eyes, nose tip and mouth corners.

    When the head turns, the nose tip moves sideways relative to the eyes by roughly
    0.55 * eye distance * tan(yaw). Pitch uses where the nose sits between eyes and mouth.
    """
    eyes, nose, mouth = landmarks[:2], landmarks[2], landmarks[3:5]
    eye_mid, mouth_mid = eyes.mean(axis=0), mouth.mean(axis=0)
    eye_dist = float(np.linalg.norm(eyes[1] - eyes[0]))
    face_height = float(mouth_mid[1] - eye_mid[1])
    if eye_dist < 1 or face_height < 1:
        return 90.0, 90.0
    yaw = math.degrees(math.atan(abs(nose[0] - eye_mid[0]) / eye_dist / 0.55))
    pitch = (float(nose[1] - eye_mid[1]) / face_height - 0.55) * 150
    return yaw, pitch


def align(frame: np.ndarray, landmarks: np.ndarray) -> np.ndarray:
    matrix, _ = cv2.estimateAffinePartial2D(landmarks.astype(np.float32), ARCFACE_LANDMARKS, method=cv2.LMEDS)
    if matrix is None:  # degenerate landmarks (e.g. all on one point): no similarity transform exists
        raise ValueError("cannot align face: degenerate landmarks")
    return cv2.warpAffine(frame, matrix, (112, 112), borderValue=0)


def crop_face(frame: np.ndarray, bbox, margin: float = 0.4) -> np.ndarray:
    x1, y1, x2, y2 = bbox
    mw, mh = (x2 - x1) * margin, (y2 - y1) * margin
    h, w = frame.shape[:2]
    x1, y1 = int(max(0, x1 - mw)), int(max(0, y1 - mh))
    x2, y2 = int(min(w, x2 + mw)), int(min(h, y2 + mh))
    return frame[y1:y2, x1:x2].copy()


def nms(boxes: np.ndarray, scores: np.ndarray, threshold: float) -> list[int]:
    order = scores.argsort()[::-1]
    areas = (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1])
    keep = []
    while order.size:
        i = order[0]
        keep.append(int(i))
        xx1 = np.maximum(boxes[i, 0], boxes[order[1:], 0])
        yy1 = np.maximum(boxes[i, 1], boxes[order[1:], 1])
        xx2 = np.minimum(boxes[i, 2], boxes[order[1:], 2])
        yy2 = np.minimum(boxes[i, 3], boxes[order[1:], 3])
        inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
        overlap = inter / (areas[i] + areas[order[1:]] - inter)
        order = order[1:][overlap <= threshold]
    return keep


class InsightFaceBackend:
    """SCRFD + ArcFace ONNX models, decoded here so the `insightface` package is not needed."""

    STRIDES = (8, 16, 32)
    ANCHORS = 2

    def __init__(self, cfg: RecognitionConfig, detector: Path, recognizer: Path):
        import onnxruntime as ort

        providers = ["CUDAExecutionProvider", "CPUExecutionProvider"] if cfg.use_gpu else ["CPUExecutionProvider"]
        self.det = ort.InferenceSession(str(detector), providers=providers)
        self.rec = ort.InferenceSession(str(recognizer), providers=providers)
        self.det_size = cfg.det_size
        self._centers: dict[tuple[int, int, int], np.ndarray] = {}

    def detect(self, frame: np.ndarray, threshold: float = 0.5) -> list[tuple[np.ndarray, float, np.ndarray]]:
        h, w = frame.shape[:2]
        scale = self.det_size / max(h, w)
        resized = cv2.resize(frame, (int(w * scale), int(h * scale)))
        canvas = np.zeros((self.det_size, self.det_size, 3), dtype=np.uint8)
        canvas[: resized.shape[0], : resized.shape[1]] = resized
        blob = cv2.dnn.blobFromImage(canvas, 1 / 128, (self.det_size, self.det_size), (127.5, 127.5, 127.5), swapRB=True)
        outs = self.det.run(None, {self.det.get_inputs()[0].name: blob})

        boxes, scores, kpss = [], [], []
        n = len(self.STRIDES)
        for i, stride in enumerate(self.STRIDES):
            score = outs[i].reshape(-1)
            dist = outs[i + n].reshape(-1, 4) * stride
            kps = outs[i + 2 * n].reshape(-1, 10) * stride
            centers = self._anchor_centers(stride)
            keep = score >= threshold
            c, d, k = centers[keep], dist[keep], kps[keep]
            boxes.append(np.hstack([c - d[:, :2], c + d[:, 2:]]))
            kpss.append(k.reshape(-1, 5, 2) + c[:, None, :])
            scores.append(score[keep])
        boxes, scores, kpss = np.vstack(boxes) / scale, np.concatenate(scores), np.vstack(kpss) / scale
        return [(boxes[i], float(scores[i]), kpss[i]) for i in nms(boxes, scores, 0.4)]

    def _anchor_centers(self, stride: int) -> np.ndarray:
        key = (self.det_size, self.det_size, stride)
        if key not in self._centers:
            size = self.det_size // stride
            grid = np.stack(np.mgrid[:size, :size][::-1], axis=-1).astype(np.float32).reshape(-1, 2) * stride
            self._centers[key] = np.repeat(grid, self.ANCHORS, axis=0)
        return self._centers[key]

    def embed(self, frame: np.ndarray, landmarks: np.ndarray) -> np.ndarray:
        face = align(frame, landmarks)
        blob = cv2.dnn.blobFromImage(face, 1 / 127.5, (112, 112), (127.5, 127.5, 127.5), swapRB=True)
        return self.rec.run(None, {self.rec.get_inputs()[0].name: blob})[0][0]


class OpenCVBackend:
    """YuNet + SFace through OpenCV's FaceDetectorYN / FaceRecognizerSF."""

    def __init__(self, cfg: RecognitionConfig, detector: Path, recognizer: Path):
        self.det = cv2.FaceDetectorYN.create(str(detector), "", (320, 320), 0.5, 0.3, 5000)
        self.rec = cv2.FaceRecognizerSF.create(str(recognizer), "")
        self.max_side = cfg.det_size

    def detect(self, frame: np.ndarray, threshold: float = 0.5) -> list[tuple[np.ndarray, float, np.ndarray]]:
        h, w = frame.shape[:2]
        scale = min(1.0, self.max_side / max(h, w))
        small = cv2.resize(frame, (int(w * scale), int(h * scale))) if scale < 1 else frame
        self.det.setInputSize((small.shape[1], small.shape[0]))
        self.det.setScoreThreshold(threshold)
        _, faces = self.det.detect(small)
        out = []
        for f in faces if faces is not None else []:
            x, y, bw, bh = f[:4] / scale
            out.append((np.array([x, y, x + bw, y + bh]), float(f[14]), f[4:14].reshape(5, 2) / scale))
        return out

    def embed(self, frame: np.ndarray, landmarks: np.ndarray) -> np.ndarray:
        # Same 5-point alignment as the other backend; SFace expects a 112x112 aligned face.
        return self.rec.feature(align(frame, landmarks))[0]


BACKENDS = {"insightface": InsightFaceBackend, "opencv": OpenCVBackend}


class FaceRecognizer:
    def __init__(self, cfg: RecognitionConfig, base_dir: Path = Path(".")):
        if cfg.backend not in BACKENDS:
            raise ValueError(f"recognition.backend must be one of {sorted(BACKENDS)}")
        detector, recognizer = (base_dir / p for p in cfg.model_files())
        for path in (detector, recognizer):
            if not path.exists():
                raise FileNotFoundError(f"{path} not found; run: python -m attendance download-models")
        self.cfg = cfg
        self.model_id = cfg.model_id()
        self.backend = BACKENDS[cfg.backend](cfg, detector, recognizer)

    def analyze(self, frame: np.ndarray) -> list[DetectedFace]:
        c = self.cfg
        out = []
        for box, det, landmarks in self.backend.detect(frame):
            bbox = tuple(float(v) for v in box)
            size = min(bbox[2] - bbox[0], bbox[3] - bbox[1])
            yaw, pitch = head_pose(landmarks)
            sharp = sharpness(frame, bbox)
            ok = (size >= c.min_face_px and det >= c.min_det_score and sharp >= c.min_sharpness
                  and yaw <= c.max_yaw and abs(pitch) <= c.max_pitch)
            quality = (det * min(1.0, size / 112) * min(1.0, sharp / 150)
                       * math.cos(math.radians(min(yaw, 89))) * math.cos(math.radians(min(abs(pitch), 89))))
            try:
                emb = self.backend.embed(frame, landmarks).astype(np.float32)
            except ValueError:  # unusable landmarks: skip this face rather than stop the camera loop
                continue

            emb /= max(float(np.linalg.norm(emb)), 1e-12)
            out.append(DetectedFace(bbox, emb, det, size, sharp, yaw, pitch, ok, quality))
        return out

    def largest_face(self, image: np.ndarray) -> DetectedFace | None:
        faces = self.analyze(image)
        return max(faces, key=lambda f: f.size) if faces else None
