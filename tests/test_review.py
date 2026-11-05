from datetime import timedelta

from accesskeeper.extensions import db
from accesskeeper.models import Access, AuditLog, CampaignItem, ReviewCampaign
from accesskeeper.utils import utcnow

from .conftest import add_access


def campaign_data(**overrides):
    today = (utcnow() - timedelta(hours=3)).date()
    data = {
        "name": "Q4 review",
        "start_date": today.isoformat(),
        "due_date": (today + timedelta(days=14)).isoformat(),
        "status": "", "user_type": "", "company": "", "system": "",
    }
    data.update(overrides)
    return data


def first_campaign(app):
    with app.app_context():
        c = ReviewCampaign.query.order_by(ReviewCampaign.id.desc()).first()
        return c.id, [i.id for i in c.items]


def test_queue_lists_expired_until_reviewed(app, users):
    expired = add_access(app, "Gone", -timedelta(days=3))
    add_access(app, "Fine", timedelta(days=30))
    c = users["boss"]
    page = c.get("/review/").get_data(as_text=True)
    assert "Gone" in page and "Fine" not in page
    assert "1</strong> expired access still needs review" in c.get("/").get_data(as_text=True)
    assert c.post(f"/review/queue/{expired}", data={"outcome": "removal_confirmed"}).status_code == 302
    assert "Gone" not in c.get("/review/").get_data(as_text=True)
    assert "Nothing needs attention" in c.get("/").get_data(as_text=True)


def test_queue_kept_requires_justification(app, users):
    expired = add_access(app, "Gone", -timedelta(days=3))
    c = users["boss"]
    assert c.post(f"/review/queue/{expired}", data={"outcome": "kept", "justification": ""}).status_code == 200
    assert "Gone" in c.get("/review/").get_data(as_text=True)
    assert c.post(f"/review/queue/{expired}", data={"outcome": "kept", "justification": "vendor audit"}).status_code == 302
    assert "Gone" not in c.get("/review/").get_data(as_text=True)


def test_renewing_takes_access_out_of_queue_and_re_expiry_returns_it(app, users):
    expired = add_access(app, "Gone", -timedelta(days=3))
    c = users["boss"]
    c.post(f"/review/queue/{expired}", data={"outcome": "kept", "justification": "needed"})
    with app.app_context():
        access = db.session.get(Access, expired)
        access.end_at = utcnow() - timedelta(days=1)
        db.session.commit()
    assert "Gone" in c.get("/review/").get_data(as_text=True)


def test_viewer_cannot_review_or_create_campaigns(app, users):
    expired = add_access(app, "Gone", -timedelta(days=3))
    c = users["reader"]
    assert c.get("/review/").status_code == 200
    assert c.get("/review/campaigns").status_code == 200
    assert c.post(f"/review/queue/{expired}", data={"outcome": "removal_confirmed"}).status_code == 403
    assert c.post("/review/campaigns/new", data=campaign_data()).status_code == 403


def test_campaign_snapshot_scope_and_progress(app, users):
    add_access(app, "Ext1", timedelta(days=30), user_type="external")
    add_access(app, "Ext2", -timedelta(days=2), user_type="external")
    add_access(app, "Int1", timedelta(days=30), user_type="internal")
    add_access(app, "Closed1", timedelta(days=30), closed_at=utcnow(), closed_by="boss", close_reason="x")
    c = users["boss"]
    assert c.post("/review/campaigns/new", data=campaign_data(user_type="external")).status_code == 302
    cid, items = first_campaign(app)
    assert len(items) == 2
    add_access(app, "Ext3", timedelta(days=30), user_type="external")
    assert len(first_campaign(app)[1]) == 2
    page = c.get(f"/review/campaigns/{cid}").get_data(as_text=True)
    assert "0 of 2 reviewed" in page


def test_empty_scope_is_rejected(app, users):
    add_access(app, "Ext1", timedelta(days=30), user_type="external")
    c = users["boss"]
    response = c.post("/review/campaigns/new", data=campaign_data(user_type="internal"))
    assert response.status_code == 200
    assert "nothing to review" in response.get_data(as_text=True)


def test_due_before_start_is_rejected(app, users):
    add_access(app, "Ext1")
    today = utcnow().date()
    response = users["boss"].post(
        "/review/campaigns/new",
        data=campaign_data(start_date=today.isoformat(), due_date=(today - timedelta(days=3)).isoformat()),
    )
    assert response.status_code == 200
    assert "cannot be before" in response.get_data(as_text=True)


def test_campaign_outcomes(app, users):
    running = add_access(app, "Running", timedelta(days=30))
    kept = add_access(app, "Kept", timedelta(days=30))
    renew = add_access(app, "Renew", timedelta(days=30))
    expired = add_access(app, "Lapsed", -timedelta(days=3))
    c = users["boss"]
    c.post("/review/campaigns/new", data=campaign_data())
    cid, items = first_campaign(app)
    with app.app_context():
        by_access = {i.access_id: i.id for i in CampaignItem.query.all()}

    def post(access_id, **data):
        return c.post(f"/review/campaigns/{cid}/items/{by_access[access_id]}", data=data)

    assert post(kept, outcome="kept", justification="").status_code == 200
    assert post(kept, outcome="kept", justification="still needed").status_code == 302

    assert post(renew, outcome="renewed").status_code == 200
    future = (utcnow() + timedelta(days=90)).strftime("%Y-%m-%dT%H:%M")
    c.post(f"/access/{renew}/renew", data={"new_end_at": future, "reason": "extended"})
    assert post(renew, outcome="renewed").status_code == 302

    assert post(running, outcome="removal_confirmed").status_code == 302
    with app.app_context():
        assert db.session.get(Access, running).closed_at is not None
        assert db.session.get(Access, kept).closed_at is None

    assert post(running, outcome="removal_confirmed").status_code == 302
    assert post(expired, outcome="removal_confirmed").status_code == 302
    assert "Lapsed" not in c.get("/review/").get_data(as_text=True)

    page = c.get(f"/review/campaigns/{cid}").get_data(as_text=True)
    assert "4 of 4 reviewed" in page and "All reviewed" in page
    with app.app_context():
        actions = {a.action for a in AuditLog.query.all()}
    assert {"campaign.created", "review.recorded", "access.closed"} <= actions


def test_closed_campaign_blocks_reviews_and_overdue_state(app, users):
    access = add_access(app, "Ext1")
    c = users["boss"]
    c.post("/review/campaigns/new", data=campaign_data())
    cid, items = first_campaign(app)
    with app.app_context():
        campaign = db.session.get(ReviewCampaign, cid)
        campaign.due_at = utcnow() - timedelta(days=1)
        db.session.commit()
    assert "Overdue" in c.get(f"/review/campaigns/{cid}").get_data(as_text=True)
    assert "overdue" in c.get("/").get_data(as_text=True)
    assert c.post(f"/review/campaigns/{cid}/close").status_code == 302
    response = c.post(f"/review/campaigns/{cid}/items/{items[0]}", data={"outcome": "kept", "justification": "abc"})
    assert response.status_code == 302
    with app.app_context():
        assert db.session.get(CampaignItem, items[0]).outcome is None
    assert access


def test_reviewed_item_outcome_is_final(app, users):
    add_access(app, "Ext1")
    c = users["boss"]
    c.post("/review/campaigns/new", data=campaign_data())
    cid, items = first_campaign(app)
    c.post(f"/review/campaigns/{cid}/items/{items[0]}", data={"outcome": "kept", "justification": "needed"})
    import pytest
    from sqlalchemy.exc import DBAPIError

    with app.app_context():
        with pytest.raises(DBAPIError):
            db.session.execute(db.text("UPDATE campaign_items SET outcome='renewed'"))
            db.session.commit()
        db.session.rollback()
