from datetime import timedelta

from accesskeeper.extensions import db
from accesskeeper.models import Access
from accesskeeper.services import csv_safe
from accesskeeper.utils import utcnow

from .conftest import add_access

FORM = {
    "name": "Ana Souza", "email": "ana@example.com", "company": "Contoso", "user_type": "external",
    "access_type": "Global Reader", "system": "Firewall", "description": "d", "justification": "j",
    "authorized_by": "Boss", "start_at": "2026-01-01T10:00", "end_at": "2026-01-08T10:00",
}


def status_of(app, access_id):
    with app.app_context():
        return db.session.get(Access, access_id).status_at(utcnow(), 14)


def test_status_logic(app, users):
    assert status_of(app, add_access(app, "A", timedelta(days=30))) == "active"
    assert status_of(app, add_access(app, "B", timedelta(days=3))) == "expiring"
    assert status_of(app, add_access(app, "C", -timedelta(days=1))) == "expired"
    assert status_of(app, add_access(app, "D", timedelta(days=3), closed_at=utcnow(), closed_by="boss", close_reason="x")) == "closed"
    future = add_access(app, "E", timedelta(days=60), start_at=utcnow() + timedelta(days=30))
    assert status_of(app, future) == "scheduled"
    soon = add_access(app, "F", timedelta(days=5), start_at=utcnow() + timedelta(days=1))
    assert status_of(app, soon) == "scheduled"


def test_scheduled_filter_and_dashboard(app, users):
    add_access(app, "Later", timedelta(days=60), start_at=utcnow() + timedelta(days=30))
    add_access(app, "Now", timedelta(days=60))
    c = users["boss"]
    page = c.get("/access/?status=scheduled").get_data(as_text=True)
    assert "Later" in page and "Now" not in page
    active = c.get("/access/?status=active").get_data(as_text=True)
    assert "Now" in active and "Later" not in active
    home = c.get("/").get_data(as_text=True)
    assert "Scheduled" in home


def test_editing_start_into_the_future_makes_it_scheduled(app, users):
    access_id = add_access(app, "Mover", timedelta(days=200))
    c = users["boss"]
    data = dict(FORM, start_at=(utcnow() + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M"))
    assert c.post(f"/access/{access_id}/edit", data=data).status_code == 302
    assert status_of(app, access_id) == "scheduled"


def test_viewer_cannot_write(app, users):
    access_id = add_access(app)
    c = users["reader"]
    assert c.post("/access/new", data=FORM).status_code == 403
    for suffix in ("edit", "renew", "close"):
        assert c.post(f"/access/{access_id}/{suffix}", data={}).status_code == 403
    assert c.get("/access/").status_code == 200
    assert c.get("/access/export.csv").status_code == 200


def test_create_renew_close(app, users):
    c = users["boss"]
    assert c.post("/access/new", data=FORM).status_code == 302
    with app.app_context():
        access = Access.query.one()
        access_id = access.id
    future = (utcnow() + timedelta(days=400)).strftime("%Y-%m-%dT%H:%M")
    response = c.post(f"/access/{access_id}/renew", data={"new_end_at": future, "reason": "project extended"})
    assert response.status_code in (302, 200)
    page = c.get(f"/access/{access_id}").get_data(as_text=True)
    assert "project extended" in page
    assert c.post(f"/access/{access_id}/close", data={"reason": "removed early"}).status_code == 302
    assert status_of(app, access_id) == "closed"
    assert c.post(f"/access/{access_id}/edit", data=FORM).status_code == 302


def test_csv_formula_injection_is_neutralised():
    assert csv_safe("=1+1").startswith("'")
    assert csv_safe("  @cmd").startswith("'")
    assert csv_safe("normal") == "normal"


def test_archive_holds_only_closed(app, users):
    add_access(app, "Runner", timedelta(days=30))
    add_access(app, "Closer", timedelta(days=30), closed_at=utcnow(), closed_by="boss", close_reason="done")
    expired = add_access(app, "Expirer", -timedelta(days=2))
    c = users["boss"]
    main = c.get("/access/").get_data(as_text=True)
    assert "Runner" in main and "Closer" not in main and "Expirer" in main
    archive = c.get("/access/archive").get_data(as_text=True)
    assert "Closer" in archive and "Runner" not in archive and "Expirer" not in archive
    assert "Closer" in c.get("/access/?status=closed").get_data(as_text=True)
    c.post(f"/review/queue/{expired}", data={"outcome": "kept", "justification": "vendor exception"})
    assert "Expirer" in c.get("/access/").get_data(as_text=True)
    assert "Expirer" not in c.get("/access/archive").get_data(as_text=True)
    assert "Expirer" not in c.get("/review/").get_data(as_text=True)


def test_removal_confirmed_makes_record_terminal(app, users):
    expired = add_access(app, "Gone", -timedelta(days=2))
    c = users["boss"]
    assert c.post(f"/review/queue/{expired}", data={"outcome": "removal_confirmed"}).status_code == 302
    assert status_of(app, expired) == "closed"
    assert "Gone" in c.get("/access/archive").get_data(as_text=True)
    assert "Gone" not in c.get("/access/").get_data(as_text=True)
    future = (utcnow() + timedelta(days=30)).strftime("%Y-%m-%dT%H:%M")
    c.post(f"/access/{expired}/renew", data={"new_end_at": future, "reason": "again"})
    assert status_of(app, expired) == "closed"
    assert c.post(f"/access/{expired}/edit", data=FORM).status_code == 302
    page = c.get(f"/access/{expired}").get_data(as_text=True)
    assert "after the end date" in page and "Register again" in page
    assert "Renew</a>" not in page


def test_closed_record_cannot_enter_campaign_scope(app, users):
    expired = add_access(app, "Gone", -timedelta(days=2))
    add_access(app, "Live", timedelta(days=30))
    c = users["boss"]
    c.post(f"/review/queue/{expired}", data={"outcome": "removal_confirmed"})
    today = (utcnow() - timedelta(hours=3)).date().isoformat()
    c.post("/review/campaigns/new", data={"name": "X", "start_date": today, "due_date": today,
                                          "status": "", "user_type": "", "company": "", "system": ""})
    page = c.get("/review/campaigns/1").get_data(as_text=True)
    assert "Live" in page and "Gone" not in page


def test_register_again_prefills_without_dates_ticket_notes(app, users):
    closed = add_access(
        app, "Copy Me", timedelta(days=30), closed_at=utcnow(), closed_by="boss", close_reason="done",
        ticket="INC-1", notes="old notes", system="Firewall",
    )
    c = users["boss"]
    page = c.get(f"/access/new?source={closed}").get_data(as_text=True)
    assert 'value="Copy Me"' in page and "INC-1" not in page and "old notes" not in page
    assert '<option selected value="Firewall">' in page
    response = c.post(f"/access/new?source={closed}", data=FORM)
    assert response.status_code == 302
    with app.app_context():
        assert Access.query.count() == 2
    assert "registered again from access:" in c.get("/audit?action=access.created").get_data(as_text=True)
    running = add_access(app, "Running")
    assert "Prefilled" not in c.get(f"/access/new?source={running}").get_data(as_text=True)
