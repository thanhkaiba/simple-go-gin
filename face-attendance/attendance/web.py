"""Small admin page: today's attendance and the queue of faces the system could not identify."""
from __future__ import annotations

import datetime as dt
import hmac

from flask import Flask, Response, abort, redirect, render_template_string, request, send_from_directory, url_for

from .config import Config
from .service import AttendanceService, minutes_late
from .store import Store

LAYOUT = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Camera attendance</title>
<style>
 body{font-family:system-ui,sans-serif;margin:0 auto;max-width:1000px;padding:16px;color:#222;background:#fafafa}
 nav a{margin-right:16px} h1{font-size:1.4em} h2{font-size:1.1em;margin-top:28px}
 table{border-collapse:collapse;width:100%;background:#fff} td,th{border-bottom:1px solid #eee;padding:6px 8px;text-align:left}
 img.thumb{height:56px;border-radius:4px} .late{color:#c25400} .absent{color:#b00020} .ok{color:#1b7f3a}
 .stats span{display:inline-block;margin-right:18px;font-weight:600}
 .cards{display:flex;flex-wrap:wrap;gap:12px} .card{background:#fff;border:1px solid #ddd;border-radius:8px;padding:10px;width:220px}
 .card img{width:100%;border-radius:4px} select,button{margin-top:6px;width:100%;padding:6px} .warn{background:#fff4e5;padding:8px;border-radius:6px}
</style></head><body>
<nav><a href="{{ url_for('index') }}">Today</a><a href="{{ url_for('unknowns') }}">Unidentified faces ({{ pending }})</a></nav>
{% block body %}{% endblock %}
</body></html>"""

INDEX = """{% extends "layout" %}{% block body %}
<h1>Attendance {{ rep.day.isoformat() }}</h1>
<form><input type="date" name="day" value="{{ rep.day.isoformat() }}"> <button style="width:auto">Show</button></form>
{% if not rep.workday %}<p>Not a work day.</p>{% endif %}
<p class="stats"><span class="ok">On time: {{ rep.on_time|length }}</span><span class="late">Late: {{ rep.late|length }}</span>
<span class="absent">Not seen: {{ rep.not_seen|length }}</span><span>Unidentified: {{ rep.pending_unknowns }}</span></p>
<p>On time if first seen before {{ rep.window.late_after.strftime('%H:%M') }}. Camera runs {{ rep.window.start.strftime('%H:%M') }}–{{ rep.window.end.strftime('%H:%M') }}.</p>
{% for a, b in rep.outages %}<p class="warn">⚠️ Camera/system down {{ a.strftime('%H:%M') }}–{{ b.strftime('%H:%M') }}</p>{% endfor %}
<h2 class="late">Late</h2>
<table><tr><th>Photo</th><th>Employee</th><th>Arrived</th><th>Late by</th><th>Source</th></tr>
{% for a in rep.late %}<tr><td>{% if a.image %}<img class="thumb" src="{{ url_for('image', path=a.image) }}">{% endif %}</td>
<td>{{ a.name }}</td><td>{{ a.time }}</td><td>{{ late(a) }} min</td><td>{{ source_label(a.source) }}</td></tr>{% endfor %}</table>
<h2 class="absent">Not seen on camera</h2>
<table>{% for id, name in rep.not_seen %}<tr><td>{{ name }}</td><td>{{ id }}</td></tr>{% endfor %}</table>
<h2 class="ok">On time</h2>
<table><tr><th>Photo</th><th>Employee</th><th>Arrived</th><th>Source</th></tr>
{% for a in rep.on_time %}<tr><td>{% if a.image %}<img class="thumb" src="{{ url_for('image', path=a.image) }}">{% endif %}</td>
<td>{{ a.name }}</td><td>{{ a.time }}</td><td>{{ source_label(a.source) }}</td></tr>{% endfor %}</table>
{% endblock %}"""

UNKNOWNS = """{% extends "layout" %}{% block body %}
<h1>Unidentified faces</h1>
<p>Pick the right person to record their arrival at the time of the photo. The system also learns this face for next time.</p>
{% if error %}<p class="warn">{{ error }}</p>{% endif %}
<div class="cards">{% for u in items %}<div class="card">
 {% if u.image %}<img src="{{ url_for('image', path=u.image) }}">{% endif %}
 <div>{{ local(u.ts).strftime('%Y-%m-%d %H:%M:%S') }}</div>
 {% if u.guess_id %}<div>Closest match: {{ employees.get(u.guess_id, u.guess_id) }} ({{ '%.2f'|format(u.guess_score) }})</div>{% endif %}
 <form method="post" action="{{ url_for('assign', unknown_id=u.id) }}">
  <select name="employee_id">{% for id, name in employees.items() %}
   <option value="{{ id }}" {% if id == u.guess_id %}selected{% endif %}>{{ name }}</option>{% endfor %}</select>
  <button>Confirm this person</button></form>
 <form method="post" action="{{ url_for('ignore', unknown_id=u.id) }}"><button>Ignore (visitor / not an employee)</button></form>
</div>{% else %}<p>Nothing to review.</p>{% endfor %}</div>
{% endblock %}"""

SOURCES = {"camera": "Camera", "admin": "Confirmed by admin"}


def create_app(cfg: Config, store: Store, service: AttendanceService) -> Flask:
    app = Flask(__name__)
    from jinja2 import DictLoader

    app.jinja_loader = DictLoader({"layout": LAYOUT})
    crops = cfg.path(cfg.data_dir) / "crops"

    @app.before_request
    def require_login():
        if not cfg.web.password:
            return None
        auth = request.authorization
        if (auth and hmac.compare_digest(auth.username or "", cfg.web.username)
                and hmac.compare_digest(auth.password or "", cfg.web.password)):
            return None
        return Response("Login required", 401, {"WWW-Authenticate": 'Basic realm="attendance"'})

    def render(template: str, **ctx):
        return render_template_string(template, pending=len(store.unknowns()), **ctx)

    @app.get("/")
    def index():
        day = service.now().date()
        if request.args.get("day"):
            try:
                day = dt.date.fromisoformat(request.args["day"])
            except ValueError:
                abort(400)
        rep = service.report(day)
        return render(INDEX, rep=rep, late=lambda a: minutes_late(a, rep.window),
                      source_label=lambda s: SOURCES.get(s, s))

    @app.get("/unknowns")
    def unknowns(error: str | None = None):
        items = sorted(store.unknowns(), key=lambda u: u.ts, reverse=True)
        return render(UNKNOWNS, items=items, employees=store.employees(), error=error,
                      local=lambda ts: ts.astimezone(service.tz))

    @app.post("/unknowns/<int:unknown_id>/assign")
    def assign(unknown_id: int):
        try:
            service.resolve_unknown(unknown_id, request.form.get("employee_id", ""))
        except ValueError as exc:
            return unknowns(error=str(exc)), 400
        return redirect(url_for("unknowns"))

    @app.post("/unknowns/<int:unknown_id>/ignore")
    def ignore(unknown_id: int):
        service.ignore_unknown(unknown_id)
        return redirect(url_for("unknowns"))

    @app.get("/images/<path:path>")
    def image(path: str):
        return send_from_directory(crops, path)

    return app
