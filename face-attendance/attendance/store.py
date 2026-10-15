"""SQLite persistence. All state survives a restart, so a reboot mid-morning loses nothing.

Image paths stored here are relative to the data directory: "crops/..." for camera
snapshots (deleted after the retention period) and "faces/..." for reference photos.
"""
from __future__ import annotations

import csv
import datetime as dt
import io
import json
import sqlite3
import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SCHEMA = """
CREATE TABLE IF NOT EXISTS employees (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS embeddings (
    id INTEGER PRIMARY KEY,
    model TEXT NOT NULL,
    employee_id TEXT NOT NULL,
    vector BLOB NOT NULL,
    source TEXT NOT NULL,
    image TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS checkins (
    day TEXT NOT NULL,
    employee_id TEXT NOT NULL,
    first_seen TEXT NOT NULL,
    source TEXT NOT NULL,
    score REAL,
    image TEXT,
    PRIMARY KEY (day, employee_id)
);
CREATE TABLE IF NOT EXISTS unknowns (
    id INTEGER PRIMARY KEY,
    day TEXT NOT NULL,
    ts TEXT NOT NULL,
    image TEXT,
    model TEXT,
    vector BLOB,
    guess_id TEXT,
    guess_score REAL,
    status TEXT NOT NULL DEFAULT 'pending',
    employee_id TEXT,
    notified INTEGER NOT NULL DEFAULT 0,
    group_id INTEGER  -- unknown faces that look like the same person share a group
);
CREATE TABLE IF NOT EXISTS calendar (
    day TEXT PRIMARY KEY,
    kind TEXT NOT NULL,  -- 'holiday' (nobody works) or 'workday' (extra working day)
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS leaves (
    id INTEGER PRIMARY KEY,
    employee_id TEXT NOT NULL,
    start_day TEXT NOT NULL,
    end_day TEXT NOT NULL,
    kind TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    user TEXT NOT NULL,
    action TEXT NOT NULL,
    details TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS notices (
    day TEXT NOT NULL,
    kind TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (day, kind)
);
CREATE TABLE IF NOT EXISTS outages (
    day TEXT NOT NULL,
    start TEXT NOT NULL,
    end TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Labels for leave types sent by the HR system; other types are shown as sent.
LEAVE_KINDS = {"leave": "Leave", "business_trip": "Business trip", "remote": "Working remotely",
               "sick": "Sick", "other": "Other"}


@dataclass
class Employee:
    id: str
    name: str
    active: bool


@dataclass
class Checkin:
    employee_id: str
    first_seen: dt.datetime
    source: str
    score: float | None
    image: str | None


@dataclass
class Unknown:
    id: int
    ts: dt.datetime
    image: str | None
    model: str | None
    vector: np.ndarray | None
    guess_id: str | None
    guess_score: float | None
    status: str
    employee_id: str | None
    group_id: int = 0


@dataclass
class Reference:
    id: int
    employee_id: str
    image: str | None
    source: str
    vector: np.ndarray


@dataclass
class Leave:
    id: int
    employee_id: str
    start: dt.date
    end: dt.date
    kind: str
    note: str


def _vec(blob) -> np.ndarray | None:
    return None if blob is None else np.frombuffer(blob, dtype=np.float32)


def _blob(vector) -> bytes | None:
    return None if vector is None else np.asarray(vector, dtype=np.float32).tobytes()


class Store:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._migrate()

    def _migrate(self) -> None:
        """Add columns introduced after a database was created."""
        added = {"embeddings": [("image", "TEXT")], "unknowns": [("group_id", "INTEGER")]}
        for table, columns in added.items():
            have = {r["name"] for r in self._db.execute(f"PRAGMA table_info({table})")}
            for name, kind in columns:
                if name not in have:
                    self._db.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")
        self._db.commit()

    def _exec(self, sql: str, params=()) -> sqlite3.Cursor:
        with self._lock, self._db:
            return self._db.execute(sql, params)

    def _all(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    # employees -------------------------------------------------------------

    def import_employees(self, csv_text: str) -> int:
        """Add or rename employees from CSV text with columns id,name. Nobody is removed."""
        reader = csv.DictReader(io.StringIO(csv_text.lstrip("﻿")))
        if not reader.fieldnames or not {"id", "name"} <= {f.strip().lower() for f in reader.fieldnames}:
            raise ValueError("the CSV needs a header row with the columns id,name")
        rows = []
        for r in reader:
            r = {k.strip().lower(): (v or "").strip() for k, v in r.items() if k}
            if r.get("id") and r.get("name"):
                rows.append((r["id"], r["name"]))
        with self._lock, self._db:
            self._db.executemany(
                "INSERT INTO employees (id, name, active) VALUES (?, ?, 1) "
                "ON CONFLICT(id) DO UPDATE SET name = excluded.name, active = 1",
                rows,
            )
        return len(rows)

    def add_employee(self, employee_id: str, name: str) -> None:
        employee_id, name = employee_id.strip(), name.strip()
        if not employee_id or not name:
            raise ValueError("employee id and name are required")
        if self.employee(employee_id):
            raise ValueError(f"employee id {employee_id} already exists")
        self._exec("INSERT INTO employees (id, name, active) VALUES (?, ?, 1)", (employee_id, name))

    def update_employee(self, employee_id: str, name: str, active: bool) -> None:
        if not name.strip():
            raise ValueError("name is required")
        self._exec("UPDATE employees SET name = ?, active = ? WHERE id = ?", (name.strip(), int(active), employee_id))

    def delete_employee(self, employee_id: str) -> list[str]:
        """Remove the employee and their face data; returns reference image paths to delete.
        Past attendance records are kept."""
        images = [r["image"] for r in self._all("SELECT DISTINCT image FROM embeddings WHERE employee_id = ? "
                                                "AND image IS NOT NULL", (employee_id,))]
        with self._lock, self._db:
            self._db.execute("DELETE FROM embeddings WHERE employee_id = ?", (employee_id,))
            self._db.execute("DELETE FROM employees WHERE id = ?", (employee_id,))
        return images

    def employee(self, employee_id: str) -> Employee | None:
        rows = self._all("SELECT * FROM employees WHERE id = ?", (employee_id,))
        return Employee(rows[0]["id"], rows[0]["name"], bool(rows[0]["active"])) if rows else None

    def all_employees(self) -> list[Employee]:
        return [Employee(r["id"], r["name"], bool(r["active"]))
                for r in self._all("SELECT * FROM employees ORDER BY active DESC, name")]

    def employees(self) -> dict[str, str]:
        """Active employees: id -> name."""
        return {r["id"]: r["name"] for r in self._all("SELECT id, name FROM employees WHERE active = 1 ORDER BY name")}

    def employee_count(self) -> int:
        return self._all("SELECT COUNT(*) AS n FROM employees")[0]["n"]

    # reference faces -------------------------------------------------------

    def add_embedding(self, model: str, employee_id: str, vector: np.ndarray, source: str, now: dt.datetime,
                      image: str | None = None) -> None:
        self._exec(
            "INSERT INTO embeddings (model, employee_id, vector, source, image, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (model, employee_id, _blob(vector), source, image, now.isoformat()),
        )

    def embeddings(self, model: str) -> list[tuple[str, np.ndarray]]:
        rows = self._all(
            "SELECT e.employee_id, e.vector FROM embeddings e JOIN employees p ON p.id = e.employee_id "
            "WHERE p.active = 1 AND e.model = ?",
            (model,),
        )
        return [(r["employee_id"], _vec(r["vector"])) for r in rows]

    def references(self, model: str, employee_id: str | None = None) -> list[Reference]:
        sql, params = "SELECT * FROM embeddings WHERE model = ?", [model]
        if employee_id is not None:
            sql += " AND employee_id = ?"
            params.append(employee_id)
        return [Reference(r["id"], r["employee_id"], r["image"], r["source"], _vec(r["vector"]))
                for r in self._all(sql + " ORDER BY id", params)]

    def reference_images(self) -> list[tuple[str, str, str]]:
        """(employee_id, image, source) of every stored reference photo, from any model."""
        rows = self._all("SELECT employee_id, image, MIN(source) AS source FROM embeddings "
                         "WHERE image IS NOT NULL GROUP BY employee_id, image")
        return [(r["employee_id"], r["image"], r["source"]) for r in rows]

    def delete_reference(self, ref_id: int) -> None:
        self._exec("DELETE FROM embeddings WHERE id = ?", (ref_id,))

    def delete_reference_image(self, image: str) -> None:
        """Delete a reference photo's embeddings for all models."""
        self._exec("DELETE FROM embeddings WHERE image = ?", (image,))

    def delete_embeddings(self, model: str, employee_id: str, source_prefix: str) -> list[str]:
        images = [r["image"] for r in self._all(
            "SELECT image FROM embeddings WHERE model = ? AND employee_id = ? AND source LIKE ? AND image IS NOT NULL",
            (model, employee_id, source_prefix + "%"))]
        self._exec("DELETE FROM embeddings WHERE model = ? AND employee_id = ? AND source LIKE ?",
                   (model, employee_id, source_prefix + "%"))
        return images

    # check-ins -------------------------------------------------------------

    def record_checkin(self, day: dt.date, employee_id: str, ts: dt.datetime, source: str,
                       score: float | None, image: str | None) -> str:
        """Keep the earliest sighting per employee and day. Returns "new", "earlier" or "unchanged"."""
        with self._lock, self._db:
            row = self._db.execute(
                "SELECT first_seen FROM checkins WHERE day = ? AND employee_id = ?", (day.isoformat(), employee_id)
            ).fetchone()
            if row is None:
                self._db.execute(
                    "INSERT INTO checkins (day, employee_id, first_seen, source, score, image) VALUES (?, ?, ?, ?, ?, ?)",
                    (day.isoformat(), employee_id, ts.isoformat(), source, score, image),
                )
                return "new"
            if ts < dt.datetime.fromisoformat(row["first_seen"]):
                self._db.execute(
                    "UPDATE checkins SET first_seen = ?, source = ?, score = ?, image = ? WHERE day = ? AND employee_id = ?",
                    (ts.isoformat(), source, score, image, day.isoformat(), employee_id),
                )
                return "earlier"
            return "unchanged"

    def set_checkin(self, day: dt.date, employee_id: str, ts: dt.datetime, source: str,
                    score: float | None, image: str | None) -> None:
        """Replace the check-in whatever its current time (admin corrections)."""
        self._exec(
            "INSERT INTO checkins (day, employee_id, first_seen, source, score, image) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(day, employee_id) DO UPDATE SET first_seen = excluded.first_seen, source = excluded.source, "
            "score = excluded.score, image = excluded.image",
            (day.isoformat(), employee_id, ts.isoformat(), source, score, image),
        )

    def delete_checkin(self, day: dt.date, employee_id: str) -> Checkin | None:
        c = self.checkins(day).get(employee_id)
        if c is not None:
            self._exec("DELETE FROM checkins WHERE day = ? AND employee_id = ?", (day.isoformat(), employee_id))
        return c

    def checkins(self, day: dt.date) -> dict[str, Checkin]:
        rows = self._all("SELECT * FROM checkins WHERE day = ?", (day.isoformat(),))
        return {
            r["employee_id"]: Checkin(r["employee_id"], dt.datetime.fromisoformat(r["first_seen"]),
                                      r["source"], r["score"], r["image"])
            for r in rows
        }

    def checkins_between(self, start: dt.date, end: dt.date) -> list[tuple[dt.date, Checkin]]:
        rows = self._all("SELECT * FROM checkins WHERE day BETWEEN ? AND ? ORDER BY day",
                         (start.isoformat(), end.isoformat()))
        return [(dt.date.fromisoformat(r["day"]),
                 Checkin(r["employee_id"], dt.datetime.fromisoformat(r["first_seen"]), r["source"], r["score"],
                         r["image"])) for r in rows]

    # unknown faces ---------------------------------------------------------

    def add_unknown(self, day: dt.date, ts: dt.datetime, image: str | None, model: str | None,
                    vector: np.ndarray | None, guess_id: str | None, guess_score: float | None,
                    group_id: int | None = None) -> int:
        """Store an unidentified face; without a group it starts a new group of its own."""
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO unknowns (day, ts, image, model, vector, guess_id, guess_score, group_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (day.isoformat(), ts.isoformat(), image, model, _blob(vector), guess_id, guess_score, group_id),
            )
            if group_id is None:
                self._db.execute("UPDATE unknowns SET group_id = id WHERE id = ?", (cur.lastrowid,))
        return cur.lastrowid

    def _unknown(self, r) -> Unknown:
        return Unknown(r["id"], dt.datetime.fromisoformat(r["ts"]), r["image"], r["model"], _vec(r["vector"]),
                       r["guess_id"], r["guess_score"], r["status"], r["employee_id"], r["group_id"] or r["id"])

    def pending_groups(self) -> list[list[Unknown]]:
        """Pending unknown faces grouped by person, most recently seen group first."""
        groups: dict[int, list[Unknown]] = {}
        for u in self.unknowns():
            groups.setdefault(u.group_id, []).append(u)
        return sorted(groups.values(), key=lambda g: g[-1].ts, reverse=True)

    def group(self, group_id: int) -> list[Unknown]:
        rows = self._all("SELECT * FROM unknowns WHERE COALESCE(group_id, id) = ? AND status = 'pending' ORDER BY ts",
                         (group_id,))
        return [self._unknown(r) for r in rows]

    def resolve_group(self, group_id: int, status: str, employee_id: str | None) -> None:
        self._exec("UPDATE unknowns SET status = ?, employee_id = ? WHERE COALESCE(group_id, id) = ? AND status = 'pending'",
                   (status, employee_id, group_id))

    def pending_group_count(self, day: dt.date | None = None) -> int:
        sql, params = "SELECT COUNT(DISTINCT COALESCE(group_id, id)) AS n FROM unknowns WHERE status = 'pending'", []
        if day is not None:
            sql += " AND day = ?"
            params.append(day.isoformat())
        return self._all(sql, params)[0]["n"]

    def unknown(self, unknown_id: int) -> Unknown | None:
        rows = self._all("SELECT * FROM unknowns WHERE id = ?", (unknown_id,))
        return self._unknown(rows[0]) if rows else None

    def unknowns(self, day: dt.date | None = None, status: str | None = "pending") -> list[Unknown]:
        sql, params = "SELECT * FROM unknowns WHERE 1 = 1", []
        if day is not None:
            sql += " AND day = ?"
            params.append(day.isoformat())
        if status is not None:
            sql += " AND status = ?"
            params.append(status)
        return [self._unknown(r) for r in self._all(sql + " ORDER BY ts", params)]

    def unnotified_unknowns(self, day: dt.date) -> list[Unknown]:
        rows = self._all(
            "SELECT * FROM unknowns WHERE day = ? AND status = 'pending' AND notified = 0 ORDER BY ts", (day.isoformat(),)
        )
        return [self._unknown(r) for r in rows]

    def mark_unknowns_notified(self, ids: list[int]) -> None:
        with self._lock, self._db:
            self._db.executemany("UPDATE unknowns SET notified = 1 WHERE id = ?", [(i,) for i in ids])

    def resolve_unknown(self, unknown_id: int, status: str, employee_id: str | None) -> None:
        self._exec("UPDATE unknowns SET status = ?, employee_id = ? WHERE id = ?", (status, employee_id, unknown_id))

    # holidays and leave (mirrored from the HR system) ---------------------------

    def calendar_day(self, day: dt.date) -> tuple[str, str] | None:
        rows = self._all("SELECT kind, note FROM calendar WHERE day = ?", (day.isoformat(),))
        return (rows[0]["kind"], rows[0]["note"]) if rows else None

    def replace_external(self, start: dt.date, end: dt.date, days: list[tuple[dt.date, str, str]],
                         leaves: list[tuple[str, dt.date, dt.date, str, str]]) -> None:
        """Replace holidays/work days and leave between start and end with a fresh copy from the HR system.
        days: (day, 'holiday' | 'workday', note); leaves: (employee_id, start, end, kind, note)."""
        s, e = start.isoformat(), end.isoformat()
        with self._lock, self._db:
            self._db.execute("DELETE FROM calendar WHERE day BETWEEN ? AND ?", (s, e))
            self._db.execute("DELETE FROM leaves WHERE end_day >= ? AND start_day <= ?", (s, e))
            self._db.executemany(
                "INSERT INTO calendar (day, kind, note) VALUES (?, ?, ?) "
                "ON CONFLICT(day) DO UPDATE SET kind = excluded.kind, note = excluded.note",
                [(d.isoformat(), kind, note) for d, kind, note in days])
            self._db.executemany(
                "INSERT INTO leaves (employee_id, start_day, end_day, kind, note) VALUES (?, ?, ?, ?, ?)",
                [(emp, a.isoformat(), b.isoformat(), kind, note) for emp, a, b, kind, note in leaves])

    def leaves(self, ending_after: dt.date | None = None, employee_id: str | None = None) -> list[Leave]:
        sql, params = "SELECT * FROM leaves WHERE 1 = 1", []
        if ending_after is not None:
            sql += " AND end_day >= ?"
            params.append(ending_after.isoformat())
        if employee_id is not None:
            sql += " AND employee_id = ?"
            params.append(employee_id)
        return [Leave(r["id"], r["employee_id"], dt.date.fromisoformat(r["start_day"]),
                      dt.date.fromisoformat(r["end_day"]), r["kind"], r["note"])
                for r in self._all(sql + " ORDER BY start_day", params)]

    def leaves_on(self, day: dt.date) -> dict[str, str]:
        """employee_id -> leave kind for everyone on leave that day."""
        rows = self._all("SELECT employee_id, kind FROM leaves WHERE start_day <= ? AND end_day >= ?",
                         (day.isoformat(), day.isoformat()))
        return {r["employee_id"]: r["kind"] for r in rows}

    # audit -----------------------------------------------------------------

    def log(self, now: dt.datetime, user: str, action: str, details: str) -> None:
        self._exec("INSERT INTO audit (ts, user, action, details) VALUES (?, ?, ?, ?)",
                   (now.isoformat(), user, action, details))

    def audit(self, limit: int = 300) -> list[tuple[dt.datetime, str, str, str]]:
        rows = self._all("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,))
        return [(dt.datetime.fromisoformat(r["ts"]), r["user"], r["action"], r["details"]) for r in rows]

    # retention ---------------------------------------------------------------

    def expire_snapshots(self, before: dt.date) -> list[str]:
        """Forget camera snapshots taken before `before`; returns their image paths for deletion.
        Unknown faces from that period are removed entirely."""
        with self._lock, self._db:
            day = before.isoformat()
            images = [r["image"] for r in self._db.execute(
                "SELECT image FROM checkins WHERE day < ? AND image LIKE 'crops/%' "
                "UNION SELECT image FROM unknowns WHERE day < ? AND image IS NOT NULL", (day, day))]
            self._db.execute("UPDATE checkins SET image = NULL WHERE day < ? AND image LIKE 'crops/%'", (day,))
            self._db.execute("DELETE FROM unknowns WHERE day < ?", (day,))
        return images

    # notices, outages, outbox, kv -----------------------------------------

    def notice_sent(self, day: dt.date, kind: str) -> bool:
        return bool(self._all("SELECT 1 FROM notices WHERE day = ? AND kind = ?", (day.isoformat(), kind)))

    def mark_notice(self, day: dt.date, kind: str, now: dt.datetime) -> None:
        self._exec("INSERT OR IGNORE INTO notices (day, kind, sent_at) VALUES (?, ?, ?)",
                   (day.isoformat(), kind, now.isoformat()))

    def add_outage(self, day: dt.date, start: dt.datetime, end: dt.datetime) -> None:
        self._exec("INSERT INTO outages (day, start, end) VALUES (?, ?, ?)", (day.isoformat(), start.isoformat(), end.isoformat()))

    def outages(self, day: dt.date) -> list[tuple[dt.datetime, dt.datetime]]:
        rows = self._all("SELECT start, end FROM outages WHERE day = ? ORDER BY start", (day.isoformat(),))
        return [(dt.datetime.fromisoformat(r["start"]), dt.datetime.fromisoformat(r["end"])) for r in rows]

    def enqueue(self, payload: dict, now: dt.datetime) -> None:
        self._exec("INSERT INTO outbox (payload, created_at) VALUES (?, ?)", (json.dumps(payload, ensure_ascii=False), now.isoformat()))

    def outbox(self) -> list[tuple[int, dict, dt.datetime]]:
        rows = self._all("SELECT id, payload, created_at FROM outbox ORDER BY id")
        return [(r["id"], json.loads(r["payload"]), dt.datetime.fromisoformat(r["created_at"])) for r in rows]

    def outbox_done(self, item_id: int) -> None:
        self._exec("DELETE FROM outbox WHERE id = ?", (item_id,))

    def outbox_failed(self, item_id: int) -> None:
        self._exec("UPDATE outbox SET attempts = attempts + 1 WHERE id = ?", (item_id,))

    def get(self, key: str) -> str | None:
        rows = self._all("SELECT value FROM kv WHERE key = ?", (key,))
        return rows[0]["value"] if rows else None

    def put(self, key: str, value: str) -> None:
        self._exec("INSERT INTO kv (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                   (key, value))
