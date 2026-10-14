"""Main loop: camera -> faces -> tracker -> attendance service."""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time
import urllib.request
from pathlib import Path

import cv2

from .camera import Camera
from .config import Config
from .gallery import Gallery
from .notifier import GoogleChat
from .recognizer import FaceRecognizer, crop_face, enhance_low_light
from .service import AttendanceService
from .store import Store
from .tracker import Decision, FaceTracker, Observation

log = logging.getLogger(__name__)


def open_app(cfg: Config) -> tuple[Store, Gallery, AttendanceService]:
    data = cfg.path(cfg.data_dir)
    store = Store(data / "attendance.db")
    count = store.sync_employees(cfg.path(cfg.employees_file))
    gallery = Gallery(store.embeddings())
    missing = [e for e in store.employees() if e not in gallery.counts()]
    log.info("%d employees, %d with enrolled faces", count, count - len(missing))
    if missing:
        log.warning("No face enrolled for: %s", ", ".join(missing))
    service = AttendanceService(cfg, store, GoogleChat(cfg.google_chat.webhook_url, store), gallery)
    return store, gallery, service


def save_crop(cfg: Config, decision: Decision, ts: dt.datetime) -> str | None:
    if decision.crop is None:
        return None
    rel = Path(ts.strftime("%Y-%m-%d")) / f"{ts:%H%M%S_%f}_{decision.employee_id if decision.kind == 'identified' else 'unknown'}.jpg"
    path = cfg.path(cfg.data_dir) / "crops" / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(path), decision.crop)
    return rel.as_posix()


def _healthcheck(url: str) -> None:
    try:
        urllib.request.urlopen(url, timeout=10).read()
    except Exception as exc:
        log.warning("Healthcheck ping failed: %s", exc)


def run(cfg: Config) -> None:
    store, gallery, service = open_app(cfg)
    if cfg.web.enabled:
        from .web import create_app

        app = create_app(cfg, store, service)
        threading.Thread(target=lambda: app.run(host=cfg.web.host, port=cfg.web.port, threaded=True),
                         name="web", daemon=True).start()
        log.info("Admin page on %s", cfg.web.public_url)

    recognizer = FaceRecognizer(cfg.recognition)
    rc = cfg.recognition
    tracker = FaceTracker(rc.accept_score, rc.min_margin, rc.votes_required, rc.track_timeout_s, rc.unknown_min_frames)
    camera: Camera | None = None
    last_processed = 0.0
    last_ping = 0.0
    last_tick = 0.0
    interval = 1.0 / max(cfg.camera.process_fps, 0.1)

    def handle(decisions: list[Decision]) -> None:
        for d in decisions:
            ts = d.ts.astimezone(service.tz)
            image = save_crop(cfg, d, ts)
            if d.kind == "identified":
                service.on_identified(d.employee_id, ts, d.score, image)
            else:
                service.on_unknown(ts, image, d.embedding, d.employee_id, d.score)

    log.info("Running; processing window %s-%s", cfg.schedule.window_start, cfg.schedule.window_end)
    while True:
        now = service.now()
        camera_ok = True
        if service.in_window(now):
            if camera is None:
                camera = Camera(cfg.camera.url).start()
            frame, frame_time = camera.latest()
            camera_ok = frame is not None
            if frame is not None and frame_time - last_processed >= interval:
                last_processed = frame_time
                ts = dt.datetime.fromtimestamp(frame_time, service.tz)
                if cfg.camera.enhance_low_light:
                    frame = enhance_low_light(frame)
                observations = [
                    Observation(f.bbox, f.embedding, gallery.match(f.embedding), f.quality_ok, f.quality,
                                crop_face(frame, f.bbox))
                    for f in recognizer.analyze(frame)
                ]
                handle(tracker.update(observations, ts))
            else:
                time.sleep(0.02)
        else:
            if camera is not None:
                camera.stop()
                camera = None
                handle(tracker.flush())
            time.sleep(1.0)

        if time.time() - last_tick >= 1.0:
            last_tick = time.time()
            service.tick(camera_ok)
        if cfg.healthcheck_url and time.time() - last_ping >= 60:
            last_ping = time.time()
            threading.Thread(target=_healthcheck, args=(cfg.healthcheck_url,), daemon=True).start()
