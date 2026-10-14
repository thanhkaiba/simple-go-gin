"""Small admin page: today's attendance and the queue of faces the system could not identify."""
from __future__ import annotations

import datetime as dt
import hmac

from flask import Flask, Response, abort, redirect, render_template_string, request, send_from_directory, url_for

from .config import Config
from .service import AttendanceService, minutes_late
from .store import Store

LAYOUT = """<!doctype html>
<html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Chấm công camera</title>
<style>
 body{font-family:system-ui,sans-serif;margin:0 auto;max-width:1000px;padding:16px;color:#222;background:#fafafa}
 nav a{margin-right:16px} h1{font-size:1.4em} h2{font-size:1.1em;margin-top:28px}
 table{border-collapse:collapse;width:100%;background:#fff} td,th{border-bottom:1px solid #eee;padding:6px 8px;text-align:left}
 img.thumb{height:56px;border-radius:4px} .late{color:#c25400} .absent{color:#b00020} .ok{color:#1b7f3a}
 .stats span{display:inline-block;margin-right:18px;font-weight:600}
 .cards{display:flex;flex-wrap:wrap;gap:12px} .card{background:#fff;border:1px solid #ddd;border-radius:8px;padding:10px;width:220px}
 .card img{width:100%;border-radius:4px} select,button{margin-top:6px;width:100%;padding:6px} .warn{background:#fff4e5;padding:8px;border-radius:6px}
</style></head><body>
<nav><a href="{{ url_for('index') }}">Hôm nay</a><a href="{{ url_for('unknowns') }}">Khuôn mặt chưa xác định ({{ pending }})</a></nav>
{% block body %}{% endblock %}
</body></html>"""

INDEX = """{% extends "layout" %}{% block body %}
<h1>Chấm công ngày {{ rep.day.strftime('%d/%m/%Y') }}</h1>
<form><input type="date" name="day" value="{{ rep.day.isoformat() }}"> <button style="width:auto">Xem</button></form>
{% if not rep.workday %}<p>Không phải ngày làm việc.</p>{% endif %}
<p class="stats"><span class="ok">Đúng giờ: {{ rep.on_time|length }}</span><span class="late">Đi muộn: {{ rep.late|length }}</span>
<span class="absent">Không thấy: {{ rep.not_seen|length }}</span><span>Chưa xác định: {{ rep.pending_unknowns }}</span></p>
<p>Đúng giờ nếu thấy trước {{ rep.window.late_after.strftime('%H:%M') }}. Camera chạy {{ rep.window.start.strftime('%H:%M') }}–{{ rep.window.end.strftime('%H:%M') }}.</p>
{% for a, b in rep.outages %}<p class="warn">⚠️ Camera/hệ thống không hoạt động {{ a.strftime('%H:%M') }}–{{ b.strftime('%H:%M') }}</p>{% endfor %}
<h2 class="late">Đi muộn</h2>
<table><tr><th>Ảnh</th><th>Nhân viên</th><th>Giờ đến</th><th>Muộn</th><th>Nguồn</th></tr>
{% for a in rep.late %}<tr><td>{% if a.image %}<img class="thumb" src="{{ url_for('image', path=a.image) }}">{% endif %}</td>
<td>{{ a.name }}</td><td>{{ a.time }}</td><td>{{ late(a) }} phút</td><td>{{ source_label(a.source) }}</td></tr>{% endfor %}</table>
<h2 class="absent">Không thấy trên camera</h2>
<table>{% for id, name in rep.not_seen %}<tr><td>{{ name }}</td><td>{{ id }}</td></tr>{% endfor %}</table>
<h2 class="ok">Đúng giờ</h2>
<table><tr><th>Ảnh</th><th>Nhân viên</th><th>Giờ đến</th><th>Nguồn</th></tr>
{% for a in rep.on_time %}<tr><td>{% if a.image %}<img class="thumb" src="{{ url_for('image', path=a.image) }}">{% endif %}</td>
<td>{{ a.name }}</td><td>{{ a.time }}</td><td>{{ source_label(a.source) }}</td></tr>{% endfor %}</table>
{% endblock %}"""

UNKNOWNS = """{% extends "layout" %}{% block body %}
<h1>Khuôn mặt chưa xác định</h1>
<p>Chọn đúng người để tính chấm công vào thời điểm trong ảnh. Hệ thống cũng học thêm khuôn mặt này cho lần sau.</p>
{% if error %}<p class="warn">{{ error }}</p>{% endif %}
<div class="cards">{% for u in items %}<div class="card">
 {% if u.image %}<img src="{{ url_for('image', path=u.image) }}">{% endif %}
 <div>{{ local(u.ts).strftime('%d/%m %H:%M:%S') }}</div>
 {% if u.guess_id %}<div>Giống nhất: {{ employees.get(u.guess_id, u.guess_id) }} ({{ '%.2f'|format(u.guess_score) }})</div>{% endif %}
 <form method="post" action="{{ url_for('assign', unknown_id=u.id) }}">
  <select name="employee_id">{% for id, name in employees.items() %}
   <option value="{{ id }}" {% if id == u.guess_id %}selected{% endif %}>{{ name }}</option>{% endfor %}</select>
  <button>Xác nhận là người này</button></form>
 <form method="post" action="{{ url_for('ignore', unknown_id=u.id) }}"><button>Bỏ qua (khách / không phải nhân viên)</button></form>
</div>{% else %}<p>Không có khuôn mặt nào cần xác nhận.</p>{% endfor %}</div>
{% endblock %}"""

SOURCES = {"camera": "Camera", "admin": "Admin xác nhận"}


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
        return Response("Cần đăng nhập", 401, {"WWW-Authenticate": 'Basic realm="attendance"'})

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
