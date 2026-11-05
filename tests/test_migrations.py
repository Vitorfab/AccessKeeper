import sqlite3
from datetime import timedelta

import pytest

from accesskeeper import integrity, migrations
from accesskeeper.extensions import db
from accesskeeper.models import Access, AccessReview, AuditLog, SystemEntry
from accesskeeper.utils import utcnow

from .conftest import add_access


def test_seed_imports_existing_systems_once(app):
    add_access(app, "A", system="Entra ID")
    add_access(app, "B", system="entra id")
    add_access(app, "C", system="FortiGate")
    with app.app_context():
        db.session.execute(db.text("DROP TABLE systems"))
        db.session.commit()
        db.create_all()
        migrations.run()
        names = [e.name for e in SystemEntry.query.all()]
        assert "Firewall" in names and "FortiGate" in names
        assert sum(1 for n in names if n.lower() == "entra id") == 1
        count = len(names)
        migrations.run()
        assert SystemEntry.query.count() == count


def test_upgrade_closes_accesses_already_reviewed_as_removed(app):
    gone = add_access(app, "Gone", -timedelta(days=5))
    kept = add_access(app, "Kept", -timedelta(days=5))
    with app.app_context():
        when = utcnow() - timedelta(days=1)
        for access_id, outcome in ((gone, "removal_confirmed"), (kept, "kept")):
            access = db.session.get(Access, access_id)
            db.session.add(
                AccessReview(
                    access_id=access_id, reviewed_end_at=access.end_at, outcome=outcome,
                    justification="x" if outcome == "kept" else None, reviewed_by="boss", reviewed_at=when,
                )
            )
        db.session.commit()
        migrations.run()
        gone_row, kept_row = db.session.get(Access, gone), db.session.get(Access, kept)
        assert gone_row.closed_at == when and gone_row.closed_by == "boss"
        assert kept_row.closed_at is None
        migrations.run()


@pytest.mark.skipif(sqlite3.sqlite_version_info < (3, 35, 0), reason="needs ALTER TABLE DROP COLUMN")
def test_upgrade_from_v31_adds_columns_keeps_data_and_starts_the_chain(app):
    old = add_access(app, "Old")
    with app.app_context():
        for statement in (
            "DROP TRIGGER audit_log_no_update",
            "DROP TRIGGER audit_log_no_delete",
            "DELETE FROM audit_log",
            "DROP TABLE audit_chain",
            "ALTER TABLE audit_log DROP COLUMN hash",
            "ALTER TABLE audit_log DROP COLUMN prev_hash",
            "DROP INDEX ix_accesses_criticality",
            "ALTER TABLE accesses DROP COLUMN criticality",
            "ALTER TABLE review_campaigns DROP COLUMN scope_criticality",
        ):
            db.session.execute(db.text(statement))
        for day in (1, 2, 3):
            db.session.execute(
                db.text(
                    "INSERT INTO audit_log (timestamp, actor, action, target, ip) "
                    "VALUES (:t, 'old', 'auth.login_success', 'old', 'local')"
                ),
                {"t": f"2026-01-0{day} 10:00:00.000000"},
            )
        db.session.commit()

        db.create_all()  # create_app does this before migrations.run(), so the test must too
        migrations.run()
        migrations.run()

        columns = {row[1] for row in db.session.execute(db.text("PRAGMA table_info(audit_log)"))}
        assert {"prev_hash", "hash"} <= columns
        assert db.session.get(Access, old).criticality == "standard"
        starts = db.session.scalars(
            db.select(AuditLog).where(AuditLog.action == "audit.chain_started")
        ).all()
        assert len(starts) == 1 and "legacy_events=3" in starts[0].details
        result = integrity.verify()
        assert result.status == "ok" and result.legacy == 3

        db.session.execute(db.text("UPDATE audit_log SET actor = 'evil' WHERE id = 1"))
        db.session.commit()
        broken = integrity.verify()
        assert broken.status == "broken" and "before the chain" in broken.message
