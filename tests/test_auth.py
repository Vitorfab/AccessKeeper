from accesskeeper.extensions import db
from accesskeeper.models import AuditLog, User

from .conftest import PASSWORD


def audit_rows(app, action=None):
    with app.app_context():
        stmt = db.select(AuditLog)
        if action:
            stmt = stmt.where(AuditLog.action == action)
        return db.session.scalars(stmt).all()


def test_setup_creates_first_admin_and_closes(app, client):
    assert client.get("/").headers["Location"].endswith("/setup")
    response = client.post(
        "/setup", data={"username": "boss", "password": PASSWORD, "confirm": PASSWORD}
    )
    assert response.status_code == 302
    assert client.get("/setup").headers["Location"].endswith("/login")
    with app.app_context():
        assert User.query.one().role == "admin"


def test_failed_login_never_logs_typed_username(app, users):
    secret = "MyS3cretTyped"
    client = app.test_client()
    client.post("/login", data={"username": secret, "password": "whatever-whatever"})
    rows = audit_rows(app, "auth.login_failure")
    assert rows and all(secret.lower() not in f"{r.target}{r.details}{r.actor}".lower() for r in rows)
    assert rows[-1].actor == "anonymous"


def test_lockout_blocks_even_correct_password(app, users):
    client = app.test_client()
    for _ in range(5):
        client.post("/login", data={"username": "reader", "password": "wrong-wrong-wrong"})
    response = client.post("/login", data={"username": "reader", "password": PASSWORD})
    assert response.status_code == 200
    assert audit_rows(app, "auth.account_locked")


def test_audit_is_admin_only(users):
    assert users["boss"].get("/audit").status_code == 200
    assert users["reader"].get("/audit").status_code == 403
    assert users["reader"].get("/audit/export.csv").status_code == 403


def test_audit_log_is_append_only(app, users):
    import pytest
    from sqlalchemy.exc import DBAPIError

    with app.app_context():
        with pytest.raises((DBAPIError, RuntimeError)):
            db.session.execute(db.text("UPDATE audit_log SET actor='x'"))
            db.session.commit()
        db.session.rollback()
        with pytest.raises((DBAPIError, RuntimeError)):
            db.session.execute(db.text("DELETE FROM audit_log"))
            db.session.commit()
        db.session.rollback()


def test_security_headers(users):
    response = users["boss"].get("/")
    headers = response.headers
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    assert "http" not in headers["Content-Security-Policy"]
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["X-Frame-Options"] == "DENY"
    assert headers["Referrer-Policy"] == "no-referrer"


def test_no_inline_script_or_style_in_pages(users):
    for path in ("/", "/access/", "/access/archive", "/review/", "/review/campaigns", "/users/", "/audit", "/systems/"):
        html = users["boss"].get(path).get_data(as_text=True)
        assert " style=" not in html and "<style" not in html and "onclick" not in html
        assert "<script>" not in html
