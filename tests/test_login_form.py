"""The login form, as a browser sends it, and the limit on failed attempts."""

import pytest
from httpx import ASGITransport, AsyncClient
from starlette.middleware.sessions import SessionMiddleware

from Backend.fastapi.main import app
from Backend.fastapi.routes import template_routes
from Backend.helper.settings_manager import Settings


@pytest.fixture
def client():
    template_routes._FAILED_LOGINS.clear()
    wrapped = SessionMiddleware(app, secret_key="k" * 32)
    yield AsyncClient(transport=ASGITransport(app=wrapped), base_url="http://testserver")
    template_routes._FAILED_LOGINS.clear()


async def test_wrong_password_is_refused_for_both_form_encodings(client):
    async with client:
        urlencoded = await client.post("/login", data={"username": "admin", "password": "wrong"})
        multipart = await client.post("/login", files={"username": (None, "admin"), "password": (None, "wrong")})
    for response in (urlencoded, multipart):
        assert response.status_code < 500
        assert "session" not in response.cookies or response.status_code not in (302, 303)
        assert "<form" in response.text


async def test_right_password_signs_in(client):
    async with client:
        response = await client.post("/login", data={"username": "admin", "password": "admin"})
    assert response.status_code in (302, 303)
    assert response.headers["location"]


async def test_missing_field_is_a_client_error(client):
    async with client:
        response = await client.post("/login", data={"username": "admin"})
    assert response.status_code == 422


async def test_sixth_failed_attempt_gets_the_wait_page(client):
    async with client:
        for _ in range(template_routes._LOGIN_MAX_FAILS):
            await client.post("/login", data={"username": "admin", "password": "wrong"})
        blocked = await client.post("/login", data={"username": "admin", "password": "admin"})
    assert blocked.status_code == 429
    assert "Too many failed attempts" in blocked.text


def test_self_update_is_off_until_a_source_is_entered():
    fresh = Settings({})
    assert fresh.upstream_repo == ""
    assert fresh.upstream_branch == ""
