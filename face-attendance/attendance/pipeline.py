"""Application wiring and the main loop: camera -> faces -> tracker -> attendance service."""
from __future__ import annotations

import datetime as dt
import logging
import threading
import time
import urllib.request
from pathlib import Path

from . import settings
from .config import Config
from .faces import FaceLibrary
from .gallery import Gallery
from .live import FrameHealth, LiveStatus
from .notifier import GoogleChat
from .service import AttendanceService
from .store import Store
from .tracker import Decision, FaceTracker, Observation

log = logging.getLogger(__name__)


class App:
    """Everything the camera loop, the web page and the command line share."""

    def __init__(self, cfg: Config, now=None):
        self.cfg = cfg
        self.data_dir = cfg.path(cfg.data_dir)
        self.store = Store(self.data_dir / "attendance.db")
        settings.load(cfg, self.store)
        employees_file = cfg.path(cfg.employees_file)
        if self.store.employee_count() == 0 and employees_file.exists():
            n = self.store.import_employees(employees_file.read_text(encoding="utf-8-sig"))
            log.info("Imported %d employees from %s", n, employees_file)
        self.gallery = Gallery(self.store.embeddings(cfg.recognition.model_id()))
        self.chat = GoogleChat(lambda: cfg.google_chat.webhook_url, self.store)
        self.service = AttendanceService(cfg, self.store, self.chat, self.gallery, now=now)
        self.recognizer_lock = threading.Lock()
        self._recognizer = None
        self.library = FaceLibrary(cfg, self.store, self.gallery, self.recognizer, self.recognizer_lock,
                                   alert=self.service.alert, now=self.service.now)
        self.service.library = self.library
        self.live = LiveStatus()

    def recognizer(self):
        if self._recognizer is None:
            from .recognizer import FaceRecognizer

            self._recognizer = FaceRecognizer(self.cfg.recognition, self.cfg.base_dir)
        return self._recognizer


def save_crop(app: App, decision: Decision, ts: dt.datetime) -> str | None:
    if decision.crop is None:
        return None
    from .faces import encode_jpeg

    who = decision.employee_id if decision.kind == "identified" else "unknown"
    rel = Path("crops") / ts.strftime("%Y-%m-%d") / f"{ts:%H%M%S_%f}_{who}.jpg"
    path = app.data_dir / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(encode_jpeg(decision.crop))
    return rel.as_posix()


def _healthcheck(url: str) -> None:
    try:
        urllib.request.urlopen(url, timeout=10).read()
    except Exception as exc:
        log.warning("Healthcheck ping failed: %s", exc)


def face_label(app: App, face, candidates) -> tuple[str, tuple[int, int, int]]:
    """Text and BGR colour drawn on the live view for one face."""
    rc = app.cfg.recognition
    names = app.store.employees()
    if candidates:
        person, top = candidates[0]
        runner_up = candidates[1][1] if len(candidates) > 1 else -1.0
        if face.quality_ok and top >= rc.accept_score and top - runner_up >= rc.min_margin:
            return f"{names.get(person, person)} {top:.2f}", (60, 180, 60)
    if face.size < rc.min_face_px:
        reason = "too small"
    elif face.sharpness < rc.min_sharpness:
        reason = "blurred"
    elif face.yaw > rc.max_yaw or abs(face.pitch) > rc.max_pitch:
        reason = "turned away"
    elif face.det_score < rc.min_det_score:
        reason = "unclear"
    else:
        best = f" (closest {names.get(candidates[0][0], candidates[0][0])} {candidates[0][1]:.2f})" if candidates else ""
        return f"no match{best}", (40, 40, 220)
    return f"? {reason}", (0, 165, 255)


def draw(frame, labelled) -> bytes:
    import cv2

    from .faces import encode_jpeg

    canvas = frame.copy()
    for face, (text, colour) in labelled:
        x1, y1, x2, y2 = (int(v) for v in face.bbox)
        cv2.rectangle(canvas, (x1, y1), (x2, y2), colour, 2)
        cv2.putText(canvas, text, (x1, max(18, y1 - 8)), cv2.FONT_HERSHEY_SIMPLEX, 0.6, colour, 2)
    h, w = canvas.shape[:2]
    if w > 960:
        canvas = cv2.resize(canvas, (960, int(h * 960 / w)))
    return encode_jpeg(canvas)


def run(cfg: Config) -> None:
    from .camera import Camera
    from .recognizer import crop_face, enhance_low_light

    app = App(cfg)
    service, live = app.service, app.live
    if cfg.web.enabled:
        from .web import create_app

        web = create_app(app)
        threading.Thread(target=lambda: web.run(host=cfg.web.host, port=cfg.web.port, threaded=True),
                         name="web", daemon=True).start()
        log.info("Admin page on %s", cfg.web.public_url)

    recognizer = None
    try:
        recognizer = app.recognizer()
        app.library.reembed_missing()
    except Exception as exc:
        live.problem = f"Face models could not be loaded: {exc}"
        log.error(live.problem)

    missing = [e for e in app.store.employees() if e not in app.gallery.counts()]
    if missing:
        log.warning("No reference photos for: %s", ", ".join(missing))

    tracker = FaceTracker(cfg.recognition)
    health = FrameHealth()
    camera: Camera | None = None
    last_processed = last_ping = last_tick = 0.0
    fps_count, fps_since = 0, time.time()
    was_in_window = False

    def handle(decisions: list[Decision]) -> None:
        for d in decisions:
            ts = d.ts.astimezone(service.tz)
            image = save_crop(app, d, ts)
            if d.kind == "identified":
                service.on_identified(d.employee_id, ts, d.score, image)
                live.last_identified = f"{app.store.employees().get(d.employee_id, d.employee_id)} at {ts:%H:%M:%S}"
            else:
                service.on_unknown(ts, image, d.embedding, d.employee_id, d.score)

    log.info("Running; processing window %s-%s", cfg.schedule.window_start, cfg.schedule.window_end)
    while True:
        now = service.now()
        in_window = service.in_window(now)
        if was_in_window and not in_window:
            handle(tracker.flush())
        was_in_window = in_window
        camera_ok = True

        if (in_window or live.previewing) and recognizer is not None and cfg.camera.url:
            if camera is None or camera.source != cfg.camera.url:
                if camera is not None:
                    camera.stop()
                camera = Camera(cfg.camera.url).start()
                live.camera_state = "connecting"
            frame, frame_time = camera.latest()
            camera_ok = frame is not None
            if frame is None:
                live.camera_state = "no picture (reconnecting)" if camera.started_for() > 10 else "connecting"
                time.sleep(0.05)
            elif frame_time - last_processed >= 1.0 / cfg.camera.process_fps:
                last_processed = frame_time
                problem = health.update(frame, frame_time)
                camera_ok = problem is None
                live.camera_state = f"problem: {problem}" if problem else "ok"
                if cfg.camera.enhance_low_light:
                    frame = enhance_low_light(frame)
                with app.recognizer_lock:
                    faces = recognizer.analyze(frame)
                pairs = [(f, app.gallery.match(f.embedding)) for f in faces]
                if in_window:
                    observations = [Observation(f.bbox, f.embedding, c, f.quality_ok, f.quality, crop_face(frame, f.bbox))
                                    for f, c in pairs]
                    handle(tracker.update(observations, dt.datetime.fromtimestamp(frame_time, service.tz)))
                labelled = [(f, face_label(app, f, c)) for f, c in pairs]
                live.faces = [text for _, (text, _) in labelled]
                if live.wants_frame():
                    live.set_frame(draw(frame, labelled))
                fps_count += 1
            else:
                time.sleep(0.02)
        else:
            if camera is not None:
                camera.stop()
                camera = None
            live.camera_state = ("off (outside the camera hours)" if cfg.camera.url
                                 else "off (no camera address in Settings)")
            if in_window:
                camera_ok = False  # should be working but cannot: count it as an outage
            time.sleep(0.5)

        if time.time() - fps_since >= 5:
            live.fps, fps_count, fps_since = fps_count / (time.time() - fps_since), 0, time.time()
        if time.time() - last_tick >= 1.0:
            last_tick = time.time()
            service.tick(camera_ok)
        if cfg.healthcheck_url and time.time() - last_ping >= 60:
            last_ping = time.time()
            threading.Thread(target=_healthcheck, args=(cfg.healthcheck_url,), daemon=True).start()
