from sqlalchemy import func, select

from . import audit, integrity
from .extensions import db
from .models import Access, AccessReview, CampaignItem, OUTCOME_REMOVED, SystemEntry

DEFAULT_SYSTEMS = (
    "Identity provider / Directory",
    "Microsoft 365",
    "Google Workspace",
    "Email security gateway",
    "Cloud provider",
    "Servers",
    "Virtualization",
    "Database",
    "Storage and file shares",
    "Backup and recovery",
    "Firewall",
    "Web application firewall",
    "VPN / Remote access",
    "Network devices",
    "Endpoint security (EDR / antivirus)",
    "SIEM / Log management",
    "Privileged access / Password vault",
    "DNS and domain registrar",
    "Source code and CI/CD",
    "ITSM / Ticketing",
    "Business application (ERP / CRM)",
)


NEW_COLUMNS = (
    ("audit_log", "prev_hash", "VARCHAR(64)"),
    ("audit_log", "hash", "VARCHAR(64)"),
    ("accesses", "criticality", "VARCHAR(16) NOT NULL DEFAULT 'standard'"),
    ("review_campaigns", "scope_criticality", "VARCHAR(16)"),
)


def add_columns():
    if db.engine.dialect.name != "sqlite":
        return
    existing = {}
    for table, column, ddl in NEW_COLUMNS:
        if table not in existing:
            rows = db.session.execute(db.text(f"PRAGMA table_info({table})")).all()
            existing[table] = {row[1] for row in rows}
        if column not in existing[table]:
            db.session.execute(db.text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
    db.session.execute(
        db.text("CREATE INDEX IF NOT EXISTS ix_accesses_criticality ON accesses (criticality)")
    )
    db.session.commit()


def seed_systems():
    if db.session.scalar(select(func.count(SystemEntry.id))):
        return
    names = list(DEFAULT_SYSTEMS)
    seen = {name.lower() for name in names}
    for value in db.session.scalars(select(Access.system).group_by(Access.system)):
        value = (value or "").strip()
        if value and value.lower() not in seen:
            seen.add(value.lower())
            names.append(value)
    db.session.add_all(SystemEntry(name=name, created_by="system") for name in names)


def close_removed_accesses():
    closed = set()
    sources = (
        select(Access, AccessReview.reviewed_by, AccessReview.reviewed_at).join(
            AccessReview, AccessReview.access_id == Access.id
        ).where(
            Access.closed_at.is_(None),
            AccessReview.outcome == OUTCOME_REMOVED,
            AccessReview.reviewed_end_at == Access.end_at,
        ),
        select(Access, CampaignItem.reviewed_by, CampaignItem.reviewed_at).join(
            CampaignItem, CampaignItem.access_id == Access.id
        ).where(
            Access.closed_at.is_(None),
            CampaignItem.outcome == OUTCOME_REMOVED,
            CampaignItem.reviewed_end_at == Access.end_at,
        ),
    )
    for stmt in sources:
        for access, reviewer, reviewed_at in db.session.execute(stmt).all():
            if access.id in closed:
                continue
            closed.add(access.id)
            access.closed_at = reviewed_at
            access.closed_by = reviewer
            access.close_reason = "Removal confirmed in review"
            access.updated_at = reviewed_at
            access.updated_by = reviewer
            audit.record(
                "access.closed",
                target=f"access:{access.id}",
                details="reason: removal confirmed in review (applied when upgrading)",
                actor="system",
            )


def run():
    add_columns()
    integrity.start_chain()
    seed_systems()
    close_removed_accesses()
    db.session.commit()
