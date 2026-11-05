import pytest

from accesskeeper import audit, integrity
from accesskeeper.extensions import db
from accesskeeper.models import AuditLog


def verify(app):
    with app.app_context():
        return integrity.verify()


def run_sql(app, *statements):
    with app.app_context():
        for statement in statements:
            db.session.execute(db.text(statement))
        db.session.commit()


def test_fresh_database_starts_a_chain_that_verifies(app, users):
    users["boss"].get("/access/export.csv")
    result = verify(app)
    assert result.status == "ok"
    assert result.checked >= 5 and result.legacy == 0
    with app.app_context():
        first = db.session.scalars(db.select(AuditLog).order_by(AuditLog.id)).first()
        assert first.action == "audit.chain_started" and first.prev_hash == "0" * 64
        assert first.details.startswith("legacy_events=0 | last_legacy_id=0")


def test_every_event_is_sealed_to_the_previous_one(app, users):
    with app.app_context():
        rows = db.session.scalars(db.select(AuditLog).order_by(AuditLog.id)).all()
        assert all(row.hash and len(row.hash) == 64 for row in rows)
        for earlier, later in zip(rows, rows[1:]):
            assert later.prev_hash == earlier.hash


def test_many_events_in_one_transaction_chain_in_order(app):
    with app.app_context():
        for number in range(6):
            audit.record("test.event", details=str(number), actor="tester")
        db.session.commit()
    assert verify(app).status == "ok"


def test_rolled_back_events_leave_no_gap(app):
    with app.app_context():
        audit.record("test.rolled_back", actor="tester")
        db.session.rollback()
        audit.record("test.kept", actor="tester")
        db.session.commit()
    assert verify(app).status == "ok"


def test_changing_an_event_is_detected(app, users):
    users["boss"].get("/access/export.csv")
    run_sql(app, "DROP TRIGGER audit_log_no_update", "UPDATE audit_log SET details = 'forged' WHERE id = 3")
    result = verify(app)
    assert result.status == "broken" and result.broken_id == 3
    assert "content does not match" in result.message


def test_removing_a_middle_event_is_detected(app, users):
    users["boss"].get("/access/export.csv")
    run_sql(app, "DROP TRIGGER audit_log_no_delete", "DELETE FROM audit_log WHERE id = 3")
    result = verify(app)
    assert result.status == "broken" and result.broken_id == 4


def test_removing_the_last_event_is_detected(app, users):
    users["boss"].get("/access/export.csv")
    with app.app_context():
        last = db.session.scalars(db.select(AuditLog).order_by(AuditLog.id.desc())).first().id
    run_sql(app, "DROP TRIGGER audit_log_no_delete", f"DELETE FROM audit_log WHERE id = {last}")
    result = verify(app)
    assert result.status == "broken" and "chain head" in result.message


def test_an_event_inserted_without_a_hash_is_detected(app, users):
    run_sql(
        app,
        "INSERT INTO audit_log (timestamp, actor, action, ip) "
        "VALUES ('2026-01-01 00:00:00.000000', 'x', 'forged.event', 'local')",
    )
    result = verify(app)
    assert result.status == "broken" and "without a hash" in result.message


def test_a_changed_key_is_reported_as_not_verifiable_instead_of_tampering(app, users):
    with app.app_context():
        original = app.config["AUDIT_KEY"]
        app.config["AUDIT_KEY"] = integrity.derive_key("a-completely-different-secret-key-value")
        try:
            result = integrity.verify()
        finally:
            app.config["AUDIT_KEY"] = original
    assert result.status == "key_changed" and "not proof of tampering" in result.message


def test_audit_key_can_be_separate_from_secret_key():
    assert integrity.derive_key("a" * 40) != integrity.derive_key("b" * 40)
    assert integrity.derive_key("a" * 40) == integrity.derive_key("a" * 40)
    assert len(integrity.derive_key("a" * 40)) == 32


def test_short_audit_hash_key_is_refused(tmp_path):
    from accesskeeper import create_app

    with pytest.raises(RuntimeError, match="AUDIT_HASH_KEY"):
        create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "x" * 48,
                "AUDIT_HASH_KEY": "too-short",
                "SQLALCHEMY_DATABASE_URI": f"sqlite:///{(tmp_path / 'k.db').as_posix()}",
            }
        )


def test_integrity_screen_is_admin_only_and_records_the_check(app, users):
    assert users["reader"].get("/audit/integrity").status_code == 403
    assert users["reader"].post("/audit/integrity").status_code == 403
    page = users["boss"].get("/audit/integrity").get_data(as_text=True)
    assert "Chained events" in page and "Top of the chain" in page
    response = users["boss"].post("/audit/integrity", follow_redirects=True)
    assert "Integrity check passed" in response.get_data(as_text=True)
    with app.app_context():
        row = db.session.scalars(
            db.select(AuditLog).where(AuditLog.action == "audit.integrity_checked")
        ).one()
        assert "result=ok" in row.details


def test_integrity_screen_reports_the_first_broken_event(app, users):
    run_sql(app, "DROP TRIGGER audit_log_no_update", "UPDATE audit_log SET actor = 'evil' WHERE id = 2")
    response = users["boss"].post("/audit/integrity", follow_redirects=True)
    assert "FAILED at event #2" in response.get_data(as_text=True)
    assert verify(app).status == "broken"


def test_audit_csv_and_page_show_chain_columns(app, users):
    body = users["boss"].get("/audit/export.csv").get_data(as_text=True)
    assert "Previous hash" in body and "Hash" in body
    assert "audit.chain_started" in body
    page = users["boss"].get("/audit").get_data(as_text=True)
    assert "Verify integrity" in page and "<th>Chain</th>" in page
