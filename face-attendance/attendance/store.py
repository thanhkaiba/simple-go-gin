"""SQLite persistence. All state survives a restart, so a reboot mid-morning loses nothing."""
from __future__ import annotations

import csv
import datetime as dt
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
    employee_id TEXT NOT NULL,
    vector BLOB NOT NULL,
    source TEXT NOT NULL,
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
    vector BLOB,
    guess_id TEXT,
    guess_score REAL,
    status TEXT NOT NULL DEFAULT 'pending',
    employee_id TEXT,
    notified INTEGER NOT NULL DEFAULT 0
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
    vector: np.ndarray | None
    guess_id: str | None
    guess_score: float | None
    status: str
    employee_id: str | None


def _vec(blob) -> np.ndarray | None:
    return None if blob is None else np.frombuffer(blob, dtype=np.float32)


class Store:
    def __init__(self, path: str | Path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._db.executescript(SCHEMA)
            self._db.execute("PRAGMA journal_mode=WAL")

    def _exec(self, sql: str, params=()) -> sqlite3.Cursor:
        with self._lock, self._db:
            return self._db.execute(sql, params)

    def _all(self, sql: str, params=()) -> list[sqlite3.Row]:
        with self._lock:
            return self._db.execute(sql, params).fetchall()

    # employees -------------------------------------------------------------

    def sync_employees(self, csv_path: str | Path) -> int:
        """Load employees from a CSV with columns id,name. Employees missing from the file become inactive."""
        with open(csv_path, encoding="utf-8-sig", newline="") as f:
            rows = [(r["id"].strip(), r["name"].strip()) for r in csv.DictReader(f) if r.get("id", "").strip()]
        with self._lock, self._db:
            self._db.execute("UPDATE employees SET active = 0")
            self._db.executemany(
                "INSERT INTO employees (id, name, active) VALUES (?, ?, 1) "
                "ON CONFLICT(id) DO UPDATE SET name = excluded.name, active = 1",
                rows,
            )
        return len(rows)

    def employees(self) -> dict[str, str]:
        return {r["id"]: r["name"] for r in self._all("SELECT id, name FROM employees WHERE active = 1 ORDER BY name")}

    # embeddings ------------------------------------------------------------

    def add_embedding(self, employee_id: str, vector: np.ndarray, source: str, now: dt.datetime) -> None:
        self._exec(
            "INSERT INTO embeddings (employee_id, vector, source, created_at) VALUES (?, ?, ?, ?)",
            (employee_id, np.asarray(vector, dtype=np.float32).tobytes(), source, now.isoformat()),
        )

    def delete_embeddings(self, employee_id: str, source_prefix: str) -> None:
        self._exec("DELETE FROM embeddings WHERE employee_id = ? AND source LIKE ?", (employee_id, source_prefix + "%"))

    def embeddings(self) -> list[tuple[str, np.ndarray]]:
        rows = self._all(
            "SELECT e.employee_id, e.vector FROM embeddings e JOIN employees p ON p.id = e.employee_id WHERE p.active = 1"
        )
        return [(r["employee_id"], _vec(r["vector"])) for r in rows]

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

    def checkins(self, day: dt.date) -> dict[str, Checkin]:
        rows = self._all("SELECT * FROM checkins WHERE day = ?", (day.isoformat(),))
        return {
            r["employee_id"]: Checkin(r["employee_id"], dt.datetime.fromisoformat(r["first_seen"]),
                                      r["source"], r["score"], r["image"])
            for r in rows
        }

    # unknown faces ---------------------------------------------------------

    def add_unknown(self, day: dt.date, ts: dt.datetime, image: str | None, vector: np.ndarray | None,
                    guess_id: str | None, guess_score: float | None) -> int:
        blob = None if vector is None else np.asarray(vector, dtype=np.float32).tobytes()
        cur = self._exec(
            "INSERT INTO unknowns (day, ts, image, vector, guess_id, guess_score) VALUES (?, ?, ?, ?, ?, ?)",
            (day.isoformat(), ts.isoformat(), image, blob, guess_id, guess_score),
        )
        return cur.lastrowid

    def _unknown(self, r) -> Unknown:
        return Unknown(r["id"], dt.datetime.fromisoformat(r["ts"]), r["image"], _vec(r["vector"]),
                       r["guess_id"], r["guess_score"], r["status"], r["employee_id"])

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
