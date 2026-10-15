"""Read an RTSP stream on a background thread, keeping only the newest frame.

Reading on a separate thread prevents the decoder buffer from filling up while a frame is
being analysed, which would otherwise make the picture lag further and further behind.
"""
from __future__ import annotations

import logging
import os
import threading
import time

# RTSP over TCP: slower to start than UDP, but no smeared frames on a weak Wi-Fi link.
os.environ.setdefault("OPENCV_FFMPEG_CAPTURE_OPTIONS", "rtsp_transport;tcp")

import cv2  # noqa: E402

log = logging.getLogger(__name__)

STALE_AFTER_S = 5.0


class Camera:
    def __init__(self, url: str):
        self.source = url
        self.url = int(url) if url.isdigit() else url
        self._started = time.time()
        self._frame = None
        self._frame_time = 0.0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, name="camera", daemon=True)

    def start(self) -> "Camera":
        self._thread.start()
        return self

    def stop(self, wait_s: float = 10.0) -> None:
        """Ask the reader to stop. A reader stuck in a hung decoder call cannot be interrupted;
        it is left behind (daemon thread) and ignored once `wait_s` is over."""
        self._stop.set()
        self._thread.join(timeout=wait_s)

    def started_for(self) -> float:
        return time.time() - self._started

    def silent_for(self) -> float:
        """Seconds since the last frame (or since start, if there never was one)."""
        with self._lock:
            return time.time() - (self._frame_time or self._started)

    def latest(self):
        """Return (frame, unix_time) of the newest frame, or (None, 0) if the stream is down."""
        with self._lock:
            if self._frame is None or time.time() - self._frame_time > STALE_AFTER_S:
                return None, 0.0
            return self._frame, self._frame_time

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            cap = cv2.VideoCapture(self.url, cv2.CAP_FFMPEG) if isinstance(self.url, str) else cv2.VideoCapture(self.url)
            if not cap.isOpened():
                log.warning("Cannot open camera, retrying in %.0fs", backoff)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 30.0)
                continue
            log.info("Camera connected")
            backoff = 1.0
            # A recorded video file is played at its real speed, so it can stand in for the camera when testing.
            is_file = isinstance(self.url, str) and "://" not in self.url
            frame_gap = 1.0 / (cap.get(cv2.CAP_PROP_FPS) or 25.0) if is_file else 0.0
            while not self._stop.is_set():
                if frame_gap:
                    time.sleep(frame_gap)
                ok, frame = cap.read()
                if not ok:
                    log.warning("Camera stream interrupted, reconnecting")
                    break
                with self._lock:
                    self._frame, self._frame_time = frame, time.time()
            cap.release()
