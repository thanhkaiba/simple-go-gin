"""Load and validate config.yaml."""
from __future__ import annotations

import dataclasses
import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml


def parse_clock(value) -> dt.time:
    """Parse "HH:MM". YAML 1.1 reads an unquoted 10:00 as the base-60 integer 600, so accept that too."""
    if isinstance(value, int):
        return dt.time(value // 60, value % 60)
    return dt.datetime.strptime(str(value).strip(), "%H:%M").time()


@dataclass
class ScheduleConfig:
    window_start: str = "09:00"  # camera starts processing
    late_after: str = "10:00"  # first seen at or after this time counts as late
    window_end: str = "11:00"  # camera stops processing, daily summary is sent
    work_days: list[int] = field(default_factory=lambda: [0, 1, 2, 3, 4])  # Monday = 0

    def clocks(self) -> tuple[dt.time, dt.time, dt.time]:
        return parse_clock(self.window_start), parse_clock(self.late_after), parse_clock(self.window_end)


@dataclass
class CameraConfig:
    url: str = ""  # rtsp://..., a video file, or a webcam index such as "0"
    process_fps: float = 5.0
    enhance_low_light: bool = True


DEFAULT_MODELS = {
    "insightface": ("models/det_10g.onnx", "models/w600k_r50.onnx"),
    "opencv": ("models/face_detection_yunet_2023mar.onnx", "models/face_recognition_sface_2021dec.onnx"),
}


@dataclass
class RecognitionConfig:
    backend: str = "insightface"  # or "opencv"; see recognizer.py for the accuracy/licence trade-off
    detector_model: str = ""  # empty: the backend's default file under models/
    recognizer_model: str = ""
    det_size: int = 640
    use_gpu: bool = False
    # Quality gate: frames failing it never vote, they can only end up as "unknown".
    min_face_px: int = 60
    min_det_score: float = 0.65
    min_sharpness: float = 40.0
    max_yaw: float = 35.0
    max_pitch: float = 30.0
    # Decision: tune accept_score and min_margin with `python -m attendance evaluate`.
    accept_score: float = 0.45
    min_margin: float = 0.08
    votes_required: int = 3
    track_timeout_s: float = 1.5
    unknown_min_frames: int = 3

    def model_files(self) -> tuple[str, str]:
        default_det, default_rec = DEFAULT_MODELS.get(self.backend, ("", ""))
        return self.detector_model or default_det, self.recognizer_model or default_rec

    def model_id(self) -> str:
        """Embeddings from different recognition models are not comparable, so they are stored per model."""
        return Path(self.model_files()[1]).stem


@dataclass
class GoogleChatConfig:
    webhook_url: str = ""
    notify_late_arrivals: bool = True
    unknown_batch_minutes: int = 5


@dataclass
class LeaveApiConfig:
    """HR system API that provides holidays, extra work days and employee leave (see leave_api.py)."""
    url: str = ""  # empty: no leave data, only work_days decide who is expected
    token: str = ""  # sent as "Authorization: Bearer <token>"
    refresh_minutes: int = 15
    days_back: int = 7  # range fetched around today
    days_ahead: int = 60


@dataclass
class WebConfig:
    enabled: bool = True
    host: str = "0.0.0.0"
    port: int = 8000
    public_url: str = "http://localhost:8000"  # link put into Google Chat messages
    username: str = "admin"
    password: str = ""  # empty disables login


@dataclass
class Config:
    timezone: str = "Asia/Ho_Chi_Minh"
    data_dir: str = "data"
    employees_file: str = "employees.csv"  # imported once, when the database has no employees yet
    healthcheck_url: str = ""
    retention_days: int = 90  # face photos of arrivals and unknown faces are deleted after this
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    camera: CameraConfig = field(default_factory=CameraConfig)
    recognition: RecognitionConfig = field(default_factory=RecognitionConfig)
    google_chat: GoogleChatConfig = field(default_factory=GoogleChatConfig)
    leave_api: LeaveApiConfig = field(default_factory=LeaveApiConfig)
    web: WebConfig = field(default_factory=WebConfig)
    base_dir: Path = field(default=Path("."), repr=False)

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)

    def path(self, p: str) -> Path:
        path = Path(p)
        return path if path.is_absolute() else self.base_dir / path

    def validate(self) -> None:
        start, late, end = self.schedule.clocks()
        # Store times as "HH:MM" even if YAML read them as numbers.
        self.schedule.window_start, self.schedule.late_after, self.schedule.window_end = (
            t.strftime("%H:%M") for t in (start, late, end))
        if not start < late <= end:
            raise ValueError("schedule must satisfy window_start < late_after <= window_end")
        if self.recognition.backend not in DEFAULT_MODELS:
            raise ValueError(f"recognition.backend must be one of {sorted(DEFAULT_MODELS)}")
        if any(d not in range(7) for d in self.schedule.work_days):
            raise ValueError("work_days must contain numbers 0 (Monday) to 6 (Sunday)")
        r = self.recognition
        if not (0 < r.accept_score < 1 and 0 <= r.min_margin < 1 and 0 <= r.min_det_score <= 1):
            raise ValueError("accept_score, min_margin and min_det_score must be between 0 and 1")
        if r.votes_required < 1 or r.unknown_min_frames < 1 or r.min_face_px < 1:
            raise ValueError("votes_required, unknown_min_frames and min_face_px must be at least 1")
        if not 0 < self.camera.process_fps <= 30:
            raise ValueError("process_fps must be between 0 and 30")
        if self.retention_days < 1:
            raise ValueError("retention_days must be at least 1")
        if self.leave_api.refresh_minutes < 1:
            raise ValueError("leave_api.refresh_minutes must be at least 1")
        if self.google_chat.unknown_batch_minutes < 0:
            raise ValueError("unknown_batch_minutes must not be negative")
        self.tz  # raises on an unknown time zone


def _build(cls, data: dict):
    if not isinstance(data, dict):
        raise ValueError(f"expected a mapping for {cls.__name__}")
    known = {f.name: f for f in dataclasses.fields(cls)}
    unknown = set(data) - set(known)
    if unknown:
        raise ValueError(f"unknown config keys for {cls.__name__}: {sorted(unknown)}")
    kwargs = {}
    for name, value in data.items():
        sub = known[name].default_factory if known[name].default_factory is not dataclasses.MISSING else None
        if sub is not None and dataclasses.is_dataclass(sub):
            kwargs[name] = _build(sub, value or {})
        else:
            kwargs[name] = value
    return cls(**kwargs)


def load(path: str | Path) -> Config:
    path = Path(path)
    with path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    cfg = _build(Config, data)
    cfg.base_dir = path.resolve().parent
    cfg.validate()
    return cfg
