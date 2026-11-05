import hashlib
from datetime import timedelta

from flask_login import UserMixin
from sqlalchemy import DDL, event, func, select
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db, login_manager
from .utils import utcnow

ROLE_ADMIN = "admin"
ROLE_VIEWER = "viewer"
ROLES = (ROLE_ADMIN, ROLE_VIEWER)

USER_TYPES = ("internal", "external")

STATUS_ACTIVE = "active"
STATUS_EXPIRING = "expiring"
STATUS_SCHEDULED = "scheduled"
STATUS_EXPIRED = "expired"
STATUS_CLOSED = "closed"
STATUSES = (STATUS_ACTIVE, STATUS_EXPIRING, STATUS_SCHEDULED, STATUS_EXPIRED, STATUS_CLOSED)
STATUS_LABELS = {
    STATUS_ACTIVE: "Active",
    STATUS_EXPIRING: "Expiring soon",
    STATUS_SCHEDULED: "Scheduled",
    STATUS_EXPIRED: "Expired",
    STATUS_CLOSED: "Closed",
}
STATUS_BADGES = {
    STATUS_ACTIVE: "ok",
    STATUS_EXPIRING: "warn",
    STATUS_SCHEDULED: "info",
    STATUS_EXPIRED: "bad",
    STATUS_CLOSED: "",
}

CRIT_STANDARD = "standard"
CRIT_HIGH = "high"
CRIT_CRITICAL = "critical"
CRITICALITIES = (CRIT_STANDARD, CRIT_HIGH, CRIT_CRITICAL)
CRITICALITY_LABELS = {CRIT_STANDARD: "Standard", CRIT_HIGH: "High", CRIT_CRITICAL: "Critical"}
CRITICALITY_BADGES = {CRIT_STANDARD: "", CRIT_HIGH: "warn", CRIT_CRITICAL: "bad"}
CRITICALITY_SCOPES = {CRIT_CRITICAL: "Critical only", CRIT_HIGH: "High and above"}

OUTCOME_REMOVED = "removal_confirmed"
OUTCOME_RENEWED = "renewed"
OUTCOME_KEPT = "kept"
OUTCOMES = (OUTCOME_REMOVED, OUTCOME_RENEWED, OUTCOME_KEPT)
OUTCOME_LABELS = {
    OUTCOME_REMOVED: "Removal confirmed",
    OUTCOME_RENEWED: "Renewed",
    OUTCOME_KEPT: "Kept with justification",
}

CAMPAIGN_ACTIVE = "active"
CAMPAIGN_OVERDUE = "overdue"
CAMPAIGN_SCHEDULED = "scheduled"
CAMPAIGN_COMPLETED = "completed"
CAMPAIGN_CLOSED = "closed"
CAMPAIGN_LABELS = {
    CAMPAIGN_ACTIVE: "In progress",
    CAMPAIGN_OVERDUE: "Overdue",
    CAMPAIGN_SCHEDULED: "Scheduled",
    CAMPAIGN_COMPLETED: "All reviewed",
    CAMPAIGN_CLOSED: "Closed",
}
CAMPAIGN_BADGES = {
    CAMPAIGN_ACTIVE: "warn",
    CAMPAIGN_OVERDUE: "bad",
    CAMPAIGN_SCHEDULED: "",
    CAMPAIGN_COMPLETED: "ok",
    CAMPAIGN_CLOSED: "",
}

_HASH_METHOD = "scrypt" if hasattr(hashlib, "scrypt") else "pbkdf2:sha256:600000"
DUMMY_HASH = generate_password_hash("accesskeeper-dummy-password", method=_HASH_METHOD)


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(32), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(256), nullable=False)
    role = db.Column(db.String(16), nullable=False, default=ROLE_VIEWER)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    session_version = db.Column(db.Integer, nullable=False, default=1)
    failed_logins = db.Column(db.Integer, nullable=False, default=0)
    locked_until = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    last_login_at = db.Column(db.DateTime)

    @property
    def is_active(self):
        return bool(self.enabled)

    @property
    def is_admin(self):
        return self.role == ROLE_ADMIN

    @property
    def is_master(self):
        return self.id is not None and self.id == master_id()

    def get_id(self):
        return f"{self.id}:{self.session_version}"

    def set_password(self, password):
        self.password_hash = generate_password_hash(password, method=_HASH_METHOD)
        self.session_version = (self.session_version or 0) + 1

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    def is_locked(self, now=None):
        return self.locked_until is not None and self.locked_until > (now or utcnow())

    def register_failure(self, max_attempts, lockout_minutes):
        self.failed_logins = (self.failed_logins or 0) + 1
        if self.failed_logins >= max_attempts:
            self.locked_until = utcnow() + timedelta(minutes=lockout_minutes)
            self.failed_logins = 0
            return True
        return False

    def register_success(self):
        self.failed_logins = 0
        self.locked_until = None
        self.last_login_at = utcnow()


def master_id():
    return db.session.scalar(select(func.min(User.id)))


class AuditLog(db.Model):
    __tablename__ = "audit_log"

    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, nullable=False, default=utcnow, index=True)
    actor = db.Column(db.String(64), nullable=False)
    action = db.Column(db.String(48), nullable=False, index=True)
    target = db.Column(db.String(200))
    details = db.Column(db.String(500))
    ip = db.Column(db.String(45))
    prev_hash = db.Column(db.String(64))
    hash = db.Column(db.String(64))


audit_chain = db.Table(
    "audit_chain",
    db.Column("id", db.Integer, primary_key=True),
    db.Column("last_hash", db.String(64), nullable=False),
)


class Access(db.Model):
    __tablename__ = "accesses"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(254), nullable=False)
    company = db.Column(db.String(120), nullable=False, index=True)
    user_type = db.Column(db.String(16), nullable=False, index=True)
    access_type = db.Column(db.String(120), nullable=False)
    system = db.Column(db.String(120), nullable=False, index=True)
    criticality = db.Column(
        db.String(16), nullable=False, default=CRIT_STANDARD, server_default=CRIT_STANDARD, index=True
    )
    description = db.Column(db.String(1000), nullable=False)
    justification = db.Column(db.String(1000), nullable=False)
    authorized_by = db.Column(db.String(120), nullable=False)
    ticket = db.Column(db.String(64))
    start_at = db.Column(db.DateTime, nullable=False)
    end_at = db.Column(db.DateTime, nullable=False, index=True)
    notes = db.Column(db.String(2000))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    created_by = db.Column(db.String(64), nullable=False)
    updated_at = db.Column(db.DateTime)
    updated_by = db.Column(db.String(64))
    closed_at = db.Column(db.DateTime, index=True)
    closed_by = db.Column(db.String(64))
    close_reason = db.Column(db.String(300))

    renewals = db.relationship("AccessRenewal", order_by="AccessRenewal.id.desc()")

    def status_at(self, now, soon_days):
        if self.closed_at is not None:
            return STATUS_CLOSED
        if self.start_at > now:
            return STATUS_SCHEDULED
        if self.end_at <= now:
            return STATUS_EXPIRED
        if self.end_at <= now + timedelta(days=soon_days):
            return STATUS_EXPIRING
        return STATUS_ACTIVE


class AccessChange(db.Model):
    __tablename__ = "access_changes"

    id = db.Column(db.Integer, primary_key=True)
    access_id = db.Column(db.Integer, db.ForeignKey("accesses.id"), nullable=False, index=True)
    field = db.Column(db.String(40), nullable=False)
    old_value = db.Column(db.String(2000), nullable=False, default="")
    new_value = db.Column(db.String(2000), nullable=False, default="")
    changed_by = db.Column(db.String(64), nullable=False)
    changed_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class SystemEntry(db.Model):
    __tablename__ = "systems"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    created_by = db.Column(db.String(64), nullable=False)


class AccessRenewal(db.Model):
    __tablename__ = "access_renewals"

    id = db.Column(db.Integer, primary_key=True)
    access_id = db.Column(db.Integer, db.ForeignKey("accesses.id"), nullable=False, index=True)
    previous_end_at = db.Column(db.DateTime, nullable=False)
    new_end_at = db.Column(db.DateTime, nullable=False)
    reason = db.Column(db.String(300), nullable=False)
    renewed_by = db.Column(db.String(64), nullable=False)
    renewed_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class AccessReview(db.Model):
    __tablename__ = "access_reviews"

    id = db.Column(db.Integer, primary_key=True)
    access_id = db.Column(db.Integer, db.ForeignKey("accesses.id"), nullable=False, index=True)
    reviewed_end_at = db.Column(db.DateTime, nullable=False)
    outcome = db.Column(db.String(24), nullable=False)
    justification = db.Column(db.String(500))
    reviewed_by = db.Column(db.String(64), nullable=False)
    reviewed_at = db.Column(db.DateTime, nullable=False, default=utcnow)


class ReviewCampaign(db.Model):
    __tablename__ = "review_campaigns"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    start_at = db.Column(db.DateTime, nullable=False)
    due_at = db.Column(db.DateTime, nullable=False)
    scope_status = db.Column(db.String(16))
    scope_user_type = db.Column(db.String(16))
    scope_company = db.Column(db.String(120))
    scope_system = db.Column(db.String(120))
    scope_criticality = db.Column(db.String(16))
    created_at = db.Column(db.DateTime, nullable=False, default=utcnow)
    created_by = db.Column(db.String(64), nullable=False)
    closed_at = db.Column(db.DateTime)
    closed_by = db.Column(db.String(64))

    items = db.relationship("CampaignItem", back_populates="campaign", order_by="CampaignItem.id")


class CampaignItem(db.Model):
    __tablename__ = "campaign_items"
    __table_args__ = (db.UniqueConstraint("campaign_id", "access_id"),)

    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(
        db.Integer, db.ForeignKey("review_campaigns.id"), nullable=False, index=True
    )
    access_id = db.Column(db.Integer, db.ForeignKey("accesses.id"), nullable=False, index=True)
    status_snapshot = db.Column(db.String(16), nullable=False)
    end_at_snapshot = db.Column(db.DateTime, nullable=False)
    outcome = db.Column(db.String(24))
    justification = db.Column(db.String(500))
    reviewed_by = db.Column(db.String(64))
    reviewed_at = db.Column(db.DateTime)
    reviewed_end_at = db.Column(db.DateTime)

    campaign = db.relationship("ReviewCampaign", back_populates="items")
    access = db.relationship("Access")


def _raiser(message):
    def guard(mapper, connection, target):
        raise RuntimeError(message)

    return guard


def _protect(model, operations, message):
    table = model.__table__.name
    for operation in operations:
        event.listen(model, f"before_{operation.lower()}", _raiser(message))
        event.listen(
            model.__table__,
            "after_create",
            DDL(
                f"CREATE TRIGGER {table}_no_{operation.lower()} "
                f"BEFORE {operation} ON {table} "
                f"BEGIN SELECT RAISE(ABORT, '{message}'); END"
            ).execute_if(dialect="sqlite"),
        )


_protect(AuditLog, ("UPDATE", "DELETE"), "audit_log is append-only")
_protect(Access, ("DELETE",), "accesses are never deleted")
_protect(SystemEntry, ("DELETE",), "systems are never deleted, disable them instead")
_protect(AccessRenewal, ("UPDATE", "DELETE"), "renewal history is append-only")
_protect(AccessChange, ("UPDATE", "DELETE"), "change history is append-only")
_protect(AccessReview, ("DELETE",), "review history is never deleted")
_protect(ReviewCampaign, ("DELETE",), "campaigns are never deleted")
_protect(CampaignItem, ("DELETE",), "campaign items are never deleted")
event.listen(
    CampaignItem.__table__,
    "after_create",
    DDL(
        "CREATE TRIGGER campaign_items_reviewed_is_final "
        "BEFORE UPDATE ON campaign_items WHEN OLD.outcome IS NOT NULL "
        "BEGIN SELECT RAISE(ABORT, 'a recorded review outcome cannot be changed'); END"
    ).execute_if(dialect="sqlite"),
)


@login_manager.user_loader
def load_user(session_id):
    try:
        user_id, version = session_id.split(":")
        user = db.session.get(User, int(user_id))
        version = int(version)
    except (ValueError, AttributeError):
        return None
    if user is not None and user.enabled and user.session_version == version:
        return user
    return None
