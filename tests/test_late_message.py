import datetime as dt

import pytest

from attendance import config
from attendance.web import create_app
from conftest import DAY, at
from test_service import run_until

D = dt.date(2026, 10, 15)


def late(app, emp, hh, mm, day=DAY):
    app.service.on_identified(emp, at(hh, mm, day=day), 0.8, None)


def test_message_mentions_each_late_person(core):
    app, clock, _ = core
    app.store.update_employee("B", "Bob", True, "111222333")
    clock.t = at(10, 40)
    late(app, "A", 9, 0)  # on time: not listed
    late(app, "B", 10, 15)
    assert app.service.late_text(DAY) == "Morning, coming late today: <users/111222333> (10:15)"
    late(app, "C", 10, 32)
    assert app.service.late_text(DAY) == \
        "Morning, coming late today: <users/111222333> (10:15), and @Carol (10:32)"  # Carol has no Chat ID
    assert app.service.late_text(DAY, preview=True) == "Morning, coming late today: @Bob (10:15), and @Carol (10:32)"

    app.cfg.late_message.intro = "Good morning! Late today:"
    app.cfg.late_message.item = "{mention} {minutes} min"
    assert app.service.late_text(DAY) == "Good morning! Late today: <users/111222333> 15 min, and @Carol 32 min"


def test_sent_once_at_the_set_time_as_its_own_message(core):
    app, clock, _ = core
    payloads = []
    app.chat._post = lambda url, payload: payloads.append(payload)
    run_until(app.service, clock, at(10, 20))
    late(app, "B", 10, 15)
    run_until(app.service, clock, at(10, 59))
    assert not any(p["text"].startswith("Morning") for p in payloads)
    run_until(app.service, clock, at(11, 30))
    morning = [p for p in payloads if p["text"].startswith("Morning")]
    assert len(morning) == 1 and "thread" not in morning[0]  # a message of its own, not in the day's thread
    assert app.store.notice_time(DAY, "late_message") is not None


def test_nobody_late_or_on_leave_sends_nothing(core):
    app, clock, sent = core
    app.store.replace_external(DAY, DAY, [], [("B", DAY, DAY, "leave", "")])
    clock.t = at(10, 30)
    late(app, "B", 10, 20)  # on leave but came in late: not listed
    run_until(app.service, clock, at(11, 30))
    assert not any(m.startswith("Morning") for m in sent)


def test_disabled_or_manual_only(core):
    app, clock, sent = core
    app.cfg.late_message.auto_send = False
    clock.t = at(10, 30)
    late(app, "B", 10, 20)
    run_until(app.service, clock, at(11, 30))
    assert not any(m.startswith("Morning") for m in sent)
    assert app.service.send_late_message(DAY, user="hr")
    assert sent[-1].startswith("Morning, coming late today: @Bob")


def test_alerts_mention_people_unless_switched_off(core):
    app, clock, sent = core
    app.store.update_employee("B", "Bob", True, "999")
    app.cfg.google_chat.mention_people = False
    clock.t = at(10, 30)
    late(app, "B", 10, 20)
    assert sent[-1] == "🟠 *Bob* arrived at 10:20, 20 min late."
    app.cfg.google_chat.mention_people = True
    late(app, "C", 10, 25)
    assert sent[-1] == "🟠 @Carol arrived at 10:25, 25 min late."


def test_config_checks_send_time_and_placeholders(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text('schedule:\n  late_after: "12:30"\n  window_end: "13:00"\n')
    assert config.load(p).late_message.send_at == ""  # default follows the camera hours, any late time works
    p.write_text('late_message:\n  send_at: "09:30"\n')
    with pytest.raises(ValueError, match="late time"):
        config.load(p)
    p.write_text('late_message:\n  item: "{who}"\n')
    with pytest.raises(ValueError, match="mention"):
        config.load(p)


def test_chat_ids_from_csv_and_employee_page(core):
    app, clock, _ = core
    app.store.import_employees("id,name,chat_user\nA,Alice,123\nD,Dan,\n")
    assert app.store.employee("A").chat_user == "123" and app.store.employee("D").chat_user == ""
    app.store.import_employees("id,name\nA,Alice Nguyen\n")
    assert app.store.employee("A").chat_user == "123"  # not wiped by a CSV without the column

    flask_app = create_app(app)
    flask_app.testing = True
    c = flask_app.test_client()
    c.post("/employees/B", data={"name": "Bob", "active": "on", "chat_user": " 456 "})
    assert app.store.employee("B").chat_user == "456"
    urls = []
    app.chat._post = lambda url, payload: urls.append(payload["text"])
    page = c.post("/employees/B/test-mention", follow_redirects=True).get_data(as_text=True)
    assert "Test sent" in page and urls == ["Test mention from the attendance system: <users/456>"]

    clock.t = at(10, 40)
    late(app, "B", 10, 15)
    page = c.get("/").get_data(as_text=True)
    assert "Morning, coming late today: @Bob (10:15)" in page
    page = c.post(f"/day/{DAY}/late-message", follow_redirects=True).get_data(as_text=True)
    assert "Late message posted" in page and urls[-1] == "Morning, coming late today: <users/456> (10:15)"


def test_send_time_must_be_after_the_late_time(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text('late_message:\n  send_at: "10:00"\n')  # equal to late_after: nobody is late yet at that moment
    with pytest.raises(ValueError, match="after the late time"):
        config.load(p)
    p.write_text('late_message:\n  send_at: "10:01"\n')
    assert config.load(p).late_message.send_at == "10:01"


def test_placeholders_cannot_reach_into_objects(tmp_path):
    for item in ("{mention.__class__}", "{mention.foo}", "{}", "{0}", "{name[0]}", "{mention"):
        with pytest.raises(ValueError, match="mention"):
            config.check_late_message_item(item)
    config.check_late_message_item("{mention} ({time}, {minutes:02d} min, {name})")


def test_message_about_another_day_starts_with_its_date(core):
    app, clock, sent = core
    clock.t = at(10, 40)
    late(app, "B", 10, 15)
    clock.t = at(9, 0, day=D)  # the next day
    assert app.service.late_text(DAY) == "Wednesday 14 Oct: Morning, coming late today: @Bob (10:15)"
    assert app.service.send_late_message(DAY, user="hr")
    assert sent[-1].startswith("Wednesday 14 Oct: ")


def test_nobody_late_is_remembered_but_not_shown_as_sent(core):
    app, clock, sent = core
    late(app, "A", 9, 0)
    run_until(app.service, clock, at(11, 30))
    assert app.store.notice_time(DAY, "late_message") is None
    assert app.store.notice_time(DAY, "late_message_skipped") is not None
    flask_app = create_app(app)
    flask_app.testing = True
    page = flask_app.test_client().get("/").get_data(as_text=True)
    assert "Nobody was late at 11:00, so nothing was sent" in page and "Sent at" not in page
    # The admin later finds someone the camera missed and can still send the message by hand.
    app.service.set_arrival(DAY, "B", dt.time(10, 20))
    assert app.service.send_late_message(DAY, user="hr") and sent[-1].startswith("Morning")
    run_until(app.service, clock, at(12, 0))
    assert len([m for m in sent if m.startswith("Morning")]) == 1


def test_automatic_send_does_not_repeat_a_manual_one(core):
    app, clock, sent = core
    clock.t = at(10, 30)
    late(app, "B", 10, 20)
    assert app.service.send_late_message(DAY, user="hr")
    run_until(app.service, clock, at(11, 30))
    assert len([m for m in sent if m.startswith("Morning")]) == 1
    assert app.store.mark_notice(DAY, "late_message", clock.t) is False  # already marked


def test_chat_ids_are_normalised_and_checked(core):
    app, _, _ = core
    app.store.update_employee("A", "Alice", True, " users/123 ")
    assert app.store.employee("A").chat_user == "123"
    app.store.update_employee("A", "Alice", True, "<users/456>")
    assert app.store.employee("A").chat_user == "456"
    with pytest.raises(ValueError, match="number"):
        app.store.update_employee("A", "Alice", True, "alice@example.com")
    assert app.store.employee("A").chat_user == "456"  # nothing changed
    with pytest.raises(ValueError, match="row B"):
        app.store.import_employees("id,name,chat_user\nB,Bob,bob\n")
    app.store.import_employees("id,name,chat_user\nB,Bob,users/789\n")
    assert app.store.employee("B").chat_user == "789"

    flask_app = create_app(app)
    flask_app.testing = True
    page = flask_app.test_client().post("/employees/C", data={"name": "Carol", "active": "on", "chat_user": "x"},
                                        follow_redirects=True).get_data(as_text=True)
    assert "is a number" in page


def test_picture_of_the_late_people(core):
    import cv2
    from test_evidence import seen

    from attendance import latephoto

    app, clock, _ = core
    payloads = []
    app.chat._post = lambda url, payload: payloads.append(payload)
    clock.t = at(10, 40)
    seen(app, "A", at(9, 0))  # on time: not in the picture
    late(app, "C", 10, 32)  # entered by hand: no photo
    assert app.service.late_photo(DAY) is None  # nobody on camera: no picture
    seen(app, "B", at(10, 15))  # camera: the whole person was kept
    rel = app.service.late_photo(DAY)
    assert rel == f"late/{DAY}.jpg"
    picture = cv2.imread(str(app.data_dir / rel))
    assert picture.shape[1] == 2 * latephoto.TILE_W + 3 * latephoto.GAP  # two people side by side
    first = (app.data_dir / rel).stat().st_mtime_ns

    # Unchanged arrivals: the same file. A correction: the picture is made again.
    assert app.service.late_photo(DAY) == rel and (app.data_dir / rel).stat().st_mtime_ns == first
    app.service.delete_arrival(DAY, "C")
    assert app.service.late_photo(DAY) == rel
    assert cv2.imread(str(app.data_dir / rel)).shape[1] == latephoto.TILE_W + 2 * latephoto.GAP

    # A plain http address (the office network): Google Chat cannot fetch it, so the message links to it.
    assert app.service.send_late_message(DAY, user="hr")
    p = payloads[-1]
    assert "cardsV2" not in p
    assert p["text"].startswith("Morning, coming late today: @Bob (10:15)\n📷 <http://pc:8000/late-photo/2026-10-14/")
    # An https address: the picture is shown in the message, under the text.
    app.cfg.web.public_url = "https://office.example.ts.net"
    assert app.service.send_late_message(DAY, user="hr")
    p = payloads[-1]
    assert p["text"] == "Morning, coming late today: @Bob (10:15)"
    image = p["cardsV2"][0]["card"]["sections"][0]["widgets"][0]["image"]
    assert image["imageUrl"].startswith(f"https://office.example.ts.net/late-photo/{DAY}/")
    # Switched off: text only, and no picture is made.
    app.cfg.late_message.photo = False
    assert app.service.late_photo(DAY) is None and app.service.late_photo_url(DAY) is None
    assert app.service.send_late_message(DAY, user="hr")
    assert "cardsV2" not in payloads[-1] and "late-photo" not in payloads[-1]["text"]


def test_picture_is_served_by_its_token_without_the_password(core):
    from test_evidence import seen
    from test_web import client

    app, clock, _ = core
    clock.t = at(10, 40)
    seen(app, "B", at(10, 15))
    url = app.service.late_photo_url(DAY)
    assert url.startswith("http://pc:8000/late-photo/")
    path = url[len("http://pc:8000"):]
    c = client(app, password="s3cret")
    assert c.get("/").status_code == 401
    resp = c.get(path)
    assert resp.status_code == 200 and resp.mimetype == "image/jpeg" and resp.data[:2] == b"\xff\xd8"
    token = app.service.late_photo_token(DAY)
    assert c.get(f"/late-photo/{DAY}/{'0' * 32}.jpg").status_code == 404  # wrong token
    assert c.get(f"/late-photo/{D}/{app.service.late_photo_token(D)}.jpg").status_code == 404  # nobody late that day
    assert c.get(f"/late-photo/not-a-day/{token}.jpg").status_code == 404
    # The Today page shows the picture and says it will only be linked from an http address.
    page = c.get("/", headers={"Authorization": "Basic YWRtaW46czNjcmV0"}).get_data(as_text=True)
    assert f'<img src="{url}"' in page and "only link to this picture" in page


def test_pictures_of_late_people_follow_the_evidence_retention(core):
    from test_evidence import seen

    from attendance import latephoto

    app, clock, _ = core
    old = DAY - dt.timedelta(days=100)  # within late_evidence_days
    ancient = DAY - dt.timedelta(days=400)  # past it
    seen(app, "B", at(10, 20, day=old))
    seen(app, "C", at(10, 30, day=ancient))
    assert app.service.late_photo(old) and app.service.late_photo(ancient)
    clock.t = at(8, 0)
    app.service.tick()  # runs the daily cleanup
    assert (app.data_dir / latephoto.photo_path(old)).exists()
    assert not (app.data_dir / latephoto.photo_path(ancient)).exists()
    assert not (app.data_dir / latephoto.photo_path(ancient)).with_suffix(".key").exists()


def test_labels_are_drawn_without_accents():
    from attendance.latephoto import ascii_label

    assert ascii_label("Nguyễn Văn Đức") == "Nguyen Van Duc"
    assert ascii_label("Trần Thị Hồng đào") == "Tran Thi Hong dao"
    assert ascii_label("日本") == "?"
