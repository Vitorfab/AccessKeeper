from accesskeeper.extensions import db
from accesskeeper.models import Access, SystemEntry

from .conftest import add_access
from .test_access import FORM


def test_catalog_is_seeded_with_generic_defaults(app):
    with app.app_context():
        names = {e.name for e in SystemEntry.query.all()}
    assert {"Firewall", "Microsoft 365", "Cloud provider", "Web application firewall"} <= names


def test_form_is_a_select_and_rejects_values_outside_catalog(app, users):
    c = users["boss"]
    page = c.get("/access/new").get_data(as_text=True)
    assert '<select id="system"' in page and "<option value=\"Firewall\">" in page
    response = c.post("/access/new", data=dict(FORM, system="Made up system"))
    assert response.status_code == 200
    with app.app_context():
        assert Access.query.count() == 0
    assert c.post("/access/new", data=dict(FORM, system="")).status_code == 200


def test_only_admins_manage_the_catalog(app, users):
    assert users["reader"].get("/systems/").status_code == 403
    assert users["reader"].post("/systems/", data={"name": "Nope"}).status_code == 403
    assert users["second"].post("/systems/", data={"name": "Remote desktop gateway"}).status_code == 302
    with app.app_context():
        assert SystemEntry.query.filter_by(name="Remote desktop gateway").one().created_by == "second"


def test_duplicate_names_are_rejected_ignoring_case(app, users):
    response = users["boss"].post("/systems/", data={"name": "firewall"})
    assert response.status_code == 200
    assert "already in the catalog" in response.get_data(as_text=True)


def test_disabled_system_leaves_the_form_but_old_records_still_edit(app, users):
    access_id = add_access(app, "Old", system="Firewall")
    with app.app_context():
        entry = SystemEntry.query.filter_by(name="Firewall").one()
        entry_id = entry.id
    c = users["boss"]
    assert c.post(f"/systems/{entry_id}/toggle").status_code == 302
    assert '<option value="Firewall">' not in c.get("/access/new").get_data(as_text=True)
    edit = c.get(f"/access/{access_id}/edit").get_data(as_text=True)
    assert "Firewall (not in the catalog)" in edit
    assert c.post(f"/access/{access_id}/edit", data=dict(FORM, system="Firewall", name="Renamed")).status_code == 302
    with app.app_context():
        assert db.session.get(Access, access_id).name == "Renamed"
    c.post(f"/systems/{entry_id}/toggle")
    assert '<option value="Firewall">' in c.get("/access/new").get_data(as_text=True)


def test_systems_are_never_deleted(app):
    import pytest
    from sqlalchemy.exc import DBAPIError

    with app.app_context():
        with pytest.raises((DBAPIError, RuntimeError)):
            db.session.execute(db.text("DELETE FROM systems"))
            db.session.commit()
        db.session.rollback()
