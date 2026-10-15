"""Attendance rules, admin corrections and scheduled Google Chat notices.

Rules for a work day (times from config.schedule):
- First seen before late_after (e.g. 10:00): on time. Being seen again later changes nothing.
- First seen at or after late_after: late, and announced as soon as they are seen.
- At late_after: one alert listing everyone not seen yet.
- At window_end (e.g. 11:00): daily summary.
- People on leave are never counted as late or missing; holidays send nothing.
- Faces the system could not identify are reported to the admin with a link to review them.
"""
from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

import numpy as np

from . import leave_api
from .config import Config
from .gallery import Gallery
from .notifier import GoogleChat
from .store import LEAVE_KINDS, Store

if TYPE_CHECKING:
    from .faces import FaceLibrary

log = logging.getLogger(__name__)

GAP = dt.timedelta(seconds=60)  # no frames for longer than this counts as an outage
ALIVE_SAVE_EVERY = dt.timedelta(seconds=10)
FLUSH_EVERY = dt.timedelta(seconds=30)
# An unknown face that resembles someone already checked in today is most likely that person
# walking past again at a bad angle; reporting it would only create work for the admin.
ALREADY_SEEN_SIMILARITY = 0.3

SOURCES = {"camera": "Camera", "admin": "Admin confirmed photo", "manual": "Entered by admin",
           "corrected": "Corrected by admin"}


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
    score: float | None = None

    @property
    def time(self) -> str:
        return self.ts.strftime("%H:%M")


@dataclass
class DayReport:
    day: dt.date
    workday: bool
    day_note: str
    window: Window
    on_time: list[Arrival] = field(default_factory=list)
    late: list[Arrival] = field(default_factory=list)
    not_seen: list[tuple[str, str]] = field(default_factory=list)  # (id, name)
    on_leave: list[tuple[str, str, str]] = field(default_factory=list)  # (id, name, kind label)
    no_photos: list[tuple[str, str]] = field(default_factory=list)
    pending_unknowns: int = 0
    outages: list[tuple[dt.datetime, dt.datetime]] = field(default_factory=list)
    # True when the camera never worked that day (e.g. before the system was installed or the
    # computer was off): "not seen" then says nothing about who was absent.
    no_camera_data: bool = False

    @property
    def total(self) -> int:
        return len(self.on_time) + len(self.late) + len(self.not_seen)


def minutes_late(a: Arrival, w: Window) -> int:
    return int((a.ts - w.late_after).total_seconds() // 60)


@dataclass
class PersonSummary:
    employee_id: str
    name: str
    work_days: int = 0
    on_time: int = 0
    late: int = 0
    late_minutes: int = 0
    not_seen: int = 0
    on_leave: int = 0


@dataclass
class PeriodReport:
    start: dt.date
    end: dt.date
    days: list[DayReport]  # work days only
    people: list[PersonSummary]

    @property
    def no_data_days(self) -> list[DayReport]:
        return [d for d in self.days if d.no_camera_data]


class AttendanceService:
    def __init__(self, cfg: Config, store: Store, chat: GoogleChat, gallery: Gallery,
                 now: Callable[[], dt.datetime] | None = None):
        self.cfg = cfg
        self.tz = cfg.tz
        self.store = store
        self.chat = chat
        self.gallery = gallery
        self.library: FaceLibrary | None = None  # set by the app; used to learn admin-confirmed faces
        self._now = now or (lambda: dt.datetime.now(self.tz))
        saved = store.get("last_alive")
        self._last_alive = dt.datetime.fromisoformat(saved) if saved else None
        self._last_alive_saved = self._last_alive
        self._last_unknown_notice: dt.datetime | None = None
        self._last_flush: dt.datetime | None = None
        self._camera_seen_day: dt.date | None = None

    @property
    def model_id(self) -> str:
        return self.cfg.recognition.model_id()

    # time helpers ------------------------------------------------------------

    def now(self) -> dt.datetime:
        return self._now()

    def window(self, day: dt.date) -> Window:
        start, late, end = self.cfg.schedule.clocks()
        return Window(*(dt.datetime.combine(day, t, tzinfo=self.tz) for t in (start, late, end)))

    def day_info(self, day: dt.date) -> tuple[bool, str]:
        """(is a work day, note) taking the holiday calendar into account."""
        override = self.store.calendar_day(day)
        if override:
            kind, note = override
            label = "Holiday" if kind == "holiday" else "Extra work day"
            return kind == "workday", f"{label}: {note}" if note else label
        return day.weekday() in self.cfg.schedule.work_days, ""

    def is_workday(self, day: dt.date) -> bool:
        return self.day_info(day)[0]

    def in_window(self, now: dt.datetime) -> bool:
        w = self.window(now.date())
        return w.start <= now < w.end and self.is_workday(now.date())

    def _local(self, ts: dt.datetime) -> dt.datetime:
        return ts.astimezone(self.tz)

    def _link(self, path: str = "") -> str:
        return self.cfg.web.public_url.rstrip("/") + path

    def _send(self, text: str, day: dt.date) -> None:
        self.chat.send(text, f"attendance-{day.isoformat()}", self.now())

    def alert(self, text: str) -> None:
        """System alert, posted in today's thread."""
        self._send(text, self.now().date())

    def _name(self, employee_id: str) -> str:
        e = self.store.employee(employee_id)
        return e.name if e else employee_id

    # events from the camera pipeline ----------------------------------------------

    def on_identified(self, employee_id: str, ts: dt.datetime, score: float, image: str | None) -> None:
        ts = self._local(ts)
        result = self.store.record_checkin(ts.date(), employee_id, ts, "camera", score, image)
        log.info("Identified %s at %s (score %.2f): %s", employee_id, ts.strftime("%H:%M:%S"), score, result)
        if result == "new":
            self._announce(employee_id, ts, suffix="")

    def on_unknown(self, ts: dt.datetime, image: str | None, vector: np.ndarray | None,
                   guess_id: str | None, guess_score: float | None) -> int | None:
        ts = self._local(ts)
        if (guess_id and guess_score is not None and guess_score >= ALREADY_SEEN_SIMILARITY
                and guess_id in self.store.checkins(ts.date())):
            log.info("Unclear face at %s resembles %s, who is already checked in; not reported",
                     ts.strftime("%H:%M:%S"), guess_id)
            return None
        log.info("Unknown face at %s (closest %s %.2f)", ts.strftime("%H:%M:%S"), guess_id, guess_score or 0)
        return self.store.add_unknown(ts.date(), ts, image, self.model_id, vector, guess_id, guess_score)

    # admin actions -------------------------------------------------------------

    def resolve_unknown(self, unknown_id: int, employee_id: str, user: str = "admin") -> None:
        """Admin says this unknown face is `employee_id`: learn the face and count the arrival."""
        u = self.store.unknown(unknown_id)
        if u is None or u.status != "pending":
            raise ValueError("this face has already been handled")
        if employee_id not in self.store.employees():
            raise ValueError(f"unknown employee {employee_id}")
        self.store.resolve_unknown(unknown_id, "assigned", employee_id)
        if u.vector is not None and u.model:
            if self.library is not None:
                self.library.learn(employee_id, u.vector, u.image, u.model)
            else:
                self.store.add_embedding(u.model, employee_id, u.vector, "admin", self.now())
                if u.model == self.model_id:
                    self.gallery.add(employee_id, u.vector)
        ts = self._local(u.ts)
        result = self.store.record_checkin(ts.date(), employee_id, ts, "admin", u.guess_score, u.image)
        self.store.log(self.now(), user, "confirm face", f"{ts:%Y-%m-%d %H:%M} -> {self._name(employee_id)}")
        if result in ("new", "earlier"):
            self._announce(employee_id, ts, suffix=" (confirmed by admin)")

    def ignore_unknown(self, unknown_id: int, user: str = "admin") -> None:
        self.store.resolve_unknown(unknown_id, "ignored", None)
        self.store.log(self.now(), user, "ignore face", f"unknown #{unknown_id}")

    def set_arrival(self, day: dt.date, employee_id: str, time: dt.time, user: str = "admin") -> None:
        """Enter or change an arrival time by hand."""
        if employee_id not in self.store.employees():
            raise ValueError(f"unknown employee {employee_id}")
        ts = dt.datetime.combine(day, time, tzinfo=self.tz)
        old = self.store.checkins(day).get(employee_id)
        self.store.set_checkin(day, employee_id, ts, "manual", None, old.image if old else None)
        before = f" (was {self._local(old.first_seen):%H:%M})" if old else ""
        self.store.log(self.now(), user, "set arrival", f"{day} {self._name(employee_id)} {time:%H:%M}{before}")
        self._announce(employee_id, ts, suffix=" (entered by admin)")

    def reassign_arrival(self, day: dt.date, from_id: str, to_id: str, user: str = "admin") -> None:
        """The camera recorded the wrong person: move the record (time and photo) to the right one."""
        if to_id not in self.store.employees():
            raise ValueError(f"unknown employee {to_id}")
        if from_id == to_id:
            raise ValueError("choose a different employee")
        c = self.store.delete_checkin(day, from_id)
        if c is None:
            raise ValueError("there is no arrival to move")
        ts = self._local(c.first_seen)
        result = self.store.record_checkin(day, to_id, ts, "corrected", c.score, c.image)
        self.store.log(self.now(), user, "reassign arrival",
                       f"{day} {ts:%H:%M} {self._name(from_id)} -> {self._name(to_id)}")
        if result in ("new", "earlier"):
            self._announce(to_id, ts, suffix=" (corrected by admin)")

    def delete_arrival(self, day: dt.date, employee_id: str, user: str = "admin") -> None:
        c = self.store.delete_checkin(day, employee_id)
        if c is None:
            raise ValueError("there is no arrival to delete")
        self.store.log(self.now(), user, "delete arrival",
                       f"{day} {self._name(employee_id)} {self._local(c.first_seen):%H:%M}")

    def _announce(self, employee_id: str, ts: dt.datetime, suffix: str) -> None:
        day = ts.date()
        if not self.is_workday(day) or employee_id in self.store.leaves_on(day):
            return
        w = self.window(day)
        name = self._name(employee_id)
        when = f"{ts:%H:%M}" if day == self.now().date() else f"{ts:%H:%M} on {day:%Y-%m-%d}"
        if ts >= w.late_after:
            if self.cfg.google_chat.notify_late_arrivals:
                late = int((ts - w.late_after).total_seconds() // 60)
                self._send(f"🟠 *{name}* arrived at {when}, {late} min late{suffix}.", day)
        elif self.store.notice_sent(day, "deadline"):
            # They were on the "not seen" list, but turned out to have arrived on time.
            self._send(f"✅ Update: *{name}* arrived at {when}{suffix}, not late.", day)

    # reports -------------------------------------------------------------------

    def report(self, day: dt.date) -> DayReport:
        w = self.window(day)
        workday, note = self.day_info(day)
        rep = DayReport(day, workday, note, w)
        employees = self.store.employees()
        checkins = self.store.checkins(day)
        leave = self.store.leaves_on(day)
        enrolled = self.gallery.counts()
        for emp_id, name in employees.items():
            c = checkins.get(emp_id)
            if c is not None:
                a = Arrival(emp_id, name, self._local(c.first_seen), c.source, c.image, c.score)
                (rep.late if a.ts >= w.late_after else rep.on_time).append(a)
            elif emp_id in leave:
                rep.on_leave.append((emp_id, name, LEAVE_KINDS.get(leave[emp_id], leave[emp_id])))
            else:
                rep.not_seen.append((emp_id, name))
            if emp_id not in enrolled:
                rep.no_photos.append((emp_id, name))
        rep.on_time.sort(key=lambda a: a.ts)
        rep.late.sort(key=lambda a: a.ts)
        rep.pending_unknowns = len(self.store.unknowns(day))
        now = self.now()
        rep.outages = self._outages(day, now)
        rep.no_camera_data = (workday and (day < now.date() or now >= w.start)
                              and not self.store.notice_sent(day, "camera_ok"))
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

    def _extra_lines(self, rep: DayReport) -> list[str]:
        lines = []
        stale = leave_api.warning(self.cfg, self.store, self.now())
        if stale:
            lines.append(stale)
        if rep.on_leave:
            lines.append("🏖 On leave: " + ", ".join(f"{n} ({k})" for _, n, k in rep.on_leave))
        if rep.no_photos:
            lines.append("⚠️ No reference photos, cannot be recognised: " + ", ".join(n for _, n in rep.no_photos))
        if rep.outages:
            spans = ", ".join(f"{a:%H:%M}–{b:%H:%M}" for a, b in rep.outages)
            lines.append(f"⚠️ Camera/system was down: {spans}. Arrivals during this time may be missing.")
        return lines

    def deadline_text(self, rep: DayReport) -> str:
        w = rep.window
        if not rep.not_seen:
            lines = [f"⏰ {w.late_after:%H:%M}: all {rep.total} people arrived on time 🎉"]
        else:
            lines = [f"⏰ *{w.late_after:%H:%M}: {len(rep.not_seen)}/{rep.total} people not seen yet* (counted as late):"]
            lines += [f"• {name}" for _, name in rep.not_seen]
        if rep.pending_unknowns:
            lines.append(f"❓ {rep.pending_unknowns} unidentified faces, possibly people on this list. "
                         f"Review: {self._link('/unknowns')}")
        lines += self._extra_lines(rep)
        lines.append(f"Details: {self._link('/')}")
        return "\n".join(lines)

    def summary_text(self, rep: DayReport) -> str:
        w = rep.window
        lines = [
            f"📋 *Attendance summary {rep.day:%Y-%m-%d}*",
            f"On time: {len(rep.on_time)} · Late: {len(rep.late)} · Not seen: {len(rep.not_seen)}"
            f" · On leave: {len(rep.on_leave)} · Unidentified faces: {rep.pending_unknowns}",
        ]
        if rep.late:
            lines.append("*Late:*")
            lines += [f"• {a.name}: {a.time} ({minutes_late(a, w)} min late)" for a in rep.late]
        if rep.not_seen:
            lines.append(f"*Not seen on camera between {w.start:%H:%M} and {w.end:%H:%M}:*")
            lines += [f"• {name}" for _, name in rep.not_seen]
        if rep.pending_unknowns:
            lines.append(f"❓ {rep.pending_unknowns} unidentified faces to review: {self._link('/unknowns')}")
        lines += self._extra_lines(rep)
        lines.append(f"Details: {self._link('/')}")
        return "\n".join(lines)

    def period_report(self, start: dt.date, end: dt.date) -> PeriodReport:
        """Late and absence totals per employee over a range of days. Today is included once
        the late time has passed; days that are not work days are skipped."""
        now = self.now()
        end = min(end, now.date())
        if end == now.date() and now < self.window(end).late_after:
            end -= dt.timedelta(days=1)
        people: dict[str, PersonSummary] = {}
        days = []
        day = start
        while day <= end:
            rep = self.report(day)
            day += dt.timedelta(days=1)
            if not rep.workday:
                continue
            days.append(rep)
            for a in rep.on_time:
                p = people.setdefault(a.employee_id, PersonSummary(a.employee_id, a.name))
                p.work_days, p.on_time = p.work_days + 1, p.on_time + 1
            for a in rep.late:
                p = people.setdefault(a.employee_id, PersonSummary(a.employee_id, a.name))
                p.work_days, p.late = p.work_days + 1, p.late + 1
                p.late_minutes += minutes_late(a, rep.window)
            if not rep.no_camera_data:
                for emp_id, name in rep.not_seen:
                    p = people.setdefault(emp_id, PersonSummary(emp_id, name))
                    p.work_days, p.not_seen = p.work_days + 1, p.not_seen + 1
            for emp_id, name, _ in rep.on_leave:
                p = people.setdefault(emp_id, PersonSummary(emp_id, name))
                p.on_leave += 1
        ranked = sorted(people.values(), key=lambda p: (-(p.late + p.not_seen), -p.late_minutes, p.name))
        return PeriodReport(start, end, days, ranked)

    def unknown_text(self, unknowns) -> str:
        times = ", ".join(self._local(u.ts).strftime("%H:%M") for u in unknowns)
        return (f"❓ {len(unknowns)} unidentified faces ({times}). "
                f"Please pick the right person: {self._link('/unknowns')}")

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

        if self.store.get("cleanup_day") != day.isoformat():
            self.cleanup(day)
            self.store.put("cleanup_day", day.isoformat())

        if self._last_flush is None or now - self._last_flush >= FLUSH_EVERY:
            self.chat.flush(now)
            self._last_flush = now

    def cleanup(self, today: dt.date) -> int:
        """Delete camera snapshots and unknown faces older than the retention period."""
        cutoff = today - dt.timedelta(days=self.cfg.retention_days)
        data_dir = self.cfg.path(self.cfg.data_dir)
        removed = 0
        for image in self.store.expire_snapshots(cutoff):
            path = data_dir / image
            if image.startswith("crops/") and path.exists():
                path.unlink()
                removed += 1
        crops = data_dir / "crops"
        if crops.exists():
            for folder in crops.iterdir():  # folders are named by day
                if folder.is_dir() and folder.name < cutoff.isoformat() and not any(folder.iterdir()):
                    folder.rmdir()
        if removed:
            log.info("Retention: deleted %d snapshots older than %s", removed, cutoff)
        return removed

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
            if self._camera_seen_day != day:  # remember that the camera worked on this day
                self.store.mark_notice(day, "camera_ok", now)
                self._camera_seen_day = day

    def _set_alive(self, ts: dt.datetime, force: bool = False) -> None:
        self._last_alive = ts
        if force or self._last_alive_saved is None or ts - self._last_alive_saved >= ALIVE_SAVE_EVERY:
            self.store.put("last_alive", ts.isoformat())
            self._last_alive_saved = ts
