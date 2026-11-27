"""Routes that hand out library data must ask who is calling."""

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute
from httpx import ASGITransport, AsyncClient

from Backend.fastapi import main
from Backend.fastapi.main import app, require_playlist_access
from Backend.fastapi.security.credentials import require_auth


class FakeRequest:
    def __init__(self, session):
        self.session = session


def dependency_names(route):
    names = set()

    def walk(dependant):
        for dep in dependant.dependencies:
            names.add(getattr(dep.call, "__name__", ""))
            walk(dep)

    walk(route.dependant)
    return names


def all_routes(routes=None):
    """Every API route of the app, including those that came in through a router."""
    for route in app.routes if routes is None else routes:
        if isinstance(route, APIRoute):
            yield route
        elif getattr(route, "original_router", None) is not None:
            yield from all_routes(route.original_router.routes)


def routes_for(path):
    found = [r for r in all_routes() if r.path == path]
    assert found, path
    return found


async def test_playlist_is_refused_without_session_or_token():
    with pytest.raises(HTTPException) as error:
        await require_playlist_access(FakeRequest({}), None)
    assert error.value.status_code == 401


async def test_playlist_is_given_to_a_signed_in_admin():
    assert await require_playlist_access(FakeRequest({"authenticated": True, "sid": "abc"}), None) is True


async def test_playlist_checks_the_token_it_is_given(monkeypatch):
    seen = []

    async def fake_verify(token):
        seen.append(token)
        if token != "good":
            raise HTTPException(status_code=401, detail="Invalid or expired API token")
        return {"token": token}

    monkeypatch.setattr(main, "verify_token", fake_verify)

    assert await require_playlist_access(FakeRequest({}), "good") is True
    with pytest.raises(HTTPException) as error:
        await require_playlist_access(FakeRequest({}), "guess")
    assert error.value.status_code == 401
    assert seen == ["good", "guess"]


@pytest.mark.parametrize("path", [
    "/api/media/playlist/export.m3u8",
    "/api/media/playlist/m3u",
    "/media/playlist/m3u",
])
def test_playlist_routes_use_the_access_check(path):
    for route in routes_for(path):
        assert "require_playlist_access" in dependency_names(route)


@pytest.mark.parametrize("path", ["/stream/stats", "/stream/stats/{stream_id}"])
def test_stream_statistics_need_a_login(path):
    for route in routes_for(path):
        assert require_auth.__name__ in dependency_names(route)


async def test_open_in_app_accepts_catalog_ids():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/open/stremio/series/tt1234567")
    assert response.status_code == 200
    assert "stremio:///detail/series/tt1234567" in response.text


@pytest.mark.parametrize("content_id", [
    'x";alert(1)',
    "<script>alert(1)</script>",
    "a b",
])
async def test_open_in_app_rejects_anything_that_is_not_an_id(content_id):
    from urllib.parse import quote

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(f"/open/stremio/series/{quote(content_id, safe='')}")
    assert response.status_code == 404
    assert "alert(1)" not in response.text


# Addresses that may answer a caller who has neither a session nor a valid token.
PUBLIC_GET = {
    "/login",                                        # the sign-in form
    "/status",                                       # {"status": "ok", "version": ...}
    "/manifest.webmanifest", "/pwa-icon.svg", "/sw.js",
    "/open/{app_name}/{media_type}/{content_id}",    # hand-off page, no library data
    "/request",                                      # public request form
    "/api/request/search", "/api/request/popular",   # its two lookups
    "/stremio/{token}/configure",                    # shows "Unknown" for a token it does not know
}


def _fill(path):
    import re

    return re.sub(r"\{[^}]+\}", "nobody", path)


async def test_nothing_else_answers_an_anonymous_caller():
    """A new route must either ask who is calling or be added to PUBLIC_GET on purpose."""
    from starlette.middleware.sessions import SessionMiddleware

    paths = sorted({r.path for r in all_routes() if "GET" in r.methods} - PUBLIC_GET)
    assert len(paths) > 60
    wrapped = SessionMiddleware(app, secret_key="k" * 32)
    transport = ASGITransport(app=wrapped, raise_app_exceptions=False)
    answered = []
    async with AsyncClient(transport=transport, base_url="http://test", timeout=20) as client:
        for path in paths:
            response = await client.get(_fill(path))
            if response.status_code < 300:
                answered.append((path, response.status_code))
    assert answered == []


@pytest.mark.parametrize("path", ["/docs", "/redoc", "/openapi.json"])
async def test_the_api_description_is_not_published(path):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(path)
    assert response.status_code == 404


async def test_other_sites_cannot_read_answers_with_the_visitor_cookie():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/status", headers={"Origin": "https://elsewhere.example", "Cookie": "session=abc"})
    assert response.headers.get("access-control-allow-origin") == "*"
    assert "access-control-allow-credentials" not in response.headers


def test_telegram_downloads_stay_in_memory():
    """download_media() without in_memory writes a file named by the sender of the message."""
    import pathlib
    import re

    root = pathlib.Path(__file__).resolve().parents[1]
    calls = []
    for folder in ("Backend", "tools"):
        for source in (root / folder).rglob("*.py"):
            for line in source.read_text(encoding="utf-8").splitlines():
                if re.search(r"\.download_media\(|\.download\(", line):
                    calls.append((source.name, line.strip()))
    assert calls
    assert [c for c in calls if "in_memory=True" not in c[1]] == []
