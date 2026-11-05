from flask import (
    Blueprint,
    Response,
    current_app,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_login import current_user

from .. import audit, integrity, reviews, services
from ..extensions import db
from ..models import CAMPAIGN_OVERDUE
from ..permissions import admin_required
from ..utils import local_dt, utcnow

bp = Blueprint("main", __name__)


@bp.get("/")
def index():
    now = utcnow()
    soon = current_app.config["EXPIRING_DAYS"]
    campaigns = reviews.open_campaign_rows(now)
    return render_template(
        "home.html",
        counts=services.status_counts(now, soon),
        crit=services.criticality_counts(now, soon),
        pending=services.pending_review_count(now),
        upcoming=services.upcoming(now),
        campaigns=campaigns,
        integrity_due=(
            integrity.reminder(now, current_app.config["INTEGRITY_REMINDER_DAYS"])
            if current_user.is_admin
            else None
        ),
        overdue_campaigns=sum(1 for r in campaigns if r["state"] == CAMPAIGN_OVERDUE),
        soon_days=soon,
        now=now,
    )


@bp.get("/audit")
@admin_required
def audit_log():
    filters = services.parse_audit_filters(request.args)
    page = db.paginate(
        services.audit_select(filters),
        page=filters["page"],
        per_page=services.AUDIT_PAGE_SIZE,
        error_out=False,
    )
    qargs = services.audit_filter_args(filters)
    return render_template(
        "audit.html",
        page=page,
        filters=filters,
        qargs=qargs,
        actions=services.audit_actions(),
    )


@bp.get("/audit/export.csv")
@admin_required
def audit_export():
    filters = services.parse_audit_filters(request.args)
    rows = db.session.scalars(services.audit_select(filters)).all()
    body = services.audit_csv(
        rows, current_app.config["TIMEZONE"], current_app.config["CSV_DELIMITER"]
    )
    audit.record("export.audit_csv", details=f"rows={len(rows)}")
    db.session.commit()
    filename = f"accesskeeper-audit-{local_dt(utcnow(), '%Y%m%d-%H%M')}.csv"
    return Response(
        body.encode("utf-8-sig"),
        content_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@bp.get("/audit/integrity")
@admin_required
def audit_integrity():
    return render_template(
        "audit_integrity.html", chain=integrity.summary(), last_check=integrity.last_check()
    )


@bp.post("/audit/integrity")
@admin_required
def audit_integrity_verify():
    result = integrity.verify()
    details = f"result={result.status} | events={result.checked}"
    if result.broken_id:
        details += f" | first broken event #{result.broken_id}"
    audit.record(integrity.CHECK_ACTION, details=details)
    db.session.commit()
    category = {"ok": "ok", "broken": "error"}.get(result.status, "warn")
    flash(result.message, category)
    return redirect(url_for("main.audit_integrity"))
