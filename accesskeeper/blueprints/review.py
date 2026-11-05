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
from sqlalchemy import select

from .. import audit, reviews, services
from ..extensions import db
from ..forms import BulkReviewForm, CampaignForm, ReviewForm
from ..models import (
    CAMPAIGN_LABELS,
    OUTCOME_LABELS,
    OUTCOME_REMOVED,
    OUTCOME_RENEWED,
    OUTCOMES,
    Access,
    CampaignItem,
    ReviewCampaign,
)
from ..permissions import admin_required
from ..reviews import QUEUE_OUTCOMES, ReviewError
from ..utils import local_dt, to_local, utcnow

bp = Blueprint("review", __name__, url_prefix="/review")

ITEM_PAGE_SIZE = 50
ITEM_FILTERS = ("all", "pending", "reviewed")


def _soon_days():
    return current_app.config["EXPIRING_DAYS"]


def _campaign_page(campaign):
    return redirect(url_for("review.campaign_detail", campaign_id=campaign.id))


def _fill_scope_choices(form):
    form.company.choices = [("", "Any company")] + [
        (v, v) for v in services.distinct_values(Access.company)
    ]
    form.system.choices = [("", "Any system")] + [
        (v, v) for v in services.distinct_values(Access.system)
    ]


def _outcome_choices(outcomes):
    return [(o, OUTCOME_LABELS[o]) for o in outcomes]


@bp.get("/")
def queue():
    now = utcnow()
    try:
        number = max(1, int(request.args.get("page", 1)))
    except ValueError:
        number = 1
    page = db.paginate(
        reviews.pending_select(now), page=min(number, 100000), per_page=services.PAGE_SIZE, error_out=False
    )
    return render_template("review/queue.html", page=page, now=now)


@bp.route("/queue/<int:access_id>", methods=["GET", "POST"])
@admin_required
def queue_item(access_id):
    access = db.get_or_404(Access, access_id)
    now = utcnow()
    if not reviews.awaiting_review(access, now):
        flash("This access is not waiting for review.", "info")
        return redirect(url_for("review.queue"))
    form = ReviewForm()
    form.outcome.choices = _outcome_choices(QUEUE_OUTCOMES)
    if form.validate_on_submit():
        try:
            reviews.record_queue_review(access, form.outcome.data, form.justification.data, current_user, now)
        except ReviewError as error:
            db.session.rollback()
            flash(str(error), "error")
        else:
            db.session.commit()
            flash("Review recorded.", "ok")
            return redirect(url_for("review.queue"))
    return render_template("review/item.html", form=form, access=access, campaign=None, now=now)


@bp.get("/campaigns")
def campaigns():
    now = utcnow()
    items = db.session.scalars(
        select(ReviewCampaign).order_by(ReviewCampaign.id.desc()).limit(200)
    ).all()
    return render_template("review/campaigns.html", rows=reviews.campaign_rows(items, now))


@bp.route("/campaigns/new", methods=["GET", "POST"])
@admin_required
def campaign_new():
    now = utcnow()
    form = CampaignForm()
    _fill_scope_choices(form)
    if request.method == "GET":
        form.start_date.data = to_local(now).date()
    if form.validate_on_submit():
        values = {
            "name": form.name.data,
            "start_date": form.start_date.data,
            "due_date": form.due_date.data,
            "status": form.status.data,
            "user_type": form.user_type.data,
            "criticality": form.criticality.data,
            "company": form.company.data,
            "system": form.system.data,
        }
        try:
            campaign = reviews.create_campaign(values, current_user, now, _soon_days())
        except ReviewError as error:
            db.session.rollback()
            flash(str(error), "error")
        else:
            db.session.commit()
            flash("Campaign created. The matching accesses were added to it.", "ok")
            return _campaign_page(campaign)
    return render_template("review/campaign_new.html", form=form)


@bp.get("/campaigns/<int:campaign_id>")
def campaign_detail(campaign_id):
    campaign = db.get_or_404(ReviewCampaign, campaign_id)
    now = utcnow()
    show = request.args.get("show", "all")
    if show not in ITEM_FILTERS:
        show = "all"
    stmt = (
        select(CampaignItem)
        .join(Access, Access.id == CampaignItem.access_id)
        .where(CampaignItem.campaign_id == campaign.id)
        .order_by(Access.end_at.asc(), CampaignItem.id.asc())
    )
    if show == "pending":
        stmt = stmt.where(CampaignItem.outcome.is_(None))
    elif show == "reviewed":
        stmt = stmt.where(CampaignItem.outcome.is_not(None))
    try:
        number = max(1, int(request.args.get("page", 1)))
    except ValueError:
        number = 1
    page = db.paginate(stmt, page=min(number, 100000), per_page=ITEM_PAGE_SIZE, error_out=False)
    soon = _soon_days()
    row = reviews.campaign_rows([campaign], now)[0]
    can_review = current_user.is_admin and campaign.closed_at is None and campaign.start_at <= now
    return render_template(
        "review/campaign_detail.html",
        row=row,
        campaign=campaign,
        scope=reviews.scope_text(campaign),
        page=page,
        entries=[(item, item.access.status_at(now, soon)) for item in page.items],
        show=show,
        now=now,
        can_review=can_review,
        bulk_form=BulkReviewForm() if can_review else None,
    )


def _bulk_item_ids():
    ids = []
    for raw in request.form.getlist("item_ids"):
        try:
            value = int(raw)
        except ValueError:
            continue
        if value not in ids:
            ids.append(value)
    return ids


@bp.post("/campaigns/<int:campaign_id>/bulk")
@admin_required
def campaign_bulk(campaign_id):
    campaign = db.get_or_404(ReviewCampaign, campaign_id)
    form = BulkReviewForm()
    item_ids = _bulk_item_ids()
    if not form.validate_on_submit():
        for errors in form.errors.values():
            for message in errors:
                flash(message, "error")
        return _campaign_page(campaign)
    if not item_ids:
        flash("Select at least one item.", "error")
        return _campaign_page(campaign)

    now = utcnow()
    outcome = form.outcome.data
    checked, problems = reviews.plan_bulk_review(
        campaign, item_ids, outcome, form.justification.data, now, _soon_days()
    )
    if problems:
        return render_template(
            "review/bulk.html", campaign=campaign, problems=problems, items=[], form=form,
            outcome=outcome, item_ids=item_ids, confirm=False,
        )
    needs_confirmation = outcome == OUTCOME_REMOVED and request.form.get("confirm") != "1"
    if needs_confirmation:
        return render_template(
            "review/bulk.html", campaign=campaign, problems=[], items=[c[0] for c in checked],
            form=form, outcome=outcome, item_ids=item_ids, confirm=True,
        )
    try:
        reviews.apply_bulk_review(campaign, checked, outcome, current_user, now)
    except ReviewError as error:
        db.session.rollback()
        flash(str(error), "error")
    else:
        db.session.commit()
        flash(f"Recorded {len(checked)} reviews: {OUTCOME_LABELS[outcome]}.", "ok")
    return _campaign_page(campaign)


def _report_context(campaign):
    now = utcnow()
    row = reviews.campaign_rows([campaign], now)[0]
    return now, row, reviews.report_rows(campaign.id)


@bp.get("/campaigns/<int:campaign_id>/report")
def campaign_report(campaign_id):
    campaign = db.get_or_404(ReviewCampaign, campaign_id)
    now, row, rows = _report_context(campaign)
    soon = _soon_days()
    return render_template(
        "review/report.html",
        campaign=campaign,
        row=row,
        scope=reviews.scope_text(campaign),
        totals=reviews.outcome_totals(campaign.id),
        items=rows,
        generated_at=now,
        soon=soon,
    )


@bp.get("/campaigns/<int:campaign_id>/report.csv")
def campaign_report_csv(campaign_id):
    campaign = db.get_or_404(ReviewCampaign, campaign_id)
    now, row, rows = _report_context(campaign)
    body = reviews.report_csv(
        campaign,
        CAMPAIGN_LABELS[row["state"]],
        rows,
        current_app.config["TIMEZONE"],
        current_app.config["CSV_DELIMITER"],
    )
    audit.record(
        "export.campaign_csv", target=f"campaign:{campaign.id}", details=f"rows={len(rows)}"
    )
    db.session.commit()
    filename = f"accesskeeper-campaign-{campaign.id}-{local_dt(now, '%Y%m%d-%H%M')}.csv"
    return Response(
        body.encode("utf-8-sig"),
        content_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@bp.post("/campaigns/<int:campaign_id>/close")
@admin_required
def campaign_close(campaign_id):
    campaign = db.get_or_404(ReviewCampaign, campaign_id)
    try:
        reviews.close_campaign(campaign, current_user, utcnow())
    except ReviewError as error:
        db.session.rollback()
        flash(str(error), "error")
    else:
        db.session.commit()
        flash("Campaign closed.", "ok")
    return _campaign_page(campaign)


@bp.route("/campaigns/<int:campaign_id>/items/<int:item_id>", methods=["GET", "POST"])
@admin_required
def campaign_item(campaign_id, item_id):
    campaign = db.get_or_404(ReviewCampaign, campaign_id)
    item = db.get_or_404(CampaignItem, item_id)
    if item.campaign_id != campaign.id:
        return redirect(url_for("review.campaigns"))
    now = utcnow()
    if item.outcome is not None or campaign.closed_at is not None:
        flash("This item can no longer be reviewed.", "info")
        return _campaign_page(campaign)
    form = ReviewForm()
    form.outcome.choices = _outcome_choices(OUTCOMES)
    if form.validate_on_submit():
        try:
            reviews.record_campaign_review(
                campaign, item, form.outcome.data, form.justification.data,
                current_user, now, _soon_days(),
            )
        except ReviewError as error:
            db.session.rollback()
            flash(str(error), "error")
        else:
            db.session.commit()
            flash("Review recorded.", "ok")
            return _campaign_page(campaign)
    return render_template(
        "review/item.html", form=form, access=item.access, campaign=campaign, now=now,
        renewed_outcome=OUTCOME_RENEWED,
    )
