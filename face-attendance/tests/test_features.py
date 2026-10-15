import datetime as dt
import json

import numpy as np
import pytest

from attendance import settings
from attendance.live import FrameHealth
from attendance.pipeline import App
from conftest import DAY, at, face_image, person_vector
from test_service import run_until

SATURDAY = dt.date(2026, 10, 17)


# holidays and leave ---------------------------------------------------------------

def test_holiday_sends_nothing_and_extra_workday_counts(env):
    service, store, clock, sent = env
    store.replace_external(DAY, SATURDAY, [(DAY, "holiday", "National day"), (SATURDAY, "workday", "Make-up day")], [])
    run_until(service, clock, at(12, 0), step=dt.timedelta(minutes=5))
    assert sent == []
    assert service.report(DAY).day_note == "Holiday: National day"

    clock.t = at(8, 0, day=SATURDAY)
    run_until(service, clock, at(10, 1, day=SATURDAY))
    assert "3/3 people not seen yet" in sent[-1]


def test_people_on_leave_are_not_late_or_missing(env):
    service, store, clock, sent = env
    store.replace_external(DAY, DAY, [], [("B", DAY - dt.timedelta(days=1), DAY + dt.timedelta(days=1), "business_trip", "Hanoi")])
    run_until(service, clock, at(10, 1))
    deadline = sent[-1]
    assert "1/2 people not seen yet" not in deadline and "2/2 people not seen yet" in deadline
    assert "• Bob" not in deadline and "🏖 On leave: Bob (Business trip)" in deadline
    clock.t = at(10, 30)
    service.on_identified("B", at(10, 30), 0.8, None)  # came in anyway: no "late" message
    assert not sent[-1].startswith("🟠")


# corrections --------------------------------------------------------------------

def test_admin_can_add_move_and_delete_arrivals_with_audit_trail(env):
    service, store, clock, sent = env
    run_until(service, clock, at(10, 5))
    service.on_identified("A", at(9, 10), 0.8, "crops/x.jpg")

    # The camera thought it was Alice, but it was Carol.
    service.reassign_arrival(DAY, "A", "C", user="hr")
    assert set(store.checkins(DAY)) == {"C"}
    assert store.checkins(DAY)["C"].image == "crops/x.jpg"
    assert sent[-1] == "✅ Update: *Carol* arrived at 09:10 (corrected by admin), not late."

    # Bob forgot to look at the camera; HR enters his arrival.
    service.set_arrival(DAY, "B", dt.time(9, 45), user="hr")
    assert store.checkins(DAY)["B"].source == "manual"
    service.set_arrival(DAY, "B", dt.time(10, 20), user="hr")  # changing to a later time is allowed
    assert store.checkins(DAY)["B"].first_seen == at(10, 20)

    service.delete_arrival(DAY, "C", user="hr")
    assert "C" not in store.checkins(DAY)
    actions = [a for _, who, a, _ in store.audit() if who == "hr"]
    assert actions == ["delete arrival", "set arrival", "set arrival", "reassign arrival"]
    with pytest.raises(ValueError):
        service.delete_arrival(DAY, "C")


def test_messages_about_past_days_carry_the_date(env):
    service, store, clock, sent = env
    run_until(service, clock, at(10, 1))
    clock.t = at(9, 0, day=DAY + dt.timedelta(days=1))
    service.set_arrival(DAY, "B", dt.time(9, 30))
    assert sent[-1] == "✅ Update: *Bob* arrived at 09:30 on 2026-10-14 (entered by admin), not late."


def test_unclear_face_of_someone_already_checked_in_is_not_reported(env):
    service, store, clock, _ = env
    service.on_identified("A", at(9, 5), 0.8, None)
    assert service.on_unknown(at(9, 30), None, None, "A", 0.35) is None
    assert service.on_unknown(at(9, 31), None, None, "B", 0.35) is not None


# retention ----------------------------------------------------------------------

def test_old_snapshots_are_deleted_but_reference_photos_kept(core):
    app, clock, _ = core
    data = app.data_dir
    old_day, new_day = DAY - dt.timedelta(days=100), DAY - dt.timedelta(days=5)
    for day in (old_day, new_day):
        (data / "crops" / day.isoformat()).mkdir(parents=True)
        (data / "crops" / day.isoformat() / "a.jpg").write_bytes(b"x")
        app.store.record_checkin(day, "A", at(9, 0, day=day), "camera", 0.8, f"crops/{day}/a.jpg")
        app.store.add_unknown(day, at(9, 0, day=day), f"crops/{day}/a.jpg", "m", None, None, None)
    (data / "faces" / "A").mkdir(parents=True)
    (data / "faces" / "A" / "ref.jpg").write_bytes(b"x")

    clock.t = at(8, 0)
    app.service.tick()
    assert not (data / "crops" / old_day.isoformat()).exists()
    assert (data / "crops" / new_day.isoformat() / "a.jpg").exists()
    assert (data / "faces" / "A" / "ref.jpg").exists()
    assert app.store.checkins(old_day)["A"].image is None  # the arrival record itself is kept
    assert [u.ts.date() for u in app.store.unknowns(status=None)] == [new_day]


# settings -----------------------------------------------------------------------

def test_settings_apply_live_persist_and_reject_invalid_values(core):
    app, clock, _ = core
    settings.save(app.cfg, app.store, {"schedule": {"late_after": "09:30"}, "recognition": {"accept_score": "0.5"}})
    assert app.service.window(DAY).late_after == at(9, 30)
    assert app.cfg.recognition.accept_score == 0.5

    with pytest.raises(ValueError):
        settings.save(app.cfg, app.store, {"schedule": {"late_after": "12:00"}})  # after window_end
    assert app.cfg.schedule.late_after == "09:30"
    with pytest.raises(ValueError):
        settings.save(app.cfg, app.store, {"recognition": {"backend": "opencv"}})  # needs a restart

    fresh = App(type(app.cfg)(employees_file="employees.csv", data_dir="data", base_dir=app.cfg.base_dir))
    assert fresh.cfg.schedule.late_after == "09:30"
    assert json.loads(app.store.get("settings"))["recognition"]["accept_score"] == "0.5"


# reference photos -----------------------------------------------------------------

def test_upload_learn_delete_and_look_alike_alert(core):
    app, clock, sent = core
    lib = app.library
    result = lib.add_photo("A", face_image(1))
    assert result.ok and result.warnings == []
    refs = app.store.references(lib.model_id, "A")
    assert refs[-1].image.startswith("faces/A/") and (app.data_dir / refs[-1].image).exists()
    assert app.gallery.counts()["A"] == 2

    assert not lib.add_photo("A", face_image(0)).ok  # no face

    # Bob's folder accidentally holds a photo of Alice.
    result = lib.add_photo("B", face_image(1))
    assert result.ok and "looks like Alice" in result.warnings[0]
    assert sent[-1].startswith("⚠️ Look-alike reference photos: Bob and Alice")
    assert [(p.a, p.b) for p in lib.look_alikes()] == [("A", "B")]

    wrong = app.store.references(lib.model_id, "B")[-1].image
    lib.delete_photo(wrong)
    assert not (app.data_dir / wrong).exists()
    assert lib.look_alikes() == []


def test_switching_model_reembeds_saved_photos(core):
    app, clock, _ = core
    app.library.add_photo("C", face_image(3))
    app.cfg.recognition.recognizer_model = "models/other_model.onnx"
    assert app.library.model_id == "other_model"
    assert app.library.reembed_missing() == 1
    assert app.gallery.counts() == {"C": 1}
    assert app.library.reembed_missing() == 0


def test_delete_employee_removes_face_data(core):
    app, _, _ = core
    app.library.add_photo("C", face_image(3))
    app.library.delete_employee("C")
    assert app.store.employee("C") is None
    assert not (app.data_dir / "faces" / "C").exists()
    assert "C" not in app.gallery.counts()


# camera health ------------------------------------------------------------------

def test_frame_health_detects_black_blank_and_frozen_images():
    rng = np.random.default_rng(0)
    h = FrameHealth(bad_after_s=60)
    black = np.zeros((480, 640, 3), np.uint8)
    assert h.update(black, 0) is None and h.update(black, 59) is None
    assert h.update(black, 61) == "image is black"

    h = FrameHealth(bad_after_s=60)
    still = rng.integers(0, 255, (480, 640, 3), dtype=np.uint8)
    for t in range(0, 70, 5):
        problem = h.update(still.copy(), t)
    assert problem == "image is frozen"

    h = FrameHealth(bad_after_s=60)
    for t in range(0, 120, 5):  # a normal, slightly noisy scene
        assert h.update(rng.integers(0, 255, (480, 640, 3), dtype=np.uint8), t) is None


# period report ------------------------------------------------------------------

def test_period_report_counts_late_not_seen_and_leave(env):
    service, store, clock, _ = env
    monday, tuesday = dt.date(2026, 10, 12), dt.date(2026, 10, 13)
    store.replace_external(monday, DAY, [], [("C", tuesday, tuesday, "leave", "")])
    for day in (monday, tuesday, DAY):
        store.mark_notice(day, "camera_ok", at(9, 0, day=day))
    store.record_checkin(dt.date(2026, 10, 9), "A", at(9, 1, day=dt.date(2026, 10, 9)), "manual", None, None)
    for day, emp, hh, mm in [(monday, "A", 9, 5), (monday, "B", 10, 15), (tuesday, "A", 10, 30), (DAY, "A", 9, 0)]:
        store.record_checkin(day, emp, at(hh, mm, day=day), "camera", 0.8, None)
    clock.t = at(9, 30)  # before today's late time: today is left out
    rep = service.period_report(dt.date(2026, 10, 9), DAY)
    assert [d.day for d in rep.days] == [dt.date(2026, 10, 9), monday, tuesday]  # the weekend is skipped
    assert [d.day for d in rep.no_data_days] == [dt.date(2026, 10, 9)]  # before the system ran
    people = {p.employee_id: p for p in rep.people}
    assert (people["A"].on_time, people["A"].late, people["A"].late_minutes) == (2, 1, 30)  # entered arrival counts
    assert (people["B"].late, people["B"].not_seen, people["B"].late_minutes) == (1, 1, 15)
    assert (people["C"].not_seen, people["C"].on_leave, people["C"].work_days) == (1, 1, 1)
    assert rep.people[0].employee_id == "B"  # most late + not seen first

    clock.t = at(10, 30)
    assert service.period_report(DAY, DAY).days[0].day == DAY


def test_days_without_camera_data_are_flagged(env):
    service, store, clock, _ = env
    clock.t = at(9, 30)
    assert service.report(DAY).no_camera_data  # camera hours started but no frame yet
    service.tick(camera_ok=True)
    assert not service.report(DAY).no_camera_data
    assert service.report(DAY - dt.timedelta(days=1)).no_camera_data
    assert not service.report(DAY + dt.timedelta(days=1)).no_camera_data  # the future is not "missing"


def test_correcting_a_wrong_sighting_brings_back_the_real_later_arrival(env):
    """Alice was taken for Bob at 9:20; Bob really came at 10:15. Before sightings were kept,
    Bob's real arrival was dropped (he 'already had' 9:20) and moving 9:20 to Alice left him
    'not seen'. Now his arrival is recomputed from his remaining sightings: late."""
    service, store, clock, sent = env
    run_until(service, clock, at(10, 20))
    service.on_identified("B", at(9, 20), 0.6, "crops/alice.jpg")   # wrong: it was Alice
    service.on_identified("B", at(10, 15), 0.9, "crops/bob.jpg")    # Bob himself
    assert store.checkins(DAY)["B"].first_seen == at(9, 20)
    assert len(store.sightings(DAY, "B")) == 2

    service.reassign_arrival(DAY, "B", "A", user="hr")
    arrivals = store.checkins(DAY)
    assert arrivals["A"].first_seen == at(9, 20) and arrivals["A"].source == "corrected"
    assert arrivals["B"].first_seen == at(10, 15) and arrivals["B"].image == "crops/bob.jpg"
    assert sent[-2:] == ["🟠 @Bob arrived at 10:15, 15 min late (corrected by admin).",
                         "✅ Update: *Alice* arrived at 09:20 (corrected by admin), not late."]
    assert [d.employee_id for d in service.report(DAY).late] == ["B"]


def test_rejecting_and_restoring_single_sightings(env):
    service, store, clock, sent = env
    run_until(service, clock, at(10, 20))
    service.on_identified("C", at(9, 5), 0.5, None)    # a visitor taken for Carol
    service.on_identified("C", at(9, 40), 0.9, None)   # Carol
    first = store.sightings(DAY, "C")[0]
    service.reject_sighting(first.id, user="hr")
    assert store.checkins(DAY)["C"].first_seen == at(9, 40)
    assert [s.status for s in store.sightings(DAY, "C")] == ["rejected", "ok"]
    service.restore_sighting(first.id, user="hr")
    assert store.checkins(DAY)["C"].first_seen == at(9, 5)

    # Deleting the arrival removes everything so far; a later real sighting counts again.
    service.delete_arrival(DAY, "C", user="hr")
    assert "C" not in store.checkins(DAY)
    service.on_identified("C", at(10, 25), 0.9, None)
    assert store.checkins(DAY)["C"].first_seen == at(10, 25)


def test_typed_arrival_time_wins_over_the_camera(env):
    service, store, clock, sent = env
    run_until(service, clock, at(10, 20))
    service.on_identified("A", at(9, 5), 0.6, None)
    service.set_arrival(DAY, "A", dt.time(10, 5), user="hr")
    service.on_identified("A", at(9, 50), 0.9, None)   # later camera sightings do not undo it
    assert store.checkins(DAY)["A"].first_seen == at(10, 5)
    service.set_arrival(DAY, "A", dt.time(9, 30), user="hr")
    assert store.checkins(DAY)["A"].first_seen == at(9, 30)


def test_old_databases_get_their_arrivals_as_sightings(tmp_path):
    import sqlite3

    from attendance.store import Store

    db = sqlite3.connect(tmp_path / "a.db")
    db.execute("CREATE TABLE checkins (day TEXT NOT NULL, employee_id TEXT NOT NULL, first_seen TEXT NOT NULL, "
               "source TEXT NOT NULL, score REAL, image TEXT, PRIMARY KEY (day, employee_id))")
    db.execute("INSERT INTO checkins VALUES (?, 'A', ?, 'camera', 0.8, 'crops/a.jpg')",
               (DAY.isoformat(), at(9, 10).isoformat()))
    db.commit()
    db.close()
    store = Store(tmp_path / "a.db")
    [s] = store.sightings(DAY, "A")
    assert s.ts == at(9, 10) and s.image == "crops/a.jpg"
    assert store.record_checkin(DAY, "A", at(9, 30), "camera", 0.9, None) == "unchanged"


def test_learning_skips_faces_nearly_identical_to_a_kept_one(core):
    from conftest import person_vector

    app, clock, _ = core
    lib, model = app.library, app.cfg.recognition.model_id()
    assert not lib.learn("A", person_vector(1), None, model)  # same as Alice's reference
    other = person_vector(1) + 0.5 * person_vector(50)
    assert lib.learn("A", other / np.linalg.norm(other), None, model)
    assert app.gallery.counts()["A"] == 2
