import pytest

from .conftest import login


def replayed(app, client, action):
    cookie = client.get_cookie("ak_session").value
    assert client.get("/access/").status_code == 200
    action(client)
    other = app.test_client()
    other.set_cookie("ak_session", cookie)
    return other.get("/access/").status_code


def test_a_cookie_copied_before_logout_stops_working(app, users):
    status = replayed(app, users["boss"], lambda c: c.post("/logout"))
    assert status == 302


def test_a_cookie_copied_before_idle_sign_out_stops_working(app, users):
    status = replayed(app, users["reader"], lambda c: c.post("/session/expire"))
    assert status == 302


def test_signing_in_again_after_logout_works(app, users):
    users["boss"].post("/logout")
    again = login(app, "boss")
    assert again.get("/access/").status_code == 200


@pytest.mark.parametrize(
    "path",
    [
        "/access/99999999999999999999",
        "/access/99999999999999999999/edit",
        "/review/campaigns/99999999999999999999",
        "/review/queue/99999999999999999999",
        "/users/99999999999999999999/reset-password",
        "/access/2147483648",
    ],
)
def test_huge_ids_are_a_404_not_a_server_error(users, path):
    assert users["boss"].get(path).status_code == 404
