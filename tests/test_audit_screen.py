from datetime import timedelta

from accesskeeper.utils import utcnow

from .conftest import add_access


def test_audit_filters_and_export(app, users):
    add_access(app, "Visible")
    c = users["boss"]
    c.get("/access/export.csv")
    page = c.get("/audit?action=export.access_csv").get_data(as_text=True)
    assert "<code>export.access_csv</code>" in page and "<code>auth.login_success</code>" not in page
    assert "<code>auth.login_success</code>" in c.get("/audit?actor=boss").get_data(as_text=True)
    today = (utcnow() - timedelta(hours=3)).date().isoformat()
    assert "<code>auth.login_success</code>" in c.get(f"/audit?date_from={today}&date_to={today}").get_data(as_text=True)
    assert "No events match" in c.get("/audit?date_to=2000-01-01").get_data(as_text=True)
    assert "No events match" in c.get("/audit?q=zzz-not-there").get_data(as_text=True)
    csv = c.get("/audit/export.csv?action=auth.login_success")
    assert csv.status_code == 200 and "text/csv" in csv.content_type
    body = csv.get_data(as_text=True)
    assert "Time (UTC)" in body and "auth.login_success" in body
    assert "export.audit_csv" in c.get("/audit").get_data(as_text=True)


def test_invalid_audit_params_do_not_crash(users):
    c = users["boss"]
    assert c.get("/audit?page=abc&date_from=nonsense&date_to=99").status_code == 200
