import importlib.util
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy.exc import DBAPIError

from accesskeeper import integrity
from accesskeeper.extensions import db
from accesskeeper.models import Access, AccessChange
from accesskeeper.utils import local_input, utcnow

from .conftest import add_access
from .test_access import FORM

SCRIPT = Path(__file__).resolve().parent.parent / "verify_audit.py"


def load_script():
    spec = importlib.util.spec_from_file_location("verify_audit", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def db_file(app):
    return app.config["SQLALCHEMY_DATABASE_URI"].removeprefix("sqlite:///")


def run_script(app, monkeypatch, *args):
    monkeypatch.setenv("SECRET_KEY", "x" * 48)
    monkeypatch.setenv("AUDIT_HASH_KEY", "")
    return load_script().main(["--db", db_file(app), *args])


# ---- edit history

def edit_form(**overrides):
    return dict(FORM, start_at=(utcnow() - timedelta(days=60)).strftime("%Y-%m-%dT%H:%M"), **overrides)


def test_edit_records_old_and_new_value_per_changed_field(app, users):
    access_id = add_access(app, "Ana Souza", company="Contoso", system="Firewall", authorized_by="Boss")
    with app.app_context():
        start = db.session.get(Access, access_id).start_at
    data = dict(FORM, company="Fabrikam", ticket="CHG-7", start_at=local_input(start))
    assert users["boss"].post(f"/access/{access_id}/edit", data=data).status_code == 302
    with app.app_context():
        rows = {c.field: c for c in AccessChange.query.filter_by(access_id=access_id)}
    assert rows["company"].old_value == "Contoso" and rows["company"].new_value == "Fabrikam"
    assert rows["ticket"].old_value == "" and rows["ticket"].new_value == "CHG-7"
    assert all(c.changed_by == "boss" for c in rows.values())
    page = users["boss"].get(f"/access/{access_id}").get_data(as_text=True)
    assert "Edit history" in page and "Fabrikam" in page and "CHG-7" in page


def test_unedited_access_shows_an_empty_history(app, users):
    access_id = add_access(app, "Ana Souza")
    page = users["boss"].get(f"/access/{access_id}").get_data(as_text=True)
    assert "Never edited" in page
    with app.app_context():
        assert AccessChange.query.count() == 0


def test_change_history_is_append_only(app, users):
    access_id = add_access(app)
    users["boss"].post(f"/access/{access_id}/edit", data=edit_form(company="Other"))
    with app.app_context():
        assert AccessChange.query.count() >= 1
        for sql in ("UPDATE access_changes SET new_value='x'", "DELETE FROM access_changes"):
            with pytest.raises((DBAPIError, RuntimeError)):
                db.session.execute(db.text(sql))
                db.session.commit()
            db.session.rollback()


def test_viewer_can_read_history(app, users):
    access_id = add_access(app)
    assert users["reader"].get(f"/access/{access_id}").status_code == 200


# ---- expiring digest

def test_digest_lists_expiring_and_pending_review_only(app, users):
    add_access(app, "Soonish", timedelta(days=3))
    add_access(app, "Lapsed", -timedelta(days=2))
    add_access(app, "Faraway", timedelta(days=90))
    for name in ("boss", "reader"):
        page = users[name].get("/access/digest").get_data(as_text=True)
        assert "Soonish" in page and "Lapsed" in page and "Faraway" not in page


def test_digest_requires_login(client):
    assert client.get("/access/digest").status_code == 302


# ---- integrity reminder

def test_reminder_logic(app, users):
    with app.app_context():
        now = utcnow()
        assert integrity.reminder(now, 30) == {"never": True, "days": None}
        assert integrity.reminder(now, 0) is None
    users["boss"].post("/audit/integrity")
    with app.app_context():
        assert integrity.reminder(utcnow(), 30) is None
        assert integrity.reminder(utcnow() + timedelta(days=31), 30)["days"] == 31


def test_dashboard_reminder_is_for_admins_and_clears_after_a_check(app, users):
    assert "never been verified" in users["boss"].get("/").get_data(as_text=True)
    assert "never been verified" not in users["reader"].get("/").get_data(as_text=True)
    users["boss"].post("/audit/integrity")
    assert "never been verified" not in users["boss"].get("/").get_data(as_text=True)


# ---- standalone verifier

def test_script_passes_on_a_healthy_database(app, users, monkeypatch, capsys):
    users["boss"].get("/access/export.csv")
    assert run_script(app, monkeypatch) == 0
    out = capsys.readouterr().out
    assert out.startswith("OK") and "Top of the chain" in out


def test_script_agrees_with_the_app_on_tampering(app, users, monkeypatch, capsys):
    users["boss"].get("/access/export.csv")
    with app.app_context():
        db.session.execute(db.text("DROP TRIGGER audit_log_no_update"))
        db.session.execute(db.text("UPDATE audit_log SET details = 'forged' WHERE id = 3"))
        db.session.commit()
        assert integrity.verify().status == "broken"
    assert run_script(app, monkeypatch) == 1
    assert "event #3" in capsys.readouterr().out


def test_script_detects_a_removed_last_event(app, users, monkeypatch, capsys):
    users["boss"].get("/access/export.csv")
    with app.app_context():
        last = db.session.scalar(db.text("SELECT MAX(id) FROM audit_log"))
        db.session.execute(db.text("DROP TRIGGER audit_log_no_delete"))
        db.session.execute(db.text(f"DELETE FROM audit_log WHERE id = {last}"))
        db.session.commit()
    assert run_script(app, monkeypatch) == 1
    assert "chain head" in capsys.readouterr().out


def test_script_compares_with_a_saved_top_hash(app, users, monkeypatch, capsys):
    users["boss"].get("/access/export.csv")
    assert run_script(app, monkeypatch) == 0
    top = capsys.readouterr().out.split("Top of the chain: ")[1].strip()
    assert run_script(app, monkeypatch, "--top", top) == 0
    assert run_script(app, monkeypatch, "--top", "0" * 64) == 1


def test_script_with_a_wrong_key_cannot_verify(app, users, monkeypatch, capsys):
    monkeypatch.setenv("SECRET_KEY", "y" * 48)
    monkeypatch.setenv("AUDIT_HASH_KEY", "")
    assert load_script().main(["--db", db_file(app)]) == 2
    assert "not the one that started the chain" in capsys.readouterr().out


def test_script_never_writes_to_the_database(app, users, monkeypatch):
    before = Path(db_file(app)).read_bytes()
    run_script(app, monkeypatch)
    assert Path(db_file(app)).read_bytes() == before


def test_script_reports_a_missing_database(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("SECRET_KEY", "x" * 48)
    assert load_script().main(["--db", str(tmp_path / "none.db")]) == 2
    assert "not found" in capsys.readouterr().out
