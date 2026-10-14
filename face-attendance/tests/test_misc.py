import datetime as dt

import numpy as np
import pytest

from attendance import config
from attendance.evaluate import Sample, recommend, sweep
from attendance.gallery import Gallery
from attendance.notifier import GoogleChat
from attendance.store import Store

NOW = dt.datetime(2026, 10, 14, 9, 0, tzinfo=dt.timezone.utc)


def test_gallery_uses_mean_of_two_best_references():
    a1, a2, b = np.eye(3, dtype=np.float32)
    g = Gallery([("A", a1), ("A", a2), ("B", b)])
    m = g.match(a1)
    assert m[0] == ("A", pytest.approx(0.5))
    assert m[1] == ("B", pytest.approx(0.0))


def test_outbox_keeps_messages_until_delivered(tmp_path):
    store = Store(tmp_path / "db")
    online = {"up": False}
    delivered = []

    def post(url, payload):
        if not online["up"]:
            raise OSError("network down")
        delivered.append(payload["text"])

    chat = GoogleChat("https://example/hook?key=1", store, post=post)
    chat.send("one", None, NOW)
    chat.send("two", None, NOW)
    assert delivered == [] and len(store.outbox()) == 2
    online["up"] = True
    chat.flush(NOW)
    assert delivered == ["one", "two"] and store.outbox() == []


def test_outbox_drops_messages_older_than_a_day(tmp_path):
    store = Store(tmp_path / "db")
    chat = GoogleChat("https://example/hook", store, post=lambda u, p: (_ for _ in ()).throw(OSError()))
    chat.send("old", None, NOW)
    chat.flush(NOW + dt.timedelta(days=2))
    assert store.outbox() == []


def test_config_loads_example_and_quoted_or_unquoted_times(tmp_path):
    cfg = config.load(__file__.rsplit("/tests/", 1)[0] + "/config.example.yaml")
    assert cfg.schedule.clocks() == (dt.time(9), dt.time(10), dt.time(11))
    p = tmp_path / "c.yaml"
    p.write_text("schedule:\n  window_start: 09:00\n  late_after: 10:00\n  window_end: 11:30\n")
    assert config.load(p).schedule.clocks() == (dt.time(9), dt.time(10), dt.time(11, 30))


def test_config_rejects_bad_schedule_and_typos(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text('schedule:\n  window_start: "10:00"\n  late_after: "09:00"\n')
    with pytest.raises(ValueError):
        config.load(p)
    p.write_text("camera:\n  ulr: rtsp://x\n")
    with pytest.raises(ValueError, match="ulr"):
        config.load(p)


def test_sweep_and_recommend_pick_safest_setting_with_no_wrong_matches():
    samples = [
        Sample("A", "A", 0.70, 0.20),
        Sample("A", "A", 0.50, 0.45),  # accepted once the threshold is low enough
        Sample("B", "A", 0.48, 0.47),  # would be a wrong match
        Sample(None, "B", 0.40, 0.10),  # stranger
    ]
    rows = sweep(samples, thresholds=[0.35, 0.45, 0.6], margins=[0.0, 0.04])
    best = recommend(rows)
    assert best.wrong == 0
    assert (best.threshold, best.margin, best.correct) == (0.45, 0.04, 2)


def test_sync_employees_handles_excel_bom_and_deactivates_removed(tmp_path):
    csv = tmp_path / "e.csv"
    csv.write_text("﻿id,name\nA,Ánh\nB,Bình\n", encoding="utf-8")
    store = Store(tmp_path / "db")
    store.sync_employees(csv)
    csv.write_text("id,name\nA,Ánh\n", encoding="utf-8")
    store.sync_employees(csv)
    assert store.employees() == {"A": "Ánh"}


def test_config_model_files_follow_backend(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("recognition:\n  backend: opencv\n")
    rc = config.load(p).recognition
    assert rc.model_files()[1] == "models/face_recognition_sface_2021dec.onnx"
    assert rc.model_id() == "face_recognition_sface_2021dec"
    p.write_text("recognition:\n  backend: dlib\n")
    with pytest.raises(ValueError, match="backend"):
        config.load(p)


def test_recommend_takes_middle_of_safe_range_not_its_edge():
    samples = [Sample("A", "A", 0.80, 0.10), Sample(None, "A", 0.30, 0.05)]
    best = recommend(sweep(samples, thresholds=[0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8], margins=[0.0]))
    # 0.2 and 0.3 accept the stranger; 0.4-0.8 are all safe, so take the middle.
    assert best.threshold == 0.6 and best.correct == 1 and best.employees == 1
