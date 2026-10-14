import base64

import numpy as np

from attendance.web import create_app
from test_service import DAY, at, env  # noqa: F401  (pytest fixture)


def client(service, store, password=""):
    service.cfg.web.password = password
    app = create_app(service.cfg, store, service)
    app.testing = True
    return app.test_client()


def test_pages_render_and_admin_can_assign_unknown(env):  # noqa: F811
    service, store, clock, sent = env
    clock.t = at(10, 30)
    service.on_identified("A", at(9, 5), 0.7, "a.jpg")
    uid = service.on_unknown(at(9, 20), "u.jpg", np.ones(512, dtype=np.float32), "B", 0.4)
    c = client(service, store)

    page = c.get("/").get_data(as_text=True)
    assert "Anh A" in page and "09:05" in page and "Không thấy: 2" in page

    page = c.get("/unknowns").get_data(as_text=True)
    assert "Giống nhất: Anh B" in page

    resp = c.post(f"/unknowns/{uid}/assign", data={"employee_id": "B"})
    assert resp.status_code == 302
    assert "B" in store.checkins(DAY)
    assert c.post(f"/unknowns/{uid}/assign", data={"employee_id": "B"}).status_code == 400


def test_password_protects_pages(env):  # noqa: F811
    service, store, _, _ = env
    c = client(service, store, password="s3cret")
    assert c.get("/").status_code == 401
    token = base64.b64encode(b"admin:s3cret").decode()
    assert c.get("/", headers={"Authorization": f"Basic {token}"}).status_code == 200
