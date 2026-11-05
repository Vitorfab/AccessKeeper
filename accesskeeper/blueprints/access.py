from flask import Blueprint, Response, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import select

from .. import audit, reviews, services
from ..extensions import db
from ..forms import AccessCreateForm, AccessEditForm, CloseForm, RenewForm
from ..models import (
    STATUS_ACTIVE,
    STATUS_EXPIRED,
    STATUS_EXPIRING,
    STATUS_SCHEDULED,
    STATUSES,
    CRITICALITIES,
    CRITICALITY_LABELS,
    Access,
    AccessRenewal,
    AuditLog,
)
from ..permissions import admin_required
from ..utils import local_dt, local_input, to_local, to_utc, utc_text, utcnow

bp = Blueprint("access", __name__, url_prefix="/access")

FIELDS = (
    "name", "email", "company", "user_type", "access_type", "system", "criticality",
    "description", "justification", "authorized_by", "ticket", "notes",
)
OPTIONAL_FIELDS = ("ticket", "notes")
TRAIL_LIMIT = 100
CLOSABLE = (STATUS_ACTIVE, STATUS_EXPIRING, STATUS_SCHEDULED)
COPY_FIELDS = (
    "name", "email", "company", "user_type", "access_type", "system", "criticality",
    "description", "justification", "authorized_by",
)


def _soon_days():
    return current_app.config["EXPIRING_DAYS"]


def _target(access):
    return f"access:{access.id}"


def _detail(access):
    return redirect(url_for("access.detail", access_id=access.id))


def _value(form, name):
    value = getattr(form, name).data
    return (value or None) if name in OPTIONAL_FIELDS else value


def _warn_if_long(criticality, start, end):
    for message in services.duration_warnings(criticality, start, end, current_app.config):
        flash(message, "warn")


def _source_access():
    try:
        source_id = int(request.args.get("source", ""))
    except ValueError:
        return None
    source = db.session.get(Access, source_id)
    return source if source is not None and source.closed_at is not None else None


def _form_page(form, mode, access=None, source=None):
    return render_template(
        "access/form.html",
        form=form,
        mode=mode,
        access=access,
        access_types=services.distinct_values(Access.access_type),
        source=source,
        warn_days=services.LONG_DURATION_DAYS,
    )


def _list_page(archived):
    now = utcnow()
    soon = _soon_days()
    filters = services.parse_filters(request.args)
    filters["archived"] = archived
    if archived:
        filters["status"] = ""
    page = db.paginate(
        services.filtered_select(filters, now, soon),
        page=filters["page"],
        per_page=services.PAGE_SIZE,
        error_out=False,
    )
    qargs = services.filter_args(filters)
    return render_template(
        "access/list.html",
        page=page,
        rows=[(a, a.status_at(now, soon)) for a in page.items],
        filters=filters,
        qargs=qargs,
        base_args={**qargs, "sort": filters["sort"], "dir": filters["dir"]},
        statuses=STATUSES,
        criticalities=CRITICALITIES,
        companies=services.distinct_values(Access.company),
        systems=services.distinct_values(Access.system),
        archived=archived,
        endpoint="access.archive" if archived else "access.index",
        now=now,
    )


@bp.get("/")
def index():
    return _list_page(False)


@bp.get("/archive")
def archive():
    return _list_page(True)


@bp.get("/export.csv")
def export_csv():
    now = utcnow()
    soon = _soon_days()
    filters = services.parse_filters(request.args)
    accesses = db.session.scalars(services.filtered_select(filters, now, soon)).all()
    body = services.access_csv(
        accesses,
        now,
        soon,
        current_app.config["TIMEZONE"],
        current_app.config["CSV_DELIMITER"],
    )
    audit.record(
        "export.access_csv",
        details=f"rows={len(accesses)}" + (" | archive" if filters["archived"] else ""),
    )
    db.session.commit()
    filename = f"accesskeeper-accesses-{local_dt(now, '%Y%m%d-%H%M')}.csv"
    return Response(
        body.encode("utf-8-sig"),
        content_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@bp.route("/new", methods=["GET", "POST"])
@admin_required
def create():
    form = AccessCreateForm()
    form.system.choices = services.system_choices()
    source = _source_access()
    if request.method == "GET":
        form.start_at.data = to_local(utcnow()).replace(second=0, microsecond=0)
        if source is not None:
            for name in COPY_FIELDS:
                getattr(form, name).data = getattr(source, name)
            flash(
                f"Prefilled from the closed access of {source.name}. Check every field and set the dates.",
                "info",
            )
    if form.validate_on_submit():
        start = to_utc(form.start_at.data)
        end = to_utc(form.end_at.data)
        access = Access(
            start_at=start,
            end_at=end,
            created_by=current_user.username,
            **{name: _value(form, name) for name in FIELDS},
        )
        db.session.add(access)
        db.session.flush()
        audit.record(
            "access.created",
            target=_target(access),
            details=f"{access.name} | {access.system} | {CRITICALITY_LABELS[access.criticality]} | ends {utc_text(end)}"
            + (f" | registered again from access:{source.id}" if source is not None else ""),
        )
        db.session.commit()
        flash(f"Access for {access.name} registered.", "ok")
        _warn_if_long(access.criticality, start, end)
        return _detail(access)
    return _form_page(form, "create", source=source)


@bp.get("/<int:access_id>")
def detail(access_id):
    access = db.get_or_404(Access, access_id)
    now = utcnow()
    status = access.status_at(now, _soon_days())
    trail = db.session.scalars(
        select(AuditLog)
        .where(AuditLog.target == _target(access))
        .order_by(AuditLog.id.desc())
        .limit(TRAIL_LIMIT)
    ).all()
    return render_template(
        "access/detail.html",
        access=access,
        status=status,
        can_close=status in CLOSABLE,
        trail=trail,
        review_entries=reviews.review_history(access.id),
        needs_review=status == STATUS_EXPIRED and reviews.awaiting_review(access, now),
        now=now,
    )


@bp.route("/<int:access_id>/edit", methods=["GET", "POST"])
@admin_required
def edit(access_id):
    access = db.get_or_404(Access, access_id)
    if access.closed_at is not None:
        flash("Closed accesses are read-only.", "error")
        return _detail(access)
    form = AccessEditForm(obj=access)
    form.system.choices = services.system_choices(access.system)
    if request.method == "GET":
        form.start_at.data = to_local(access.start_at)
    if form.validate_on_submit():
        start = to_utc(form.start_at.data)
        if start >= access.end_at:
            form.start_at.errors = list(form.start_at.errors) + [
                "The start must be before the current end date."
            ]
        else:
            changed = [n for n in FIELDS if getattr(access, n) != _value(form, n)]
            if access.start_at != start:
                changed.append("start_at")
            if not changed:
                flash("No changes to save.", "info")
                return _detail(access)
            for name in FIELDS:
                setattr(access, name, _value(form, name))
            access.start_at = start
            access.updated_at = utcnow()
            access.updated_by = current_user.username
            audit.record("access.edited", target=_target(access), details="changed: " + ", ".join(changed))
            db.session.commit()
            flash("Access updated.", "ok")
            if "criticality" in changed:
                _warn_if_long(access.criticality, access.start_at, access.end_at)
            return _detail(access)
    return _form_page(form, "edit", access)


@bp.route("/<int:access_id>/renew", methods=["GET", "POST"])
@admin_required
def renew(access_id):
    access = db.get_or_404(Access, access_id)
    if access.closed_at is not None:
        flash("Closed accesses cannot be renewed. Register a new access instead.", "error")
        return _detail(access)
    now = utcnow()
    form = RenewForm()
    if form.validate_on_submit():
        new_end = to_utc(form.new_end_at.data)
        if new_end <= max(access.end_at, now):
            form.new_end_at.errors = list(form.new_end_at.errors) + [
                "The new end must be in the future and later than the current end."
            ]
        else:
            previous = access.end_at
            db.session.add(
                AccessRenewal(
                    access_id=access.id,
                    previous_end_at=previous,
                    new_end_at=new_end,
                    reason=form.reason.data,
                    renewed_by=current_user.username,
                )
            )
            access.end_at = new_end
            access.updated_at = now
            access.updated_by = current_user.username
            audit.record(
                "access.renewed",
                target=_target(access),
                details=f"{utc_text(previous)} -> {utc_text(new_end)}; reason: {form.reason.data}",
            )
            db.session.commit()
            flash("Access renewed.", "ok")
            _warn_if_long(access.criticality, access.start_at, new_end)
            return _detail(access)
    return render_template(
        "access/renew.html",
        form=form,
        access=access,
        start_input=local_input(access.start_at),
        base_input=local_input(max(access.end_at, now)),
        warn_days=services.LONG_DURATION_DAYS,
        level=access.criticality,
    )


@bp.route("/<int:access_id>/close", methods=["GET", "POST"])
@admin_required
def close(access_id):
    access = db.get_or_404(Access, access_id)
    now = utcnow()
    if access.status_at(now, _soon_days()) not in CLOSABLE:
        flash("Expired accesses are closed through the review queue.", "error")
        return _detail(access)
    form = CloseForm()
    if form.validate_on_submit():
        access.closed_at = now
        access.closed_by = current_user.username
        access.close_reason = form.reason.data
        access.updated_at = now
        access.updated_by = current_user.username
        audit.record("access.closed", target=_target(access), details=f"reason: {form.reason.data}")
        db.session.commit()
        flash("Access closed.", "ok")
        return _detail(access)
    return render_template("access/close.html", form=form, access=access)
