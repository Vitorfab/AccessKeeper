from datetime import timedelta

from accesskeeper import services
from accesskeeper.extensions import db
from accesskeeper.models import Access, AuditLog
from accesskeeper.utils import utcnow

from .conftest import add_access
from .test_access import FORM


def campaign_data(**overrides):
    today = (utcnow() - timedelta(hours=3)).date()
    data = {
        "name": "Crit review",
        "start_date": today.isoformat(),
        "due_date": (today + timedelta(days=14)).isoformat(),
        "status": "", "user_type": "", "criticality": "", "company": "", "system": "",
    }
    data.update(overrides)
    return data


def test_new_access_defaults_to_standard(app, users):
    assert users["boss"].post("/access/new", data=FORM).status_code == 302
    with app.app_context():
        assert Access.query.one().criticality == "standard"


def test_creator_chooses_the_level(app, users):
    assert users["boss"].post("/access/new", data=dict(FORM, criticality="critical")).status_code == 302
    with app.app_context():
        assert Access.query.one().criticality == "critical"
    page = users["boss"].get("/access/1").get_data(as_text=True)
    assert "Critical" in page


def test_unknown_level_is_rejected(app, users):
    response = users["boss"].post("/access/new", data=dict(FORM, criticality="super"))
    assert response.status_code == 200
    with app.app_context():
        assert Access.query.count() == 0


def test_long_duration_warnings_depend_on_the_level_and_never_block(app, users):
    c = users["boss"]
    long_end = {"end_at": "2026-04-20T10:00"}  # 109 days after the start
    page = c.post("/access/new", data=dict(FORM, criticality="critical", **long_end), follow_redirects=True)
    assert "Critical access lasts more than 30 days" in page.get_data(as_text=True)
    page = c.post("/access/new", data=dict(FORM, criticality="high", **long_end), follow_redirects=True)
    assert "High access lasts more than 90 days" in page.get_data(as_text=True)
    page = c.post("/access/new", data=dict(FORM, criticality="standard", **long_end), follow_redirects=True)
    assert "lasts more than" not in page.get_data(as_text=True)
    with app.app_context():
        assert Access.query.count() == 3


def test_duration_warning_limits_come_from_the_settings(app, users):
    app.config["CRITICAL_MAX_DAYS"] = 200
    page = users["boss"].post(
        "/access/new", data=dict(FORM, criticality="critical", end_at="2026-04-20T10:00"), follow_redirects=True
    )
    assert "lasts more than" not in page.get_data(as_text=True)


def test_editing_the_level_is_audited_and_warns(app, users):
    access_id = add_access(app, "Mover", timedelta(days=200))
    c = users["boss"]
    data = dict(FORM, criticality="critical", start_at=(utcnow() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M"))
    page = c.post(f"/access/{access_id}/edit", data=data, follow_redirects=True).get_data(as_text=True)
    assert "Critical access lasts more than 30 days" in page
    with app.app_context():
        assert db.session.get(Access, access_id).criticality == "critical"
        details = db.session.scalars(db.select(AuditLog).where(AuditLog.action == "access.edited")).one().details
    assert "criticality" in details


def test_filter_sort_and_badge_in_the_list(app, users):
    add_access(app, "Plainy")
    add_access(app, "Highy", criticality="high")
    add_access(app, "Critty", criticality="critical")
    c = users["boss"]
    only = c.get("/access/?criticality=critical").get_data(as_text=True)
    assert "Critty" in only and "Highy" not in only and "Plainy" not in only
    page = c.get("/access/?sort=criticality&dir=desc").get_data(as_text=True)
    assert page.index("Critty") < page.index("Highy") < page.index("Plainy")
    assert 'class="badge bad">Critical' in page
    assert c.get("/access/?criticality=bogus").status_code == 200


def test_csv_has_the_criticality_column(app, users):
    add_access(app, "Critty", criticality="critical")
    body = users["boss"].get("/access/export.csv").get_data(as_text=True)
    assert "Criticality" in body.splitlines()[0] and "Critical" in body.splitlines()[1]


def test_register_again_copies_the_level(app, users):
    closed = add_access(
        app, "Copy Me", closed_at=utcnow(), closed_by="boss", close_reason="done", criticality="high"
    )
    page = users["boss"].get(f"/access/new?source={closed}").get_data(as_text=True)
    assert '<option selected value="high">' in page


def test_campaign_scope_by_level(app, users):
    add_access(app, "Plainy")
    add_access(app, "Highy", criticality="high")
    add_access(app, "Critty", criticality="critical")
    c = users["boss"]
    assert c.post("/review/campaigns/new", data=campaign_data(criticality="critical")).status_code == 302
    only = c.get("/review/campaigns/1").get_data(as_text=True)
    assert "Critty" in only and "Highy" not in only and "Critical only" in only
    assert c.post("/review/campaigns/new", data=campaign_data(criticality="high")).status_code == 302
    above = c.get("/review/campaigns/2").get_data(as_text=True)
    assert "Critty" in above and "Highy" in above and "Plainy" not in above
    assert "High and above" in above


def test_dashboard_counts_only_running_high_and_critical(app, users):
    add_access(app, "A", criticality="critical")
    add_access(app, "B", timedelta(days=3), criticality="critical")
    add_access(app, "C", -timedelta(days=1), criticality="critical")
    add_access(app, "D", criticality="high")
    add_access(app, "E")
    with app.app_context():
        assert services.criticality_counts(utcnow(), 14) == {"high": 1, "critical": 2}
    home = users["boss"].get("/").get_data(as_text=True)
    assert "Critical running" in home and "High running" in home
