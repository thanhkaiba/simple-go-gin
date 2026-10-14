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


@dataclass
class RecognitionConfig:
    model: str = "buffalo_l"
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


@dataclass
class GoogleChatConfig:
    webhook_url: str = ""
    notify_late_arrivals: bool = True
    unknown_batch_minutes: int = 5


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
    employees_file: str = "employees.csv"
    healthcheck_url: str = ""
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    camera: CameraConfig = field(default_factory=CameraConfig)
    recognition: RecognitionConfig = field(default_factory=RecognitionConfig)
    google_chat: GoogleChatConfig = field(default_factory=GoogleChatConfig)
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
        if not start < late <= end:
            raise ValueError("schedule must satisfy window_start < late_after <= window_end")
        if any(d not in range(7) for d in self.schedule.work_days):
            raise ValueError("work_days must contain numbers 0 (Monday) to 6 (Sunday)")
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
