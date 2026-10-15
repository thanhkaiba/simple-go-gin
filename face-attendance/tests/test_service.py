import datetime as dt

import numpy as np
import pytest

from attendance.gallery import Gallery
from attendance.service import AttendanceService
from conftest import DAY, at, person_vector


def run_until(service, clock, end, step=dt.timedelta(seconds=30), camera_ok=True):
    while clock.t < end:
        clock.t += step
        service.tick(camera_ok)


def test_on_time_seen_again_after_deadline_is_not_late(env):
    service, _, clock, sent = env
    run_until(service, clock, at(9, 20))
    service.on_identified("A", at(9, 15), 0.7, None)
    run_until(service, clock, at(10, 30))
    service.on_identified("A", at(10, 25), 0.7, None)  # A walks past again

    rep = service.report(DAY)
    assert [a.employee_id for a in rep.on_time] == ["A"]
    assert rep.on_time[0].time == "09:15"
    assert not any("Alice" in m and "late" in m for m in sent)


def test_not_seen_by_deadline_is_listed_then_late_arrival_announced(env):
    service, _, clock, sent = env
    run_until(service, clock, at(9, 30))
    service.on_identified("A", at(9, 25), 0.7, None)
    run_until(service, clock, at(10, 0, 30))

    deadline = [m for m in sent if m.startswith("⏰")]
    assert len(deadline) == 1
    assert "2/3 people not seen yet" in deadline[0]
    assert "• @Bob" in deadline[0] and "• @Carol" in deadline[0] and "Alice" not in deadline[0]

    clock.t = at(10, 23)
    service.on_identified("B", at(10, 23), 0.7, None)
    assert sent[-1] == "🟠 @Bob arrived at 10:23, 23 min late."

    run_until(service, clock, at(11, 1))
    summary = sent[-1]
    assert summary.startswith("📋")
    assert "On time: 1 · Late: 1 · Not seen: 1" in summary
    assert "• Bob: 10:23 (23 min late)" in summary
    assert "• Carol" in summary


def test_notices_are_sent_once_even_after_restart(env, tmp_path):
    service, store, clock, sent = env
    run_until(service, clock, at(10, 5))
    restarted = AttendanceService(service.cfg, store, service.chat, Gallery(), now=clock)
    run_until(restarted, clock, at(10, 10))
    assert len([m for m in sent if m.startswith("⏰")]) == 1


def test_unknown_faces_are_batched_and_admin_can_resolve(env):
    service, store, clock, sent = env
    run_until(service, clock, at(9, 10))
    carol_turned = person_vector(3) + 0.5 * person_vector(40)  # Carol, but not like her reference photo
    uid = service.on_unknown(at(9, 12), "x.jpg", carol_turned / np.linalg.norm(carol_turned), "C", 0.38)
    service.on_unknown(at(9, 13), "y.jpg", person_vector(20), None, None)
    run_until(service, clock, at(9, 20))
    unknown_msgs = [m for m in sent if m.startswith("❓")]
    assert len(unknown_msgs) == 1 and "2 different faces" in unknown_msgs[0] and "/unknowns" in unknown_msgs[0]

    run_until(service, clock, at(10, 5))
    assert "Carol" in [m for m in sent if m.startswith("⏰")][0]

    # Admin confirms the 9:12 face was C: on time, with a correction in the chat.
    service.resolve_unknown(uid, "C")
    assert sent[-1] == "✅ Update: *Carol* arrived at 09:12 (confirmed by admin), not late."
    rep = service.report(DAY)
    assert "C" in [a.employee_id for a in rep.on_time]
    assert service.gallery.counts()["C"] == 2  # the confirmed face was learned
    assert [r.source for r in store.references(service.model_id, "C")] == ["upload", "admin"]
    assert store.embeddings("some-other-model") == []
    with pytest.raises(ValueError):
        service.resolve_unknown(uid, "C")


def test_admin_correction_moves_late_arrival_earlier(env):
    service, store, clock, sent = env
    run_until(service, clock, at(10, 30))
    service.on_identified("B", at(10, 20), 0.7, None)
    uid = service.on_unknown(at(9, 40), "b.jpg", None, "B", 0.2)  # too unlike B to be suppressed
    service.resolve_unknown(uid, "B")
    assert store.checkins(DAY)["B"].first_seen == at(9, 40)
    assert "B" in [a.employee_id for a in service.report(DAY).on_time]


def test_no_notices_on_weekend(env):
    service, _, clock, sent = env
    saturday = dt.date(2026, 10, 17)
    clock.t = at(8, 0, day=saturday)
    run_until(service, clock, at(12, 0, day=saturday), step=dt.timedelta(minutes=5))
    assert sent == []


def test_camera_outage_is_reported(env):
    service, _, clock, sent = env
    run_until(service, clock, at(9, 30))
    run_until(service, clock, at(9, 50), camera_ok=False)
    run_until(service, clock, at(10, 1))
    deadline = [m for m in sent if m.startswith("⏰")][0]
    assert "was down: 09:30–09:50" in deadline


def test_system_started_late_reports_the_gap(env):
    service, _, clock, sent = env
    clock.t = at(9, 40)  # PC switched on at 9:40
    run_until(service, clock, at(10, 1))
    assert "was down: 08:00–09:40" in [m for m in sent if m.startswith("⏰")][0]


def test_pc_off_all_morning_sends_only_summary_with_warning(env):
    service, _, clock, sent = env
    clock.t = at(14, 0)
    service.tick()
    assert len(sent) == 1 and sent[0].startswith("📋")
    assert "was down: 08:00–11:00" in sent[0]


def test_messages_use_one_thread_per_day(env):
    service, store, clock, _ = env
    payloads = []
    service.chat._post = lambda url, payload: payloads.append((url, payload))
    run_until(service, clock, at(10, 1))
    url, payload = payloads[0]
    assert payload["thread"] == {"threadKey": "attendance-2026-10-14"}
    assert url.endswith("&messageReplyOption=REPLY_MESSAGE_FALLBACK_TO_NEW_THREAD")
