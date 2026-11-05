import csv
import io
from datetime import datetime, timedelta

from sqlalchemy import and_, case, exists, func, or_, select

from .extensions import db
from .models import (
    CRIT_CRITICAL,
    CRIT_HIGH,
    CRITICALITIES,
    CRITICALITY_LABELS,
    STATUS_ACTIVE,
    STATUS_CLOSED,
    STATUS_EXPIRED,
    STATUS_EXPIRING,
    STATUS_LABELS,
    STATUS_SCHEDULED,
    STATUSES,
    USER_TYPES,
    Access,
    AccessReview,
    AuditLog,
    CampaignItem,
    SystemEntry,
)
from .utils import local_dt, to_utc, utc_text

LONG_DURATION_DAYS = 365
PAGE_SIZE = 25

SEARCH_COLUMNS = (
    Access.name,
    Access.email,
    Access.company,
    Access.access_type,
    Access.system,
    Access.ticket,
)
SORT_COLUMNS = {
    "name": func.lower(Access.name),
    "company": func.lower(Access.company),
    "system": func.lower(Access.system),
    "criticality": case(
        (Access.criticality == CRIT_CRITICAL, 2), (Access.criticality == CRIT_HIGH, 1), else_=0
    ),
    "start": Access.start_at,
    "end": Access.end_at,
    "registered": Access.id,
}
FILTER_KEYS = ("q", "status", "user_type", "criticality", "company", "system")
_FORMULA_PREFIXES = frozenset("=+-@\t\r\n")


def status_clause(status, now, soon_days):
    is_open = Access.closed_at.is_(None)
    if status == STATUS_CLOSED:
        return Access.closed_at.is_not(None)
    if status == STATUS_SCHEDULED:
        return and_(is_open, Access.start_at > now)
    if status == STATUS_EXPIRED:
        return and_(is_open, Access.start_at <= now, Access.end_at <= now)
    if status == STATUS_EXPIRING:
        soon = now + timedelta(days=soon_days)
        return and_(is_open, Access.start_at <= now, Access.end_at > now, Access.end_at <= soon)
    if status == STATUS_ACTIVE:
        return and_(
            is_open, Access.start_at <= now, Access.end_at > now + timedelta(days=soon_days)
        )
    raise ValueError(f"Unknown status: {status}")


def not_reviewed_clause():
    reviewed_in_queue = exists().where(
        AccessReview.access_id == Access.id,
        AccessReview.reviewed_end_at == Access.end_at,
    )
    reviewed_in_campaign = exists().where(
        CampaignItem.access_id == Access.id,
        CampaignItem.reviewed_end_at == Access.end_at,
    )
    return ~or_(reviewed_in_queue, reviewed_in_campaign)


def archived_clause():
    return Access.closed_at.is_not(None)


def scope_select(scope, now, soon_days):
    stmt = select(Access)
    if scope["status"]:
        stmt = stmt.where(status_clause(scope["status"], now, soon_days))
    else:
        stmt = stmt.where(Access.closed_at.is_(None))
    if scope["user_type"]:
        stmt = stmt.where(Access.user_type == scope["user_type"])
    if scope.get("criticality") == CRIT_CRITICAL:
        stmt = stmt.where(Access.criticality == CRIT_CRITICAL)
    elif scope.get("criticality") == CRIT_HIGH:
        stmt = stmt.where(Access.criticality.in_((CRIT_HIGH, CRIT_CRITICAL)))
    if scope["company"]:
        stmt = stmt.where(Access.company == scope["company"])
    if scope["system"]:
        stmt = stmt.where(Access.system == scope["system"])
    return stmt.order_by(Access.end_at.asc(), Access.id.asc())


def status_counts(now, soon_days):
    columns = [
        func.coalesce(func.sum(case((status_clause(status, now, soon_days), 1), else_=0)), 0)
        for status in STATUSES
    ]
    return dict(zip(STATUSES, db.session.execute(select(*columns)).one()))


FIELD_LABELS = {
    "name": "Full name",
    "email": "Email",
    "company": "Company",
    "user_type": "User type",
    "access_type": "Access type",
    "system": "System / target",
    "criticality": "Criticality",
    "description": "Description",
    "justification": "Justification",
    "authorized_by": "Authorized by",
    "ticket": "Ticket",
    "notes": "Notes",
    "start_at": "Start",
}


def history_text(field, value):
    """Readable text for one value in the change history."""
    if value is None or value == "":
        return ""
    if field == "start_at":
        return utc_text(value)
    if field == "criticality":
        return CRITICALITY_LABELS.get(value, value)
    if field == "user_type":
        return str(value).capitalize()
    return str(value)


def expiring_select(now, soon_days):
    return (
        select(Access)
        .where(status_clause(STATUS_EXPIRING, now, soon_days))
        .order_by(Access.end_at.asc(), Access.id.asc())
    )


def criticality_counts(now, soon_days):
    running = or_(
        status_clause(STATUS_ACTIVE, now, soon_days), status_clause(STATUS_EXPIRING, now, soon_days)
    )
    stmt = (
        select(Access.criticality, func.count(Access.id))
        .where(running, Access.criticality.in_((CRIT_HIGH, CRIT_CRITICAL)))
        .group_by(Access.criticality)
    )
    found = dict(db.session.execute(stmt).all())
    return {level: found.get(level, 0) for level in (CRIT_HIGH, CRIT_CRITICAL)}


def duration_warnings(criticality, start, end, config):
    messages = []
    length = end - start
    if length > timedelta(days=LONG_DURATION_DAYS):
        messages.append("This access lasts more than 1 year. Consider reviewing it earlier.")
    limit = {CRIT_CRITICAL: config["CRITICAL_MAX_DAYS"], CRIT_HIGH: config["HIGH_MAX_DAYS"]}.get(criticality)
    if limit and length > timedelta(days=limit):
        messages.append(
            f"This {CRITICALITY_LABELS[criticality]} access lasts more than {limit} days. "
            "Consider a shorter duration or an earlier review."
        )
    return messages


def pending_review_count(now):
    stmt = select(func.count(Access.id)).where(
        status_clause(STATUS_EXPIRED, now, 0), not_reviewed_clause()
    )
    return db.session.scalar(stmt)


def upcoming(now, limit=8):
    stmt = (
        select(Access)
        .where(Access.closed_at.is_(None), Access.end_at > now)
        .order_by(Access.end_at.asc(), Access.id.asc())
        .limit(limit)
    )
    return db.session.scalars(stmt).all()


def parse_filters(args):
    def text(name, limit):
        return (args.get(name) or "").strip()[:limit]

    status = args.get("status", "")
    user_type = args.get("user_type", "")
    criticality = args.get("criticality", "")
    sort = args.get("sort", "")
    direction = args.get("dir", "")
    try:
        page = min(max(1, int(args.get("page", 1))), 100000)
    except (TypeError, ValueError):
        page = 1
    return {
        "q": text("q", 100),
        "status": status if status in STATUSES else "",
        "user_type": user_type if user_type in USER_TYPES else "",
        "criticality": criticality if criticality in CRITICALITIES else "",
        "company": text("company", 120),
        "system": text("system", 120),
        "sort": sort if sort in SORT_COLUMNS else "registered",
        "dir": direction if direction in ("asc", "desc") else "desc",
        "page": page,
        "archived": args.get("archived") == "1",
    }


def filter_args(filters):
    return {key: filters[key] for key in FILTER_KEYS if filters[key]}


def filtered_select(filters, now, soon_days):
    stmt = select(Access)
    if filters["q"]:
        stmt = stmt.where(
            or_(*(column.icontains(filters["q"], autoescape=True) for column in SEARCH_COLUMNS))
        )
    if filters["status"]:
        stmt = stmt.where(status_clause(filters["status"], now, soon_days))
    if filters["user_type"]:
        stmt = stmt.where(Access.user_type == filters["user_type"])
    if filters["criticality"]:
        stmt = stmt.where(Access.criticality == filters["criticality"])
    if filters["company"]:
        stmt = stmt.where(Access.company == filters["company"])
    if filters["system"]:
        stmt = stmt.where(Access.system == filters["system"])
    if filters["archived"]:
        stmt = stmt.where(archived_clause())
    elif not filters["status"]:
        stmt = stmt.where(~archived_clause())
    column = SORT_COLUMNS[filters["sort"]]
    order = column.asc() if filters["dir"] == "asc" else column.desc()
    return stmt.order_by(order, Access.id.desc())


def system_choices(current=None):
    names = db.session.scalars(
        select(SystemEntry.name)
        .where(SystemEntry.enabled.is_(True))
        .order_by(func.lower(SystemEntry.name))
    ).all()
    choices = [("", "Select a system")] + [(name, name) for name in names]
    if current and current not in names:
        choices.append((current, f"{current} (not in the catalog)"))
    return choices


def system_usage():
    stmt = select(Access.system, func.count(Access.id)).group_by(Access.system)
    return dict(db.session.execute(stmt).all())


def distinct_values(column, limit=200):
    stmt = select(column).group_by(column).order_by(func.lower(column)).limit(limit)
    return db.session.scalars(stmt).all()


def csv_safe(value):
    text = "" if value is None else str(value)
    if text[:1] in _FORMULA_PREFIXES or text.lstrip()[:1] in _FORMULA_PREFIXES:
        return "'" + text
    return text


def build_csv(header, rows, delimiter):
    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=delimiter, lineterminator="\r\n")
    writer.writerow([csv_safe(cell) for cell in header])
    for row in rows:
        writer.writerow([csv_safe(cell) for cell in row])
    return buffer.getvalue()


def access_csv(accesses, now, soon_days, timezone_name, delimiter):
    header = [
        "ID", "Name", "Email", "Company", "User type", "Access type", "System / target",
        "Description", "Justification", "Authorized by", "Ticket", "Status", "Criticality",
        f"Start ({timezone_name})", f"End ({timezone_name})", "Notes",
        "Registered by", f"Registered at ({timezone_name})",
        f"Closed at ({timezone_name})", "Closed by", "Close reason",
    ]
    rows = (
        [
            a.id, a.name, a.email, a.company, a.user_type, a.access_type, a.system,
            a.description, a.justification, a.authorized_by, a.ticket,
            STATUS_LABELS[a.status_at(now, soon_days)], CRITICALITY_LABELS.get(a.criticality, a.criticality),
            local_dt(a.start_at), local_dt(a.end_at), a.notes,
            a.created_by, local_dt(a.created_at),
            local_dt(a.closed_at), a.closed_by, a.close_reason,
        ]
        for a in accesses
    )
    return build_csv(header, rows, delimiter)


AUDIT_PAGE_SIZE = 50


def parse_audit_filters(args):
    def text(name, limit):
        return (args.get(name) or "").strip()[:limit]

    def day(name):
        try:
            return datetime.strptime(text(name, 10), "%Y-%m-%d").date()
        except ValueError:
            return None

    try:
        page = min(max(1, int(args.get("page", 1))), 100000)
    except (TypeError, ValueError):
        page = 1
    return {
        "q": text("q", 100),
        "action": text("action", 48),
        "actor": text("actor", 64),
        "date_from": day("date_from"),
        "date_to": day("date_to"),
        "page": page,
    }


def audit_filter_args(filters):
    args = {}
    for key in ("q", "action", "actor"):
        if filters[key]:
            args[key] = filters[key]
    for key in ("date_from", "date_to"):
        if filters[key]:
            args[key] = filters[key].isoformat()
    return args


def audit_select(filters):
    stmt = select(AuditLog)
    if filters["q"]:
        stmt = stmt.where(
            or_(
                AuditLog.target.icontains(filters["q"], autoescape=True),
                AuditLog.details.icontains(filters["q"], autoescape=True),
                AuditLog.ip.icontains(filters["q"], autoescape=True),
            )
        )
    if filters["action"]:
        stmt = stmt.where(AuditLog.action == filters["action"])
    if filters["actor"]:
        stmt = stmt.where(AuditLog.actor.icontains(filters["actor"], autoescape=True))
    if filters["date_from"]:
        stmt = stmt.where(
            AuditLog.timestamp >= to_utc(datetime.combine(filters["date_from"], datetime.min.time()))
        )
    if filters["date_to"]:
        end = datetime.combine(filters["date_to"], datetime.min.time()) + timedelta(days=1)
        stmt = stmt.where(AuditLog.timestamp < to_utc(end))
    return stmt.order_by(AuditLog.id.desc())


def audit_actions():
    return distinct_values(AuditLog.action, limit=500)


def audit_csv(rows, timezone_name, delimiter):
    header = [
        "ID", "Time (UTC)", f"Time ({timezone_name})", "Actor", "Action", "Target", "Details", "IP",
        "Previous hash", "Hash",
    ]
    data = (
        [
            r.id,
            r.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            local_dt(r.timestamp, "%Y-%m-%d %H:%M:%S"),
            r.actor, r.action, r.target, r.details, r.ip,
            r.prev_hash or "", r.hash or "before the chain",
        ]
        for r in rows
    )
    return build_csv(header, data, delimiter)
