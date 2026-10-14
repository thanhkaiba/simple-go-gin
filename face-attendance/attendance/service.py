"""Attendance rules and scheduled Google Chat notices.

Rules for a work day (times from config.schedule):
- First seen before late_after (e.g. 10:00): on time. Being seen again later changes nothing.
- First seen at or after late_after: late, and announced as soon as they are seen.
- At late_after: one alert listing everyone not seen yet.
- At window_end (e.g. 11:00): daily summary.
- Faces the system could not identify are reported to the admin with a link to review them.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

from .config import Config
from .gallery import Gallery
from .notifier import GoogleChat
from .store import Store

log = logging.getLogger(__name__)

GAP = dt.timedelta(seconds=60)  # no frames for longer than this counts as an outage
ALIVE_SAVE_EVERY = dt.timedelta(seconds=10)
FLUSH_EVERY = dt.timedelta(seconds=30)


@dataclass
class Window:
    start: dt.datetime
    late_after: dt.datetime
    end: dt.datetime


@dataclass
class Arrival:
    employee_id: str
    name: str
    ts: dt.datetime
    source: str
    image: str | None

    @property
    def time(self) -> str:
        return self.ts.strftime("%H:%M")


@dataclass
class DayReport:
    day: dt.date
    workday: bool
    window: Window
    on_time: list[Arrival] = field(default_factory=list)
    late: list[Arrival] = field(default_factory=list)
    not_seen: list[tuple[str, str]] = field(default_factory=list)  # (id, name)
    pending_unknowns: int = 0
    outages: list[tuple[dt.datetime, dt.datetime]] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.on_time) + len(self.late) + len(self.not_seen)


def minutes_late(a: Arrival, w: Window) -> int:
    return int((a.ts - w.late_after).total_seconds() // 60)


class AttendanceService:
    def __init__(self, cfg: Config, store: Store, chat: GoogleChat, gallery: Gallery,
                 now: Callable[[], dt.datetime] | None = None):
        self.cfg = cfg
        self.tz = cfg.tz
        self.store = store
        self.chat = chat
        self.gallery = gallery
        self._now = now or (lambda: dt.datetime.now(self.tz))
        saved = store.get("last_alive")
        self._last_alive = dt.datetime.fromisoformat(saved) if saved else None
        self._last_alive_saved = self._last_alive
        self._last_unknown_notice: dt.datetime | None = None
        self._last_flush: dt.datetime | None = None

    # time helpers ------------------------------------------------------------

    def now(self) -> dt.datetime:
        return self._now()

    def window(self, day: dt.date) -> Window:
        start, late, end = self.cfg.schedule.clocks()
        return Window(*(dt.datetime.combine(day, t, tzinfo=self.tz) for t in (start, late, end)))

    def is_workday(self, day: dt.date) -> bool:
        return day.weekday() in self.cfg.schedule.work_days

    def in_window(self, now: dt.datetime) -> bool:
        w = self.window(now.date())
        return self.is_workday(now.date()) and w.start <= now < w.end

    def _local(self, ts: dt.datetime) -> dt.datetime:
        return ts.astimezone(self.tz)

    def _link(self, path: str = "") -> str:
        return self.cfg.web.public_url.rstrip("/") + path

    def _send(self, text: str, day: dt.date) -> None:
        self.chat.send(text, f"attendance-{day.isoformat()}", self.now())

    # events from the camera pipeline and the admin ---------------------------------

    def on_identified(self, employee_id: str, ts: dt.datetime, score: float, image: str | None) -> None:
        ts = self._local(ts)
        result = self.store.record_checkin(ts.date(), employee_id, ts, "camera", score, image)
        log.info("Identified %s at %s (score %.2f): %s", employee_id, ts.strftime("%H:%M:%S"), score, result)
        if result == "new":
            self._announce(employee_id, ts, confirmed_by_admin=False)

    def on_unknown(self, ts: dt.datetime, image: str | None, vector: np.ndarray | None,
                   guess_id: str | None, guess_score: float | None) -> int:
        ts = self._local(ts)
        log.info("Unknown face at %s (closest %s %.2f)", ts.strftime("%H:%M:%S"), guess_id, guess_score or 0)
        return self.store.add_unknown(ts.date(), ts, image, vector, guess_id, guess_score)

    def resolve_unknown(self, unknown_id: int, employee_id: str) -> None:
        """Admin says this unknown face is `employee_id`: learn the face and count the arrival."""
        u = self.store.unknown(unknown_id)
        if u is None or u.status != "pending":
            raise ValueError("this face has already been handled")
        if employee_id not in self.store.employees():
            raise ValueError(f"unknown employee {employee_id}")
        self.store.resolve_unknown(unknown_id, "assigned", employee_id)
        if u.vector is not None:
            self.store.add_embedding(employee_id, u.vector, f"admin:unknown:{unknown_id}", self.now())
            self.gallery.add(employee_id, u.vector)
        ts = self._local(u.ts)
        result = self.store.record_checkin(ts.date(), employee_id, ts, "admin", u.guess_score, u.image)
        if result in ("new", "earlier"):
            self._announce(employee_id, ts, confirmed_by_admin=True)

    def ignore_unknown(self, unknown_id: int) -> None:
        self.store.resolve_unknown(unknown_id, "ignored", None)

    def _announce(self, employee_id: str, ts: dt.datetime, confirmed_by_admin: bool) -> None:
        day = ts.date()
        if not self.is_workday(day):
            return
        w = self.window(day)
        name = self.store.employees().get(employee_id, employee_id)
        suffix = " (admin xác nhận)" if confirmed_by_admin else ""
        if ts >= w.late_after:
            if self.cfg.google_chat.notify_late_arrivals:
                late = int((ts - w.late_after).total_seconds() // 60)
                self._send(f"🟠 *{name}* đến lúc {ts:%H:%M}, muộn {late} phút{suffix}.", day)
        elif self.store.notice_sent(day, "deadline"):
            # They were on the 10:00 "not seen" list, but the admin found them earlier.
            self._send(f"✅ Cập nhật: *{name}* đã đến lúc {ts:%H:%M}{suffix}, không tính đi muộn.", day)

    # reports -------------------------------------------------------------------

    def report(self, day: dt.date) -> DayReport:
        w = self.window(day)
        rep = DayReport(day, self.is_workday(day), w)
        employees = self.store.employees()
        checkins = self.store.checkins(day)
        for emp_id, name in employees.items():
            c = checkins.get(emp_id)
            if c is None:
                rep.not_seen.append((emp_id, name))
                continue
            a = Arrival(emp_id, name, self._local(c.first_seen), c.source, c.image)
            (rep.late if a.ts >= w.late_after else rep.on_time).append(a)
        rep.on_time.sort(key=lambda a: a.ts)
        rep.late.sort(key=lambda a: a.ts)
        rep.pending_unknowns = len(self.store.unknowns(day))
        rep.outages = self._outages(day, self.now())
        return rep

    def _outages(self, day: dt.date, now: dt.datetime) -> list[tuple[dt.datetime, dt.datetime]]:
        """Recorded outages plus the one still open right now, if any."""
        found = [(self._local(a), self._local(b)) for a, b in self.store.outages(day)]
        w = self.window(day)
        if self.is_workday(day) and w.start <= now and day == now.date():
            ref = self._alive_ref(w)
            end = min(now, w.end)
            if ref < w.end and end - ref > GAP:
                found.append((ref, end))
        return found

    @staticmethod
    def _outage_lines(outages) -> list[str]:
        if not outages:
            return []
        spans = ", ".join(f"{a:%H:%M}–{b:%H:%M}" for a, b in outages)
        return [f"⚠️ Camera/hệ thống không hoạt động: {spans}. Người đến trong khoảng này có thể bị thiếu."]

    def deadline_text(self, rep: DayReport) -> str:
        w = rep.window
        if not rep.not_seen:
            lines = [f"⏰ {w.late_after:%H:%M}: cả {rep.total} người đều đã đến đúng giờ 🎉"]
        else:
            lines = [f"⏰ *{w.late_after:%H:%M}: {len(rep.not_seen)}/{rep.total} người chưa thấy* (tính là đi muộn):"]
            lines += [f"• {name}" for _, name in rep.not_seen]
        if rep.pending_unknowns:
            lines.append(f"❓ Có {rep.pending_unknowns} khuôn mặt chưa xác định, có thể là người trong danh sách trên. "
                         f"Admin xác nhận tại: {self._link('/unknowns')}")
        lines += self._outage_lines(rep.outages)
        lines.append(f"Chi tiết: {self._link('/')}")
        return "\n".join(lines)

    def summary_text(self, rep: DayReport) -> str:
        w = rep.window
        lines = [
            f"📋 *Tổng kết chấm công {rep.day:%d/%m/%Y}*",
            f"Đúng giờ: {len(rep.on_time)} · Đi muộn: {len(rep.late)} · Không thấy: {len(rep.not_seen)}"
            f" · Khuôn mặt chưa xác định: {rep.pending_unknowns}",
        ]
        if rep.late:
            lines.append("*Đi muộn:*")
            lines += [f"• {a.name}: {a.time} (muộn {minutes_late(a, w)} phút)" for a in rep.late]
        if rep.not_seen:
            lines.append(f"*Không thấy trên camera từ {w.start:%H:%M} đến {w.end:%H:%M}:*")
            lines += [f"• {name}" for _, name in rep.not_seen]
        if rep.pending_unknowns:
            lines.append(f"❓ Admin cần xác nhận {rep.pending_unknowns} khuôn mặt: {self._link('/unknowns')}")
        lines += self._outage_lines(rep.outages)
        lines.append(f"Chi tiết: {self._link('/')}")
        return "\n".join(lines)

    def unknown_text(self, unknowns) -> str:
        times = ", ".join(self._local(u.ts).strftime("%H:%M") for u in unknowns)
        return (f"❓ {len(unknowns)} khuôn mặt chưa xác định được ({times}). "
                f"Admin vui lòng xem và chọn đúng người: {self._link('/unknowns')}")

    # scheduler -----------------------------------------------------------------

    def tick(self, camera_ok: bool = True) -> None:
        """Call about once a second from the main loop."""
        now = self.now()
        day = now.date()
        if self.is_workday(day):
            w = self.window(day)
            self._track_health(now, w, camera_ok)

            if now >= w.late_after and not self.store.notice_sent(day, "deadline"):
                if now < w.end:  # after window_end the summary covers it
                    self._send(self.deadline_text(self.report(day)), day)
                self.store.mark_notice(day, "deadline", now)

            batch = dt.timedelta(minutes=self.cfg.google_chat.unknown_batch_minutes)
            if now < w.end and (self._last_unknown_notice is None or now - self._last_unknown_notice >= batch):
                pending = self.store.unnotified_unknowns(day)
                if pending:
                    self._send(self.unknown_text(pending), day)
                    self.store.mark_unknowns_notified([u.id for u in pending])
                    self._last_unknown_notice = now

            if now >= w.end and not self.store.notice_sent(day, "summary"):
                self.store.mark_unknowns_notified([u.id for u in self.store.unnotified_unknowns(day)])
                self._send(self.summary_text(self.report(day)), day)
                self.store.mark_notice(day, "summary", now)

        if self._last_flush is None or now - self._last_flush >= FLUSH_EVERY:
            self.chat.flush(now)
            self._last_flush = now

    def _alive_ref(self, w: Window) -> dt.datetime:
        """Last moment we know the camera was working today, never earlier than window start."""
        last = self._last_alive
        if last is None or self._local(last) < w.start:
            return w.start
        return self._local(last)

    def _track_health(self, now: dt.datetime, w: Window, camera_ok: bool) -> None:
        day = now.date()
        if now < w.start:
            return
        ref = self._alive_ref(w)
        if now >= w.end:
            # Close an outage that was still open when the window ended (also covers a PC that was off).
            if ref < w.end:
                if w.end - ref > GAP:
                    self.store.add_outage(day, ref, w.end)
                self._set_alive(w.end, force=True)
            return
        if camera_ok:
            if now - ref > GAP:
                self.store.add_outage(day, ref, now)
            self._set_alive(now, force=now - ref > GAP)

    def _set_alive(self, ts: dt.datetime, force: bool = False) -> None:
        self._last_alive = ts
        if force or self._last_alive_saved is None or ts - self._last_alive_saved >= ALIVE_SAVE_EVERY:
            self.store.put("last_alive", ts.isoformat())
            self._last_alive_saved = ts
