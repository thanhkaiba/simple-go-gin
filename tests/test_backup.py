import datetime as dt
import hashlib
import json
import sqlite3
import sys

import pytest
from conftest import DAY, at
from test_web import client

from attendance import backup as backup_module, config, updater
from attendance.backup import SUBDIR, restore
from test_updates import make_package


def prepare(app, tmp_path):
    """Some data worth keeping: an arrival with pictures, a reference photo, a settings file."""
    data = app.data_dir
    (data / "faces" / "A").mkdir(parents=True)
    (data / "faces" / "A" / "ref.jpg").write_bytes(b"reference")
    (data / "crops" / DAY.isoformat()).mkdir(parents=True)
    (data / "crops" / DAY.isoformat() / "a.jpg").write_bytes(b"face")
    (data / "crops" / DAY.isoformat() / "a_scene.jpg").write_bytes(b"scene")
    (app.cfg.base_dir / "config.yaml").write_text("timezone: Asia/Ho_Chi_Minh\n", encoding="utf-8")
    app.service.on_identified("A", at(9, 5), 0.8, f"crops/{DAY}/a.jpg")
    drive = tmp_path / "drive"
    drive.mkdir()
    app.cfg.backup.folder = str(drive)
    return drive / SUBDIR


def test_backup_copies_everything_and_then_only_what_changed(core, tmp_path):
    app, clock, _ = core
    dest = prepare(app, tmp_path)
    clock.t = at(12, 30)
    result = app.backup.run_now()
    assert result.ok, result.message
    assert (dest / "config.yaml").exists() and (dest / "data" / "faces" / "A" / "ref.jpg").exists()
    assert (dest / "data" / "crops" / DAY.isoformat() / "a_scene.jpg").read_bytes() == b"scene"
    copy = dest / "db" / f"attendance-{DAY}.db"
    with sqlite3.connect(copy) as db:
        assert db.execute("SELECT COUNT(*) FROM employees").fetchone()[0] == 3
        assert db.execute("SELECT employee_id FROM checkins").fetchone()[0] == "A"
    assert json.loads((dest / "latest.json").read_text())["ok"] is True
    assert result.copied == 3 and app.backup.status()["ok"] and app.store.get(backup_module.DAY_KEY) == DAY.isoformat()

    # Nothing changed: nothing copied. A deleted picture is removed, a new one copied.
    assert app.backup.run_now().copied == 0
    (app.data_dir / "crops" / DAY.isoformat() / "a.jpg").unlink()
    (app.data_dir / "faces" / "A" / "new.jpg").write_bytes(b"another")
    again = app.backup.run_now()
    assert (again.copied, again.removed) == (1, 1)
    assert not (dest / "data" / "crops" / DAY.isoformat() / "a.jpg").exists()

    # Without camera pictures, only the database and the reference photos are written.
    app.cfg.backup.include_photos = False
    (app.data_dir / "crops" / DAY.isoformat() / "b.jpg").write_bytes(b"later")
    assert app.backup.run_now().copied == 0


def test_old_database_copies_are_rotated(core, tmp_path):
    app, clock, _ = core
    dest = prepare(app, tmp_path)
    app.cfg.backup.keep_days = 2
    (dest / "db").mkdir(parents=True)
    for d in ("2026-09-01", "2026-09-02", "2026-09-03"):
        (dest / "db" / f"attendance-{d}.db").write_bytes(b"old")
    clock.t = at(12, 30)
    assert app.backup.run_now().ok
    assert sorted(p.name for p in (dest / "db").glob("*.db")) == ["attendance-2026-09-03.db", f"attendance-{DAY}.db"]


def test_failures_are_reported_once_a_day_and_retried_later(core, tmp_path):
    app, clock, sent = core
    app.cfg.backup.folder = str(tmp_path / "unplugged")
    clock.t = at(12, 30)
    assert app.backup.due(clock())
    result = app.backup.run_now()
    assert not result.ok and "does not exist" in result.message
    assert sent[-1].startswith("⚠️ Backup failed") and "unplugged" in sent[-1]
    assert "failed" in app.backup.warning(clock())
    assert not app.backup.due(clock())  # not again right away
    clock.t = at(13, 45)
    assert app.backup.due(clock())  # an hour later it is tried again
    app.backup.run_now()
    assert sum(m.startswith("⚠️ Backup failed") for m in sent) == 1  # the chat is told once a day

    # Inside the data folder: refused.
    app.cfg.backup.folder = str(app.data_dir / "nested")
    (app.data_dir / "nested").mkdir()
    assert "inside the data folder" in app.backup.run_now().message


def test_scheduled_from_tick_once_a_day_after_the_set_time(core, tmp_path):
    app, clock, _ = core
    dest = prepare(app, tmp_path)
    app.cfg.backup.time = "12:00"
    clock.t = at(11, 59)
    app.service.tick()
    assert not app.backup.running and not (dest / "db").exists()
    clock.t = at(12, 1)
    app.service.tick()
    app.backup.wait()
    assert (dest / "db" / f"attendance-{DAY}.db").exists()
    assert not app.backup.due(at(15, 0))  # done for today
    assert app.backup.warning(at(12, 30)) is None
    assert "none since" in app.backup.warning(at(12, 30, day=DAY + dt.timedelta(days=3)))
    assert app.backup.due(at(12, 30, day=DAY + dt.timedelta(days=1)))


def test_restore_on_another_computer(core, tmp_path):
    app, clock, _ = core
    dest = prepare(app, tmp_path)
    clock.t = at(12, 30)
    assert app.backup.run_now().ok
    (dest / "db" / "attendance-2026-09-01.db").write_bytes(b"not a database")

    other = tmp_path / "other"
    other.mkdir()
    cfg = config.Config(data_dir="data", base_dir=other)
    cfg.validate()
    with pytest.raises(SystemExit, match="No database copy for 2026-08-01"):
        restore(cfg, dest.parent, "2026-08-01")
    with pytest.raises(sqlite3.DatabaseError):
        restore(cfg, dest.parent, "2026-09-01")
    assert not (other / "data" / "attendance.db").exists()  # nothing touched after a bad copy

    text = restore(cfg, dest.parent)
    assert "3 employees" in text and "config.yaml restored" in text
    with sqlite3.connect(other / "data" / "attendance.db") as db:
        assert db.execute("SELECT employee_id FROM checkins").fetchone()[0] == "A"
    assert (other / "data" / "faces" / "A" / "ref.jpg").read_bytes() == b"reference"
    assert (other / "config.yaml").exists()

    (other / "running.pid").write_text("1")
    with pytest.raises(SystemExit, match="Stop the program first"):
        restore(cfg, dest)


def test_backup_from_the_settings_page_and_warning_on_today(core, tmp_path):
    app, clock, _ = core
    c = client(app)
    prepare(app, tmp_path)
    app.cfg.backup.folder = ""
    page = c.get("/settings").get_data(as_text=True)
    assert "Backup folder" in page and "Back up now" in page
    resp = c.post("/settings/backup-now", data={}, follow_redirects=True)
    assert "Enter a backup folder first" in resp.get_data(as_text=True)

    clock.t = at(12, 30)
    drive = tmp_path / "drive"
    c.post("/settings/backup-now", data={"backup.folder": str(drive), "backup.time": "12:00",
                                         "backup.keep_days": "7", "backup.include_photos": "on"})
    app.backup.wait()
    assert app.cfg.backup.folder == str(drive) and app.cfg.backup.keep_days == 7
    assert app.backup.status()["ok"] and (drive / SUBDIR / "db").is_dir()
    page = c.get("/settings").get_data(as_text=True)
    assert "Last backup" in page and "backup now" in c.get("/audit").get_data(as_text=True)
    assert "backup failed" not in c.get("/").get_data(as_text=True)

    drive.rename(tmp_path / "gone")
    app.backup.run_now()
    assert "The last backup failed" in c.get("/").get_data(as_text=True)


def test_update_download_is_checked_against_the_published_checksum(tmp_path):
    archive = tmp_path / updater.asset_name()
    make_package(archive)
    good = "sha256:" + hashlib.sha256(archive.read_bytes()).hexdigest()
    exe = updater.unpack(archive.as_uri(), "9.0.0", tmp_path / "home", good)
    assert exe.exists()
    with pytest.raises(RuntimeError, match="checksum"):
        updater.unpack(archive.as_uri(), "9.0.1", tmp_path / "home", "sha256:" + "0" * 64)
    assert not (tmp_path / "home" / "versions" / "9.0.1").exists()
    assert updater.REPO == "thanhkaiba/rice-camera"
