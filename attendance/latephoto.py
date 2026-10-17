"""One picture of everyone who came late, for the daily late message.

The camera keeps a photo of the whole person for every sighting (store.body_photo). This module
tiles those of the day's late arrivals into a single picture, each with the name and the arrival
time burned in, so the message in Google Chat shows at a glance who it is about. The picture is
served without the admin password, under an address that holds an unguessable token, because
Google Chat fetches the image itself.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import math
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import numpy as np

FOLDER = "late"  # under the data folder: late/<day>.jpg
TILE_W, TILE_H = 360, 480  # every person is scaled to this height and cropped or padded to this width
LABEL_H = 48  # strip under each person: name, time
HEADER_H = 40
PER_ROW = 4
GAP = 8
BACKGROUND, TILE_BG, STRIP = (24, 24, 24), (56, 56, 56), (0, 0, 0)
WHITE, GREY = (255, 255, 255), (200, 200, 200)


@dataclass
class Person:
    name: str
    time: str  # HH:MM
    minutes: int  # late by
    photo: Path | None  # the whole person (or the face when that is all there is)


def photo_path(day: dt.date) -> str:
    return f"{FOLDER}/{day.isoformat()}.jpg"


def token(secret: str, day: dt.date) -> str:
    """The unguessable part of the picture's address: Google Chat fetches the picture without the
    admin password, while nobody can browse the pictures of other days."""
    return hmac.new(secret.encode(), day.isoformat().encode(), hashlib.sha256).hexdigest()[:32]


def ascii_label(text: str) -> str:
    """OpenCV only draws ASCII: drop the accents (Nguyễn Văn Đức -> Nguyen Van Duc)."""
    text = text.replace("Đ", "D").replace("đ", "d")
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().strip() or "?"


def _fit(text: str, width: int, size: float) -> str:
    """Shorten `text` with an ellipsis until it is at most `width` pixels wide at font `size`."""
    import cv2

    def wide(t: str) -> bool:
        return cv2.getTextSize(t, cv2.FONT_HERSHEY_SIMPLEX, size, 1)[0][0] > width

    if not wide(text):
        return text
    while len(text) > 1 and wide(text + ".."):
        text = text[:-1].rstrip()
    return text + ".."


def _tile(person: Person) -> np.ndarray:
    import cv2

    tile = np.full((TILE_H + LABEL_H, TILE_W, 3), TILE_BG, np.uint8)
    photo = cv2.imread(str(person.photo)) if person.photo and person.photo.exists() else None
    if photo is not None and photo.size:
        h, w = photo.shape[:2]
        resized = cv2.resize(photo, (max(1, round(w * TILE_H / h)), TILE_H), interpolation=cv2.INTER_AREA)
        if resized.shape[1] > TILE_W:  # wider than the tile: keep the middle
            x = (resized.shape[1] - TILE_W) // 2
            resized = resized[:, x:x + TILE_W]
        x = (TILE_W - resized.shape[1]) // 2
        tile[:TILE_H, x:x + resized.shape[1]] = resized
    else:
        text = "no photo"
        (tw, th), _ = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.8, 1)
        cv2.putText(tile, text, ((TILE_W - tw) // 2, (TILE_H + th) // 2), cv2.FONT_HERSHEY_SIMPLEX, 0.8, GREY, 1,
                    cv2.LINE_AA)
    tile[TILE_H:] = STRIP
    name = _fit(ascii_label(person.name), TILE_W - 16, 0.6)
    cv2.putText(tile, name, (8, TILE_H + 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, WHITE, 1, cv2.LINE_AA)
    cv2.putText(tile, f"{person.time}  ({person.minutes} min late)", (8, TILE_H + 40), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                GREY, 1, cv2.LINE_AA)
    return tile


def compose(people: list[Person], title: str) -> bytes:
    """The late people side by side (at most PER_ROW per row) under a title line, as JPEG."""
    import cv2

    from .faces import encode_jpeg

    if not people:
        raise ValueError("nobody to show")
    cols = min(len(people), PER_ROW)
    rows = math.ceil(len(people) / cols)
    width = cols * TILE_W + (cols + 1) * GAP
    height = HEADER_H + rows * (TILE_H + LABEL_H) + (rows + 1) * GAP
    canvas = np.full((height, width, 3), BACKGROUND, np.uint8)
    cv2.putText(canvas, _fit(ascii_label(title), width - 16, 0.7), (8, 27), cv2.FONT_HERSHEY_SIMPLEX, 0.7, WHITE, 1,
                cv2.LINE_AA)
    for i, person in enumerate(people):
        r, c = divmod(i, cols)
        y = HEADER_H + GAP + r * (TILE_H + LABEL_H + GAP)
        x = GAP + c * (TILE_W + GAP)
        canvas[y:y + TILE_H + LABEL_H, x:x + TILE_W] = _tile(person)
    return encode_jpeg(canvas)


def fingerprint(people: list[Person]) -> str:
    """Changes when the picture would: someone added or removed, a time corrected, a photo replaced."""
    parts = []
    for p in people:
        stat = p.photo.stat() if p.photo and p.photo.exists() else None
        parts.append((p.name, p.time, p.minutes, str(p.photo), stat.st_size if stat else 0, stat.st_mtime_ns if stat else 0))
    return hashlib.sha256(repr(parts).encode()).hexdigest()


def write(data_dir: Path, day: dt.date, people: list[Person], title: str) -> str:
    """Make (or refresh, when something changed) the day's picture; returns its path under data_dir."""
    rel = photo_path(day)
    path = data_dir / rel
    key = path.with_suffix(".key")
    fp = fingerprint(people)
    if path.exists() and key.exists() and key.read_text() == fp:
        return rel
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_bytes(compose(people, title))
    tmp.replace(path)  # readers (the web page, Google Chat) never see a half-written file
    key.write_text(fp)
    return rel


def remove(data_dir: Path, day: dt.date) -> bool:
    path = data_dir / photo_path(day)
    found = path.exists()
    for p in (path, path.with_suffix(".key"), path.with_suffix(".tmp")):
        p.unlink(missing_ok=True)
    return found


def expire(data_dir: Path, cutoff: dt.date) -> int:
    """Delete the pictures of days before `cutoff`."""
    folder = data_dir / FOLDER
    removed = 0
    if folder.exists():
        for p in folder.glob("*.jpg"):
            try:
                day = dt.date.fromisoformat(p.stem)
            except ValueError:
                continue
            if day < cutoff:
                removed += remove(data_dir, day)
    return removed
