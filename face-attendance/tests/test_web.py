import base64
import datetime as dt
import io

import numpy as np

from attendance.web import create_app
from conftest import DAY, at


def client(core_app, password=""):
    core_app.cfg.web.password = password
    app = create_app(core_app)
    app.testing = True
    return app.test_client()


def test_pages_render_and_admin_can_assign_unknown(core):
    app, clock, sent = core
    service, store = app.service, app.store
    clock.t = at(10, 30)
    service.on_identified("A", at(9, 5), 0.7, "crops/a.jpg")
    uid = service.on_unknown(at(9, 20), "crops/u.jpg", np.ones(512, dtype=np.float32), "B", 0.4)
    c = client(app)

    page = c.get("/").get_data(as_text=True)
    assert "Alice" in page and "09:05" in page and "Not seen: 2" in page

    page = c.get("/unknowns").get_data(as_text=True)
    assert "Closest match: Bob" in page

    resp = c.post(f"/unknowns/{uid}/assign", data={"employee_id": "B"})
    assert resp.status_code == 302
    assert "B" in store.checkins(DAY)
    again = c.post(f"/unknowns/{uid}/assign", data={"employee_id": "B"}, follow_redirects=True)
    assert "already been handled" in again.get_data(as_text=True)


def test_password_protects_pages(core):
    app, _, _ = core
    c = client(app, password="s3cret")
    assert c.get("/").status_code == 401
    token = base64.b64encode(b"admin:s3cret").decode()
    assert c.get("/", headers={"Authorization": f"Basic {token}"}).status_code == 200


def png(image) -> bytes:
    import cv2

    return cv2.imencode(".png", image)[1].tobytes()


def test_employee_management_and_photo_upload(core):
    from conftest import face_image

    app, _, sent = core
    c = client(app)
    resp = c.post("/employees", data={"id": "D", "name": "Dan"}, follow_redirects=True)
    assert "Employee added" in resp.get_data(as_text=True)
    assert "none: cannot be recognised" in c.get("/employees").get_data(as_text=True)

    resp = c.post("/employees/D/photos", data={"photos": [(io.BytesIO(png(face_image(4))), "d1.png"),
                                                          (io.BytesIO(png(face_image(0))), "empty.png"),
                                                          (io.BytesIO(b"not an image"), "x.txt")]},
                  content_type="multipart/form-data", follow_redirects=True)
    page = resp.get_data(as_text=True)
    assert "1 photo(s) added" in page and "empty.png: No face found" in page and "x.txt: not a readable image" in page
    image = app.store.references(app.library.model_id, "D")[0].image
    assert c.get(f"/media/{image}").status_code == 200

    # A photo of Alice uploaded for Dan triggers the look-alike warning.
    resp = c.post("/employees/D/photos", data={"photos": [(io.BytesIO(png(face_image(1))), "oops.png")]},
                  content_type="multipart/form-data", follow_redirects=True)
    assert "looks like Alice" in resp.get_data(as_text=True)
    assert "have reference photos that look" in c.get("/employees").get_data(as_text=True)

    c.post("/employees/D/photos/delete", data={"image": image})
    assert not (app.data_dir / image).exists()

    c.post("/employees/D", data={"name": "Daniel"})  # unticked "active"
    assert app.store.employee("D").name == "Daniel" and not app.store.employee("D").active
    c.post("/employees/D/delete", data={"confirm": "wrong"})
    assert app.store.employee("D") is not None
    c.post("/employees/D/delete", data={"confirm": "D"})
    assert app.store.employee("D") is None

    resp = c.post("/employees/import", data={"file": (io.BytesIO("id,name\nE,Eve\n".encode()), "e.csv")},
                  content_type="multipart/form-data", follow_redirects=True)
    assert "Imported 1 employees" in resp.get_data(as_text=True)


def test_attendance_corrections_from_the_page(core):
    app, clock, _ = core
    c = client(app)
    clock.t = at(10, 30)
    app.service.on_identified("A", at(9, 5), 0.8, None)
    c.post(f"/day/{DAY}/A/reassign", data={"to": "B"})
    c.post(f"/day/{DAY}/C/set", data={"time": "09:20"})
    resp = c.post(f"/day/{DAY}/C/set", data={"time": "nine"}, follow_redirects=True)
    assert "HH:MM" in resp.get_data(as_text=True)
    assert set(app.store.checkins(DAY)) == {"B", "C"}
    c.post(f"/day/{DAY}/B/delete")
    assert set(app.store.checkins(DAY)) == {"C"}
    assert "reassign arrival" in c.get("/audit").get_data(as_text=True)


def test_calendar_page(core):
    app, _, _ = core
    c = client(app)
    c.post("/calendar/days", data={"start": "2027-02-05", "end": "2027-02-07", "kind": "holiday", "note": "Tet"})
    assert app.store.calendar_day(dt.date(2027, 2, 6)) == ("holiday", "Tet")
    resp = c.post("/calendar/days", data={"start": "2027-02-07", "end": "2027-02-05", "kind": "holiday"},
                  follow_redirects=True)
    assert "end date" in resp.get_data(as_text=True)
    c.post("/calendar/leaves", data={"employee_id": "A", "start": str(DAY), "kind": "sick", "note": ""})
    assert app.store.leaves_on(DAY) == {"A": "sick"}
    page = c.get("/calendar").get_data(as_text=True)
    assert "Tet" in page and "Sick" in page
    c.post(f"/calendar/leaves/{app.store.leaves()[0].id}/delete")
    c.post("/calendar/days/2027-02-06/delete")
    assert app.store.leaves_on(DAY) == {} and app.store.calendar_day(dt.date(2027, 2, 6)) is None


def test_settings_page_saves_and_keeps_password(core):
    app, _, _ = core
    c = client(app)
    assert "Working hours" in c.get("/settings").get_data(as_text=True)
    form = {"schedule.window_start": "08:30", "schedule.late_after": "09:30", "schedule.window_end": "11:00",
            "schedule.work_days": ["0", "1", "2", "3", "4", "5"], "camera.url": "rtsp://cam", "camera.process_fps": "4",
            "recognition.accept_score": "0.5", "recognition.min_margin": "0.08", "recognition.votes_required": "3",
            "recognition.unknown_min_frames": "3", "recognition.min_face_px": "60", "recognition.min_det_score": "0.65",
            "recognition.min_sharpness": "40", "recognition.max_yaw": "35", "recognition.max_pitch": "30",
            "google_chat.webhook_url": "https://hook", "google_chat.unknown_batch_minutes": "5",
            "web.public_url": "http://pc:8000", "web.password": "", "retention_days": "30", "healthcheck_url": ""}
    resp = c.post("/settings", data=form, follow_redirects=True)
    assert "Settings saved" in resp.get_data(as_text=True)
    cfg = app.cfg
    assert (cfg.schedule.late_after, cfg.schedule.work_days, cfg.camera.url, cfg.retention_days) == \
        ("09:30", [0, 1, 2, 3, 4, 5], "rtsp://cam", 30)
    assert cfg.camera.enhance_low_light is False and cfg.web.password == ""  # unticked box; empty password kept

    resp = c.post("/settings", data={**form, "schedule.late_after": "07:00"}, follow_redirects=True)
    assert "Not saved" in resp.get_data(as_text=True) and cfg.schedule.late_after == "09:30"


def test_live_page_and_preview(core):
    app, _, _ = core
    c = client(app)
    assert "Live camera" in c.get("/live").get_data(as_text=True)
    assert c.get("/live.jpg").status_code == 404
    app.live.set_frame(b"\xff\xd8jpeg")
    assert c.get("/live.jpg").data == b"\xff\xd8jpeg"
    c.post("/live/preview", data={"action": "start"})
    status = c.get("/live.json").get_json()
    assert status["previewing"] is True and app.live.wants_frame()
    c.post("/live/preview", data={"action": "stop"})
    assert c.get("/live.json").get_json()["previewing"] is False


def test_media_and_cross_site_posts_are_restricted(core):
    app, _, _ = core
    c = client(app)
    assert c.get("/media/attendance.db").status_code == 404
    assert c.get("/media/crops/../attendance.db").status_code == 404
    assert c.post("/employees", data={"id": "X", "name": "X"}, headers={"Origin": "http://evil.example"}).status_code == 403
    assert app.store.employee("X") is None
