import time
from datetime import timedelta

from accesskeeper import create_app
from accesskeeper.extensions import db
from accesskeeper.models import AuditLog

from .conftest import PASSWORD, make_user


def test_default_idle_timeout_is_five_minutes(app, users):
    assert app.config["IDLE_TIMEOUT_MINUTES"] == 5
    page = users["boss"].get("/").get_data(as_text=True)
    assert 'data-idle-seconds="300"' in page and 'id="idle-warning"' in page
    assert "Do something to continue" in page
    assert 'name="csrf-token"' in page


def test_ping_requires_a_session(app, users):
    assert app.test_client().post("/session/ping").status_code == 401
    assert app.test_client().post("/session/expire").status_code == 401
    assert users["boss"].post("/session/ping").status_code == 204


def test_expire_signs_out_and_is_audited(app, users):
    c = users["reader"]
    assert c.post("/session/expire").status_code == 204
    assert c.get("/").status_code == 302
    with app.app_context():
        row = db.session.scalars(db.select(AuditLog).where(AuditLog.action == "auth.session_timeout")).one()
        assert row.actor == "reader"


def test_login_page_explains_the_timeout(app):
    make_user(app, "boss", "admin")
    page = app.test_client().get("/login?timeout=1").get_data(as_text=True)
    assert "signed out after 5 minutes without activity" in page


def test_server_side_idle_expiry(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "x" * 48,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{(tmp_path / 't.db').as_posix()}",
            "WTF_CSRF_ENABLED": False,
            "PERMANENT_SESSION_LIFETIME": timedelta(seconds=2),
        }
    )
    make_user(app, "boss", "admin")
    c = app.test_client()
    assert c.post("/login", data={"username": "boss", "password": PASSWORD}).status_code == 302
    assert c.get("/").status_code == 200
    time.sleep(1.3)
    assert c.get("/").status_code == 200
    time.sleep(1.3)
    assert c.get("/").status_code == 200
    time.sleep(3.5)
    assert c.get("/").status_code == 302
    with app.app_context():
        db.session.remove()
        db.engine.dispose()
