from datetime import datetime, time

from sqlalchemy import func, select

from . import audit
from .extensions import db
from .models import (
    CAMPAIGN_ACTIVE,
    CAMPAIGN_CLOSED,
    CAMPAIGN_COMPLETED,
    CAMPAIGN_OVERDUE,
    CAMPAIGN_SCHEDULED,
    CRITICALITY_LABELS,
    CRITICALITY_SCOPES,
    OUTCOME_KEPT,
    OUTCOME_LABELS,
    OUTCOME_REMOVED,
    OUTCOME_RENEWED,
    OUTCOMES,
    STATUS_CLOSED,
    STATUS_EXPIRED,
    STATUS_LABELS,
    Access,
    AccessReview,
    CampaignItem,
    ReviewCampaign,
)
from .services import build_csv, not_reviewed_clause, scope_select, status_clause
from .utils import local_dt, to_utc, utc_text

QUEUE_OUTCOMES = (OUTCOME_REMOVED, OUTCOME_KEPT)
BULK_LIMIT = 200


class ReviewError(Exception):
    pass


def _target(access):
    return f"access:{access.id}"


def _close(access, user, now, reason):
    access.closed_at = now
    access.closed_by = user.username
    access.close_reason = reason[:300]
    access.updated_at = now
    access.updated_by = user.username
    audit.record("access.closed", target=_target(access), details=f"reason: {access.close_reason}")


def _require_justification(outcome, justification):
    if outcome == OUTCOME_KEPT and len((justification or "").strip()) < 3:
        raise ReviewError("A justification is required to keep an access.")


def campaign_state(campaign, now, total, reviewed):
    if campaign.closed_at is not None:
        return CAMPAIGN_CLOSED
    if campaign.start_at > now:
        return CAMPAIGN_SCHEDULED
    if total and reviewed >= total:
        return CAMPAIGN_COMPLETED
    if campaign.due_at < now:
        return CAMPAIGN_OVERDUE
    return CAMPAIGN_ACTIVE


def progress_for(campaign_ids):
    if not campaign_ids:
        return {}
    stmt = (
        select(CampaignItem.campaign_id, func.count(CampaignItem.id), func.count(CampaignItem.outcome))
        .where(CampaignItem.campaign_id.in_(campaign_ids))
        .group_by(CampaignItem.campaign_id)
    )
    return {cid: (total, done) for cid, total, done in db.session.execute(stmt)}


def campaign_rows(campaigns, now):
    progress = progress_for([c.id for c in campaigns])
    rows = []
    for campaign in campaigns:
        total, done = progress.get(campaign.id, (0, 0))
        rows.append(
            {
                "campaign": campaign,
                "total": total,
                "done": done,
                "percent": round(done * 100 / total) if total else 0,
                "state": campaign_state(campaign, now, total, done),
            }
        )
    return rows


def open_campaign_rows(now, limit=5):
    stmt = (
        select(ReviewCampaign)
        .where(ReviewCampaign.closed_at.is_(None))
        .order_by(ReviewCampaign.due_at.asc(), ReviewCampaign.id.asc())
    )
    return campaign_rows(db.session.scalars(stmt).all(), now)[:limit]


def scope_text(campaign):
    parts = [
        STATUS_LABELS[campaign.scope_status] if campaign.scope_status else "All except closed",
        campaign.scope_user_type.capitalize() if campaign.scope_user_type else "Internal and external",
        CRITICALITY_SCOPES.get(campaign.scope_criticality, "Any criticality"),
        campaign.scope_company or "Any company",
        campaign.scope_system or "Any system",
    ]
    return " · ".join(parts)


def pending_select(now):
    return (
        select(Access)
        .where(status_clause(STATUS_EXPIRED, now, 0), not_reviewed_clause())
        .order_by(Access.end_at.asc(), Access.id.asc())
    )


def awaiting_review(access, now):
    stmt = select(func.count(Access.id)).where(
        Access.id == access.id, status_clause(STATUS_EXPIRED, now, 0), not_reviewed_clause()
    )
    return db.session.scalar(stmt) > 0


def review_history(access_id):
    entries = []
    for review in db.session.scalars(select(AccessReview).where(AccessReview.access_id == access_id)):
        entries.append(
            {
                "at": review.reviewed_at,
                "by": review.reviewed_by,
                "source": "Review queue",
                "outcome": OUTCOME_LABELS.get(review.outcome, review.outcome),
                "justification": review.justification,
            }
        )
    stmt = (
        select(CampaignItem, ReviewCampaign)
        .join(ReviewCampaign, ReviewCampaign.id == CampaignItem.campaign_id)
        .where(CampaignItem.access_id == access_id, CampaignItem.outcome.is_not(None))
    )
    for item, campaign in db.session.execute(stmt):
        entries.append(
            {
                "at": item.reviewed_at,
                "by": item.reviewed_by,
                "source": f"Campaign: {campaign.name}",
                "outcome": OUTCOME_LABELS.get(item.outcome, item.outcome),
                "justification": item.justification,
            }
        )
    entries.sort(key=lambda entry: entry["at"], reverse=True)
    return entries


def create_campaign(values, user, now, soon_days):
    scope = {
        "status": values["status"] or None,
        "user_type": values["user_type"] or None,
        "criticality": values.get("criticality") or None,
        "company": values["company"] or None,
        "system": values["system"] or None,
    }
    accesses = db.session.scalars(scope_select(scope, now, soon_days)).all()
    if not accesses:
        raise ReviewError("No accesses match this scope, so there is nothing to review.")

    campaign = ReviewCampaign(
        name=values["name"],
        start_at=to_utc(datetime.combine(values["start_date"], time.min)),
        due_at=to_utc(datetime.combine(values["due_date"], time(23, 59))),
        scope_status=scope["status"],
        scope_user_type=scope["user_type"],
        scope_criticality=scope["criticality"],
        scope_company=scope["company"],
        scope_system=scope["system"],
        created_by=user.username,
        created_at=now,
    )
    db.session.add(campaign)
    db.session.flush()
    db.session.add_all(
        CampaignItem(
            campaign_id=campaign.id,
            access_id=a.id,
            status_snapshot=a.status_at(now, soon_days),
            end_at_snapshot=a.end_at,
        )
        for a in accesses
    )
    audit.record(
        "campaign.created",
        target=f"campaign:{campaign.id}",
        details=f"{campaign.name} | items={len(accesses)} | due {utc_text(campaign.due_at)} | {scope_text(campaign)}",
    )
    return campaign


def close_campaign(campaign, user, now):
    if campaign.closed_at is not None:
        raise ReviewError("This campaign is already closed.")
    total, done = progress_for([campaign.id]).get(campaign.id, (0, 0))
    campaign.closed_at = now
    campaign.closed_by = user.username
    audit.record(
        "campaign.closed",
        target=f"campaign:{campaign.id}",
        details=f"{campaign.name} | reviewed {done} of {total}",
    )


def check_campaign_review(campaign, item, outcome, justification, now, soon_days):
    """Validate one campaign review without changing anything. Returns (justification, status)."""
    if campaign.closed_at is not None:
        raise ReviewError("This campaign is closed.")
    if campaign.start_at > now:
        raise ReviewError("This campaign has not started yet.")
    if item.campaign_id != campaign.id:
        raise ReviewError("This item does not belong to this campaign.")
    if item.outcome is not None:
        raise ReviewError("This item was already reviewed.")
    if outcome not in OUTCOMES:
        raise ReviewError("Choose a valid outcome.")
    justification = (justification or "").strip() or None
    _require_justification(outcome, justification)

    access = item.access
    status = access.status_at(now, soon_days)
    if status == STATUS_CLOSED and outcome != OUTCOME_REMOVED:
        raise ReviewError("This access is already closed. Only 'Removal confirmed' applies.")
    if outcome == OUTCOME_RENEWED and not any(
        renewal.renewed_at >= campaign.created_at for renewal in access.renewals
    ):
        raise ReviewError("Renew this access first, then record the outcome.")
    return justification, status


def apply_campaign_review(campaign, item, outcome, justification, status, user, now):
    access = item.access
    if status == STATUS_EXPIRED and outcome == OUTCOME_KEPT:
        item.reviewed_end_at = access.end_at

    if outcome == OUTCOME_REMOVED and status != STATUS_CLOSED:
        _close(access, user, now, f"Removal confirmed in review campaign: {campaign.name}")

    item.outcome = outcome
    item.justification = justification
    item.reviewed_by = user.username
    item.reviewed_at = now
    detail = f"campaign #{campaign.id} {campaign.name} | {OUTCOME_LABELS[outcome]}"
    if justification:
        detail += f" | {justification}"
    audit.record("review.recorded", target=_target(access), details=detail)


def record_campaign_review(campaign, item, outcome, justification, user, now, soon_days):
    justification, status = check_campaign_review(
        campaign, item, outcome, justification, now, soon_days
    )
    apply_campaign_review(campaign, item, outcome, justification, status, user, now)


def plan_bulk_review(campaign, item_ids, outcome, justification, now, soon_days):
    """Check every selected item. Returns (checked, problems); nothing is changed."""
    if len(item_ids) > BULK_LIMIT:
        return [], [("The selection", f"Select at most {BULK_LIMIT} items at a time.")]
    items = {
        item.id: item
        for item in db.session.scalars(select(CampaignItem).where(CampaignItem.id.in_(item_ids)))
    }
    checked, problems = [], []
    for item_id in item_ids:
        item = items.get(item_id)
        if item is None:
            problems.append((f"Item {item_id}", "This item does not exist."))
            continue
        label = f"{item.access.name} ({item.access.system})"
        try:
            clean, status = check_campaign_review(campaign, item, outcome, justification, now, soon_days)
        except ReviewError as error:
            problems.append((label, str(error)))
        else:
            checked.append((item, clean, status))
    return checked, problems


def apply_bulk_review(campaign, checked, outcome, user, now):
    for item, justification, status in checked:
        apply_campaign_review(campaign, item, outcome, justification, status, user, now)
    audit.record(
        "review.bulk_recorded",
        target=f"campaign:{campaign.id}",
        details=f"{campaign.name} | {len(checked)} items | {OUTCOME_LABELS[outcome]}",
    )


def outcome_totals(campaign_id):
    stmt = (
        select(CampaignItem.outcome, func.count(CampaignItem.id))
        .where(CampaignItem.campaign_id == campaign_id)
        .group_by(CampaignItem.outcome)
    )
    found = dict(db.session.execute(stmt).all())
    totals = [(OUTCOME_LABELS[o], found.get(o, 0)) for o in OUTCOMES]
    totals.append(("Pending", found.get(None, 0)))
    return totals


def report_rows(campaign_id):
    stmt = (
        select(CampaignItem, Access)
        .join(Access, Access.id == CampaignItem.access_id)
        .where(CampaignItem.campaign_id == campaign_id)
        .order_by(func.lower(Access.name), CampaignItem.id)
    )
    return db.session.execute(stmt).all()


def report_csv(campaign, state_label, rows, timezone_name, delimiter):
    header = [
        "Campaign ID", "Campaign", "Scope", f"Start ({timezone_name})", f"Due ({timezone_name})",
        "State", "Created by", "Access ID", "Name", "Company", "System / target", "Access type",
        "Criticality", "Status at snapshot", f"End at snapshot ({timezone_name})", "Outcome",
        "Justification", "Reviewed by", f"Reviewed at ({timezone_name})",
    ]
    common = [
        campaign.id, campaign.name, scope_text(campaign), local_dt(campaign.start_at),
        local_dt(campaign.due_at), state_label, campaign.created_by,
    ]
    data = (
        common + [
            access.id, access.name, access.company, access.system, access.access_type,
            CRITICALITY_LABELS.get(access.criticality, access.criticality),
            STATUS_LABELS.get(item.status_snapshot, item.status_snapshot),
            local_dt(item.end_at_snapshot),
            OUTCOME_LABELS.get(item.outcome, "Pending") if item.outcome else "Pending",
            item.justification, item.reviewed_by, local_dt(item.reviewed_at),
        ]
        for item, access in rows
    )
    return build_csv(header, data, delimiter)


def record_queue_review(access, outcome, justification, user, now):
    if outcome not in QUEUE_OUTCOMES:
        raise ReviewError("Choose a valid outcome.")
    justification = (justification or "").strip() or None
    _require_justification(outcome, justification)
    if not awaiting_review(access, now):
        raise ReviewError("This access is not waiting for review.")
    db.session.add(
        AccessReview(
            access_id=access.id,
            reviewed_end_at=access.end_at,
            outcome=outcome,
            justification=justification,
            reviewed_by=user.username,
            reviewed_at=now,
        )
    )
    if outcome == OUTCOME_REMOVED:
        _close(access, user, now, "Removal confirmed in the review queue")
    detail = f"review queue | {OUTCOME_LABELS[outcome]}"
    if justification:
        detail += f" | {justification}"
    audit.record("review.queue_recorded", target=_target(access), details=detail)
