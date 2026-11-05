from datetime import timedelta

import pytest

from accesskeeper import create_app
from accesskeeper.extensions import db
from accesskeeper.models import Access, User
from accesskeeper.utils import utcnow

PASSWORD = "correct-horse-battery-1"


@pytest.fixture
def app(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "SECRET_KEY": "x" * 48,
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{(tmp_path / 'test.db').as_posix()}",
            "WTF_CSRF_ENABLED": False,
        }
    )
    yield app
    with app.app_context():
        db.session.remove()
        db.engine.dispose()


@pytest.fixture
def client(app):
    return app.test_client()


def make_user(app, username, role):
    with app.app_context():
        user = User(username=username, role=role)
        user.set_password(PASSWORD)
        db.session.add(user)
        db.session.commit()


def login(app, username):
    client = app.test_client()
    response = client.post("/login", data={"username": username, "password": PASSWORD})
    assert response.status_code == 302, response.get_data(as_text=True)
    return client


@pytest.fixture
def users(app):
    make_user(app, "boss", "admin")
    make_user(app, "second", "admin")
    make_user(app, "reader", "viewer")
    return {name: login(app, name) for name in ("boss", "second", "reader")}


def add_access(app, name="Ana", ends_in=timedelta(days=30), **overrides):
    now = utcnow()
    values = dict(
        name=name,
        email=f"{name.lower()}@example.com",
        company="Contoso",
        user_type="external",
        access_type="Global Reader",
        system="Entra ID",
        description="d",
        justification="j",
        authorized_by="Boss",
        start_at=now - timedelta(days=60),
        end_at=now + ends_in,
        created_by="boss",
    )
    values.update(overrides)
    with app.app_context():
        access = Access(**values)
        db.session.add(access)
        db.session.commit()
        return access.id


def user_id(app, username):
    with app.app_context():
        return User.query.filter_by(username=username).one().id
