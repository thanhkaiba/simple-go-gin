"""Admin web page: attendance, corrections, employees and photos, calendar, live camera, settings."""
from __future__ import annotations

import datetime as dt
import hmac
import secrets
import time
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from flask import (Flask, Response, abort, flash, jsonify, redirect, render_template, request,
                   send_from_directory, url_for)

from . import settings
from .service import SOURCES, minutes_late
from .store import LEAVE_KINDS

if TYPE_CHECKING:
    from .pipeline import App

WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MAX_UPLOAD_MB = 50


def _date(value: str | None, default: dt.date) -> dt.date:
    if not value:
        return default
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        abort(400, "dates must be YYYY-MM-DD")


def _time(value: str) -> dt.time:
    try:
        return dt.time.fromisoformat(value)
    except ValueError:
        raise ValueError("enter the time as HH:MM") from None


def create_app(core: "App") -> Flask:
    cfg, store, service, library, live = core.cfg, core.store, core.service, core.library, core.live
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_MB * 1024 * 1024
    secret = store.get("web_secret")
    if not secret:
        secret = secrets.token_hex(32)
        store.put("web_secret", secret)
    app.secret_key = secret

    def user() -> str:
        auth = request.authorization
        return auth.username if cfg.web.password and auth and auth.username else "admin"

    @app.before_request
    def guard():
        if cfg.web.password:
            auth = request.authorization
            if not (auth and hmac.compare_digest(auth.username or "", cfg.web.username)
                    and hmac.compare_digest(auth.password or "", cfg.web.password)):
                return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="attendance"'})
        if request.method == "POST":
            # Reject form posts coming from other websites (cross-site request forgery).
            origin = request.headers.get("Origin") or request.headers.get("Referer")
            if origin and urlparse(origin).netloc != request.host:
                abort(403)
        return None

    @app.context_processor
    def common():
        return {"pending": len(store.unknowns()), "nav": request.endpoint, "SOURCES": SOURCES,
                "LEAVE_KINDS": LEAVE_KINDS, "WEEKDAYS": WEEKDAYS}

    def done(message: str, endpoint: str, **kwargs):
        flash(message, "ok")
        return redirect(url_for(endpoint, **kwargs))

    def failed(message: str, endpoint: str, **kwargs):
        flash(message, "error")
        return redirect(url_for(endpoint, **kwargs))

    # attendance --------------------------------------------------------------------

    @app.get("/")
    def index():
        day = _date(request.args.get("day"), service.now().date())
        rep = service.report(day)
        return render_template("index.html", rep=rep, employees=store.employees(),
                               late=lambda a: minutes_late(a, rep.window), today=service.now().date())

    @app.post("/day/<day>/<employee_id>/set")
    def set_arrival(day: str, employee_id: str):
        d = _date(day, service.now().date())
        try:
            service.set_arrival(d, employee_id, _time(request.form.get("time", "")), user())
        except ValueError as exc:
            return failed(str(exc), "index", day=day)
        return done("Arrival saved.", "index", day=day)

    @app.post("/day/<day>/<employee_id>/reassign")
    def reassign_arrival(day: str, employee_id: str):
        d = _date(day, service.now().date())
        try:
            service.reassign_arrival(d, employee_id, request.form.get("to", ""), user())
        except ValueError as exc:
            return failed(str(exc), "index", day=day)
        return done("Arrival moved to the right person.", "index", day=day)

    @app.post("/day/<day>/<employee_id>/delete")
    def delete_arrival(day: str, employee_id: str):
        d = _date(day, service.now().date())
        try:
            service.delete_arrival(d, employee_id, user())
        except ValueError as exc:
            return failed(str(exc), "index", day=day)
        return done("Arrival deleted.", "index", day=day)

    @app.get("/unknowns")
    def unknowns():
        items = sorted(store.unknowns(), key=lambda u: u.ts, reverse=True)
        return render_template("unknowns.html", items=items, employees=store.employees(),
                               local=lambda ts: ts.astimezone(service.tz))

    @app.post("/unknowns/<int:unknown_id>/assign")
    def assign(unknown_id: int):
        try:
            service.resolve_unknown(unknown_id, request.form.get("employee_id", ""), user())
        except ValueError as exc:
            return failed(str(exc), "unknowns")
        return done("Arrival recorded and face learned.", "unknowns")

    @app.post("/unknowns/<int:unknown_id>/ignore")
    def ignore(unknown_id: int):
        service.ignore_unknown(unknown_id, user())
        return done("Ignored.", "unknowns")

    # employees ---------------------------------------------------------------------

    @app.get("/employees")
    def employees():
        counts = {}
        thumbs = {}
        for r in store.references(cfg.recognition.model_id()):
            counts[r.employee_id] = counts.get(r.employee_id, 0) + 1
            if r.image and r.employee_id not in thumbs:
                thumbs[r.employee_id] = r.image
        names = {e.id: e.name for e in store.all_employees()}
        return render_template("employees.html", people=store.all_employees(), counts=counts, thumbs=thumbs,
                               look_alikes=library.look_alikes(), names=names,
                               on_leave=store.leaves_on(service.now().date()))

    @app.post("/employees")
    def add_employee():
        try:
            store.add_employee(request.form.get("id", ""), request.form.get("name", ""))
        except ValueError as exc:
            return failed(str(exc), "employees")
        store.log(service.now(), user(), "add employee", f"{request.form['id']} {request.form['name']}")
        return done("Employee added. Now upload their photos.", "employee", employee_id=request.form["id"].strip())

    @app.post("/employees/import")
    def import_employees():
        file = request.files.get("file")
        if not file:
            return failed("Choose a CSV file.", "employees")
        try:
            n = store.import_employees(file.read().decode("utf-8-sig"))
        except (ValueError, UnicodeDecodeError) as exc:
            return failed(f"Import failed: {exc}", "employees")
        library.reload()
        store.log(service.now(), user(), "import employees", f"{n} rows")
        return done(f"Imported {n} employees.", "employees")

    @app.get("/employees/<employee_id>")
    def employee(employee_id: str):
        person = store.employee(employee_id) or abort(404)
        refs = store.references(cfg.recognition.model_id(), employee_id)
        return render_template("employee.html", person=person, refs=refs,
                               leaves=store.leaves(employee_id=employee_id, ending_after=service.now().date()))

    @app.post("/employees/<employee_id>")
    def update_employee(employee_id: str):
        store.employee(employee_id) or abort(404)
        try:
            store.update_employee(employee_id, request.form.get("name", ""), request.form.get("active") == "on")
        except ValueError as exc:
            return failed(str(exc), "employee", employee_id=employee_id)
        library.reload()
        store.log(service.now(), user(), "edit employee", f"{employee_id} {request.form.get('name')} "
                  f"active={request.form.get('active') == 'on'}")
        return done("Saved.", "employee", employee_id=employee_id)

    @app.post("/employees/<employee_id>/photos")
    def upload_photos(employee_id: str):
        from .faces import decode_image

        store.employee(employee_id) or abort(404)
        files = [f for f in request.files.getlist("photos") if f.filename]
        if not files:
            return failed("Choose one or more photos.", "employee", employee_id=employee_id)
        try:
            core.recognizer()
        except Exception as exc:
            return failed(f"Face models are not available: {exc}", "employee", employee_id=employee_id)
        added = 0
        for f in files:
            try:
                result = library.add_photo(employee_id, decode_image(f.read()))
            except ValueError as exc:
                flash(f"{f.filename}: {exc}", "error")
                continue
            if result.ok:
                added += 1
            else:
                flash(f"{f.filename}: {result.message}", "error")
            for w in result.warnings:
                flash(f"{f.filename}: {w}", "warn")
        store.log(service.now(), user(), "upload photos", f"{employee_id}: {added} added")
        if added:
            flash(f"{added} photo(s) added.", "ok")
        return redirect(url_for("employee", employee_id=employee_id))

    @app.post("/employees/<employee_id>/photos/delete")
    def delete_photo(employee_id: str):
        refs = store.references(cfg.recognition.model_id(), employee_id)
        image = request.form.get("image", "")
        if request.form.get("ref_id"):  # a reference without a saved photo
            ref_id = int(request.form["ref_id"])
            if ref_id not in {r.id for r in refs}:
                abort(400)
            store.delete_reference(ref_id)
            library.reload()
            image = f"#{ref_id}"
        elif image in {r.image for r in refs}:
            library.delete_photo(image)
        else:
            abort(400)
        store.log(service.now(), user(), "delete photo", f"{employee_id} {image}")
        return done("Photo deleted.", "employee", employee_id=employee_id)

    @app.post("/employees/<employee_id>/delete")
    def delete_employee(employee_id: str):
        person = store.employee(employee_id) or abort(404)
        if request.form.get("confirm") != employee_id:
            return failed("Type the employee id to confirm.", "employee", employee_id=employee_id)
        library.delete_employee(employee_id)
        store.log(service.now(), user(), "delete employee", f"{employee_id} {person.name}")
        return done(f"{person.name} and their face data were deleted.", "employees")

    # calendar ---------------------------------------------------------------------

    @app.get("/calendar")
    def calendar():
        today = service.now().date()
        return render_template("calendar.html", days=store.calendar(today - dt.timedelta(days=30)),
                               leaves=store.leaves(ending_after=today), employees=store.employees(),
                               names={e.id: e.name for e in store.all_employees()}, today=today)

    @app.post("/calendar/days")
    def add_days():
        try:
            start = dt.date.fromisoformat(request.form.get("start", ""))
            end = dt.date.fromisoformat(request.form.get("end") or request.form.get("start", ""))
            if end < start or (end - start).days > 60:
                raise ValueError("choose an end date on or after the start date, at most 60 days later")
            kind, note = request.form.get("kind", ""), request.form.get("note", "")
            day = start
            while day <= end:
                store.set_calendar_day(day, kind, note)
                day += dt.timedelta(days=1)
        except ValueError as exc:
            return failed(str(exc), "calendar")
        store.log(service.now(), user(), f"add {kind}", f"{start}..{end} {note}")
        return done("Calendar updated.", "calendar")

    @app.post("/calendar/days/<day>/delete")
    def delete_day(day: str):
        store.delete_calendar_day(_date(day, service.now().date()))
        store.log(service.now(), user(), "delete calendar day", day)
        return done("Removed.", "calendar")

    @app.post("/calendar/leaves")
    def add_leave():
        try:
            start = dt.date.fromisoformat(request.form.get("start", ""))
            end = dt.date.fromisoformat(request.form.get("end") or request.form.get("start", ""))
            emp = request.form.get("employee_id", "")
            store.add_leave(emp, start, end, request.form.get("kind", ""), request.form.get("note", ""))
        except ValueError as exc:
            return failed(str(exc), "calendar")
        store.log(service.now(), user(), "add leave", f"{emp} {start}..{end} {request.form.get('kind')}")
        return done("Leave added.", "calendar")

    @app.post("/calendar/leaves/<int:leave_id>/delete")
    def delete_leave(leave_id: int):
        removed = store.delete_leave(leave_id)
        if removed:
            store.log(service.now(), user(), "delete leave", f"{removed.employee_id} {removed.start}..{removed.end}")
        return done("Leave removed.", "calendar")

    # live camera --------------------------------------------------------------------

    @app.get("/live")
    def live_page():
        return render_template("live.html", cfg=cfg, in_window=service.in_window(service.now()))

    @app.get("/live.jpg")
    def live_jpg():
        jpeg, _ = live.frame()
        if jpeg is None:
            abort(404)
        return Response(jpeg, mimetype="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/live.json")
    def live_json():
        _, frame_time = live.frame()  # counts as a viewer, so the camera loop keeps drawing frames
        return jsonify(camera=live.camera_state, problem=live.problem, fps=round(live.fps, 1), faces=live.faces,
                       last_identified=live.last_identified, previewing=live.previewing,
                       in_window=service.in_window(service.now()),
                       frame_age=round(time.time() - frame_time, 1) if frame_time else None)

    @app.post("/live/preview")
    def preview():
        if request.form.get("action") == "stop":
            live.stop_preview()
        else:
            live.start_preview()
        return redirect(url_for("live_page"))

    # settings ---------------------------------------------------------------------

    @app.get("/settings")
    def settings_page():
        return render_template("settings.html", s=settings.current(cfg), cfg=cfg)

    @app.post("/settings")
    def save_settings():
        form = request.form
        changes: dict = {}
        current = settings.current(cfg)
        for section, names in settings.EDITABLE.items():
            for name in names:
                key = f"{section}.{name}" if section else name
                value = current[section][name]
                if name == "password":
                    if form.get(key):
                        changes.setdefault(section, {})[name] = form[key]
                elif name == "work_days":
                    changes.setdefault(section, {})[name] = [int(d) for d in form.getlist(key)]
                elif isinstance(value, bool):
                    changes.setdefault(section, {})[name] = form.get(key) == "on"
                elif key in form:
                    changes.setdefault(section, {})[name] = form[key]
        try:
            settings.save(cfg, store, changes)
        except ValueError as exc:
            return failed(f"Not saved: {exc}", "settings_page")
        store.log(service.now(), user(), "change settings",
                  ", ".join(f"{s}.{n}" if s else n for s, f in changes.items() for n in f if n != "password"))
        return done("Settings saved and applied.", "settings_page")

    @app.post("/settings/test-chat")
    def test_chat():
        if not cfg.google_chat.webhook_url:
            return failed("Save a Google Chat webhook URL first.", "settings_page")
        try:
            core.chat.send_now("✅ The attendance system can post to this space.")
        except Exception as exc:
            return failed(f"Sending failed: {exc}", "settings_page")
        return done("Test message sent; check Google Chat.", "settings_page")

    @app.get("/audit")
    def audit():
        return render_template("audit.html", rows=store.audit(), local=lambda ts: ts.astimezone(service.tz))

    @app.get("/media/<path:path>")
    def media(path: str):
        if not (path.startswith("crops/") or path.startswith("faces/")) or ".." in Path(path).parts:
            abort(404)
        return send_from_directory(core.data_dir, path)

    return app
