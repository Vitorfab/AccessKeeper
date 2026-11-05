from accesskeeper.extensions import db
from accesskeeper.models import AuditLog, User

from .conftest import PASSWORD, make_user, user_id

NEW_PASSWORD = "another-long-password-9"


def denied(app):
    with app.app_context():
        return db.session.scalars(db.select(AuditLog).where(AuditLog.action == "user.action_denied")).all()


def role_of(app, username):
    with app.app_context():
        return User.query.filter_by(username=username).one().role


def enabled(app, username):
    with app.app_context():
        return User.query.filter_by(username=username).one().enabled


def test_first_user_is_master_and_labelled(app, users):
    html = users["boss"].get("/users/").get_data(as_text=True)
    assert "Master" in html


def test_regular_admin_cannot_touch_master(app, users):
    boss = user_id(app, "boss")
    c = users["second"]
    assert c.post(f"/users/{boss}/toggle").status_code == 403
    assert c.post(f"/users/{boss}/role", data={"role": "viewer"}).status_code == 403
    assert c.get(f"/users/{boss}/reset-password").status_code == 403
    assert c.post(
        f"/users/{boss}/reset-password", data={"password": NEW_PASSWORD, "confirm": NEW_PASSWORD}
    ).status_code == 403
    assert role_of(app, "boss") == "admin" and enabled(app, "boss")
    assert len(denied(app)) == 4
    users["boss"].get("/users/")
    login_again = app.test_client().post("/login", data={"username": "boss", "password": PASSWORD})
    assert login_again.status_code == 302


def test_regular_admin_cannot_create_or_promote_admins(app, users):
    c = users["second"]
    response = c.post(
        "/users/new",
        data={"username": "newadmin", "role": "admin", "password": NEW_PASSWORD, "confirm": NEW_PASSWORD},
    )
    assert response.status_code == 403
    reader = user_id(app, "reader")
    assert c.post(f"/users/{reader}/role", data={"role": "admin"}).status_code == 403
    assert role_of(app, "reader") == "viewer"
    with app.app_context():
        assert User.query.filter_by(username="newadmin").first() is None


def test_regular_admin_cannot_manage_other_admins(app, users):
    make_user(app, "third", "admin")
    third = user_id(app, "third")
    c = users["second"]
    assert c.post(f"/users/{third}/toggle").status_code == 403
    assert c.post(f"/users/{third}/role", data={"role": "viewer"}).status_code == 403
    assert c.get(f"/users/{third}/reset-password").status_code == 403
    assert role_of(app, "third") == "admin"


def test_regular_admin_manages_viewers(app, users):
    c = users["second"]
    response = c.post(
        "/users/new",
        data={"username": "viewer2", "role": "viewer", "password": NEW_PASSWORD, "confirm": NEW_PASSWORD},
    )
    assert response.status_code == 302
    reader = user_id(app, "reader")
    assert c.post(f"/users/{reader}/toggle").status_code == 302
    assert not enabled(app, "reader")
    assert c.post(
        f"/users/{reader}/reset-password", data={"password": NEW_PASSWORD, "confirm": NEW_PASSWORD}
    ).status_code == 302


def test_master_manages_everyone_but_self(app, users):
    c = users["boss"]
    second = user_id(app, "second")
    assert c.post(f"/users/{second}/role", data={"role": "viewer"}).status_code == 302
    assert role_of(app, "second") == "viewer"
    assert c.post(f"/users/{second}/role", data={"role": "admin"}).status_code == 302
    assert c.post(
        f"/users/{second}/reset-password", data={"password": NEW_PASSWORD, "confirm": NEW_PASSWORD}
    ).status_code == 302
    boss = user_id(app, "boss")
    c.post(f"/users/{boss}/toggle")
    c.post(f"/users/{boss}/role", data={"role": "viewer"})
    assert enabled(app, "boss") and role_of(app, "boss") == "admin"


def test_viewer_cannot_open_users(users):
    assert users["reader"].get("/users/").status_code == 403
