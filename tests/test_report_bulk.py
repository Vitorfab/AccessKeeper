from datetime import timedelta

from accesskeeper.extensions import db
from accesskeeper.models import Access, AuditLog, CampaignItem
from accesskeeper.utils import utcnow

from .conftest import add_access


def campaign_data(**overrides):
    today = (utcnow() - timedelta(hours=3)).date()
    data = {
        "name": "Q4 review",
        "start_date": today.isoformat(),
        "due_date": (today + timedelta(days=14)).isoformat(),
        "status": "", "user_type": "", "criticality": "", "company": "", "system": "",
    }
    data.update(overrides)
    return data


def make_campaign(app, client, names, **overrides):
    ids = [add_access(app, name, **overrides) for name in names]
    assert client.post("/review/campaigns/new", data=campaign_data()).status_code == 302
    with app.app_context():
        items = {i.access_id: i.id for i in CampaignItem.query.all()}
    return ids, [items[a] for a in ids]


def bulk(client, item_ids, **data):
    return client.post("/review/campaigns/1/bulk", data=dict(data, item_ids=item_ids))


def outcomes(app):
    with app.app_context():
        return {i.id: i.outcome for i in CampaignItem.query.all()}


def actions(app):
    with app.app_context():
        return [a.action for a in db.session.scalars(db.select(AuditLog).order_by(AuditLog.id))]


def test_report_page_for_admin_and_viewer(app, users):
    make_campaign(app, users["boss"], ["Ana", "Bia"])
    for name in ("boss", "reader"):
        response = users[name].get("/review/campaigns/1/report")
        assert response.status_code == 200
        page = response.get_data(as_text=True)
        assert "Q4 review" in page and "Totals" in page and "2 pending" in page
        assert "Ana" in page and "Bia" in page
        assert "js/print.js" in page and " style=" not in page and "<script>" not in page
    assert "Report</a>" in users["reader"].get("/review/campaigns/1").get_data(as_text=True)


def test_report_totals_follow_the_outcomes(app, users):
    ids, items = make_campaign(app, users["boss"], ["Ana", "Bia", "Cal"])
    assert bulk(users["boss"], items[:1], outcome="kept", justification="needed").status_code == 302
    page = users["reader"].get("/review/campaigns/1/report").get_data(as_text=True)
    assert "3 items:" in page and "1 kept with justification" in page and "2 pending" in page


def test_report_csv_is_audited_and_safe(app, users):
    add_access(app, "=HYPERLINK")
    make_campaign(app, users["boss"], ["Ana"])
    response = users["reader"].get("/review/campaigns/1/report.csv")
    assert response.status_code == 200 and "text/csv" in response.content_type
    body = response.get_data(as_text=True)
    assert "Campaign ID" in body.splitlines()[0] and "Pending" in body
    assert "'=HYPERLINK" in body and "Q4 review" in body
    assert "export.campaign_csv" in actions(app)


def test_viewer_cannot_bulk_review_or_see_checkboxes(app, users):
    ids, items = make_campaign(app, users["boss"], ["Ana"])
    assert bulk(users["reader"], items, outcome="kept", justification="x").status_code == 403
    assert 'name="item_ids"' not in users["reader"].get("/review/campaigns/1").get_data(as_text=True)
    admin_page = users["boss"].get("/review/campaigns/1").get_data(as_text=True)
    assert 'name="item_ids"' in admin_page and "data-select-all" in admin_page and "js/bulk.js" in admin_page


def test_bulk_kept_needs_justification_and_changes_nothing_without_it(app, users):
    ids, items = make_campaign(app, users["boss"], ["Ana", "Bia"])
    response = bulk(users["boss"], items, outcome="kept", justification="")
    page = response.get_data(as_text=True)
    assert response.status_code == 200 and "Nothing was changed" in page
    assert "A justification is required" in page
    assert set(outcomes(app).values()) == {None}


def test_bulk_kept_records_every_item_and_one_summary(app, users):
    ids, items = make_campaign(app, users["boss"], ["Ana", "Bia"])
    assert bulk(users["boss"], items, outcome="kept", justification="still needed").status_code == 302
    assert set(outcomes(app).values()) == {"kept"}
    log = actions(app)
    assert log.count("review.recorded") == 2 and log.count("review.bulk_recorded") == 1
    with app.app_context():
        assert all(a.closed_at is None for a in Access.query.all())


def test_bulk_is_all_or_nothing(app, users):
    ids, items = make_campaign(app, users["boss"], ["Ana", "Bia"])
    future = (utcnow() + timedelta(days=90)).strftime("%Y-%m-%dT%H:%M")
    users["boss"].post(f"/access/{ids[0]}/renew", data={"new_end_at": future, "reason": "extended"})
    response = bulk(users["boss"], items, outcome="renewed")
    page = response.get_data(as_text=True)
    assert response.status_code == 200 and "Renew this access first" in page and "Bia" in page
    assert set(outcomes(app).values()) == {None}
    assert bulk(users["boss"], items[:1], outcome="renewed").status_code == 302
    assert outcomes(app)[items[0]] == "renewed" and outcomes(app)[items[1]] is None


def test_bulk_removal_asks_for_confirmation_then_closes(app, users):
    ids, items = make_campaign(app, users["boss"], ["Ana", "Bia"])
    first = bulk(users["boss"], items, outcome="removal_confirmed")
    page = first.get_data(as_text=True)
    assert first.status_code == 200 and "cannot be undone" in page and "Confirm removal of 2" in page
    with app.app_context():
        assert all(a.closed_at is None for a in Access.query.all())
    assert set(outcomes(app).values()) == {None}
    done = bulk(users["boss"], items, outcome="removal_confirmed", confirm="1")
    assert done.status_code == 302
    with app.app_context():
        assert all(a.closed_at is not None for a in Access.query.all())
    assert set(outcomes(app).values()) == {"removal_confirmed"}


def test_bulk_rejects_empty_unknown_and_closed(app, users):
    ids, items = make_campaign(app, users["boss"], ["Ana"])
    page = users["boss"].post(
        "/review/campaigns/1/bulk", data={"outcome": "kept", "justification": "x"}, follow_redirects=True
    ).get_data(as_text=True)
    assert "Select at least one item" in page
    page = bulk(users["boss"], [99999], outcome="kept", justification="x").get_data(as_text=True)
    assert "does not exist" in page
    users["boss"].post("/review/campaigns/1/close")
    page = bulk(users["boss"], items, outcome="kept", justification="x").get_data(as_text=True)
    assert "This campaign is closed" in page
    assert set(outcomes(app).values()) == {None}


def test_bulk_and_report_pages_have_no_inline_code(app, users):
    ids, items = make_campaign(app, users["boss"], ["Ana"])
    pages = [
        bulk(users["boss"], items, outcome="removal_confirmed").get_data(as_text=True),
        bulk(users["boss"], items, outcome="kept", justification="").get_data(as_text=True),
        users["boss"].get("/review/campaigns/1").get_data(as_text=True),
        users["boss"].get("/audit/integrity").get_data(as_text=True),
    ]
    for html in pages:
        assert " style=" not in html and "<style" not in html and "onclick" not in html
        assert "<script>" not in html
