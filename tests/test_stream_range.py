"""The download route answers whole-file, ranged and HEAD requests with the right bytes."""

import hashlib

import pytest
from httpx import ASGITransport, AsyncClient

from Backend.fastapi.main import app
from Backend.fastapi.routes import stream_routes
from Backend.fastapi.security.tokens import verify_token

CHUNK = 1024 * 1024
SIZE = 3 * CHUNK + 4321
DATA = b"".join(hashlib.sha256(str(i).encode()).digest() for i in range(SIZE // 32 + 1))[:SIZE]
URL = "/dl/sometoken/someid/Harbor%20Lights%20-%2001.mkv"


class FakeFile:
    file_size = SIZE
    file_name = "Harbor Lights - 01.mkv"
    mime_type = "video/x-matroska"


class FakeStreamer:
    async def get_file_properties(self, chat_id, message_id):
        return FakeFile()

    async def prefetch_stream(self, *, offset, first_part_cut, last_part_cut, part_count, chunk_size, **_):
        async def body():
            for part in range(part_count):
                chunk = DATA[offset + part * chunk_size: offset + (part + 1) * chunk_size]
                if part_count == 1:
                    yield chunk[first_part_cut:last_part_cut]
                elif part == 0:
                    yield chunk[first_part_cut:]
                elif part == part_count - 1:
                    yield chunk[:last_part_cut]
                else:
                    yield chunk
        return body()


@pytest.fixture
def client(monkeypatch):
    streamer = FakeStreamer()

    async def decode(_):
        return {"chat_id": "1234567890", "msg_id": 5}

    async def fake_db_client(fetch):
        return 0, streamer, await fetch(streamer)

    async def nothing(*_, **__):
        return None

    async def title(_, name):
        return name

    monkeypatch.setattr(stream_routes, "decode_string", decode)
    monkeypatch.setattr(stream_routes, "_db_client", fake_db_client)
    monkeypatch.setattr(stream_routes, "_lookup_title", title)
    monkeypatch.setattr(stream_routes, "_prefetch_for", lambda index: (1, 1))
    monkeypatch.setattr(stream_routes, "record_stream_start", nothing)
    monkeypatch.setattr(stream_routes, "track_usage", nothing)
    monkeypatch.setattr(stream_routes.own_bots, "active", lambda token_data: False)
    app.dependency_overrides[verify_token] = lambda: {"token": "sometoken", "name": "tester"}
    yield AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")
    app.dependency_overrides.pop(verify_token, None)


async def test_whole_file(client):
    async with client:
        response = await client.get(URL)
    assert response.status_code == 200
    assert response.headers["content-length"] == str(SIZE)
    assert response.headers["accept-ranges"] == "bytes"
    assert "content-range" not in response.headers
    assert response.content == DATA


@pytest.mark.parametrize("header, start, end", [
    ("bytes=0-0", 0, 0),
    ("bytes=100-199", 100, 199),
    (f"bytes={CHUNK - 5}-{2 * CHUNK + 5}", CHUNK - 5, 2 * CHUNK + 5),
    (f"bytes={CHUNK}-{2 * CHUNK - 1}", CHUNK, 2 * CHUNK - 1),
    (f"bytes={SIZE - 10}-", SIZE - 10, SIZE - 1),
    ("bytes=-500", SIZE - 500, SIZE - 1),
    (f"bytes=10-{SIZE + 999}", 10, SIZE - 1),
])
async def test_ranges(client, header, start, end):
    async with client:
        response = await client.get(URL, headers={"Range": header})
    assert response.status_code == 206
    assert response.headers["content-range"] == f"bytes {start}-{end}/{SIZE}"
    assert response.headers["content-length"] == str(end - start + 1)
    assert response.content == DATA[start:end + 1]


async def test_head_sends_headers_only(client):
    async with client:
        response = await client.head(URL)
    assert response.status_code == 200
    assert response.headers["content-length"] == str(SIZE)
    assert response.headers["content-type"].startswith("video/x-matroska")
    assert response.content == b""


@pytest.mark.parametrize("header", ["bytes=abc-def", f"bytes={SIZE + 5}-{SIZE + 9}", "bytes=1-2-3"])
async def test_bad_range_is_refused(client, header):
    async with client:
        response = await client.get(URL, headers={"Range": header})
    assert response.status_code == 416
    assert response.headers["content-range"] == f"bytes */{SIZE}"


async def test_download_needs_a_valid_token():
    async def refuse(token: str):
        from fastapi import HTTPException
        raise HTTPException(status_code=401, detail="Invalid or expired API token")

    app.dependency_overrides[verify_token] = refuse
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
            response = await c.get(URL)
    finally:
        app.dependency_overrides.pop(verify_token, None)
    assert response.status_code == 401
