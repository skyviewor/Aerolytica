"""Tests for the shared Aerolytica official-account session."""

import asyncio
import stat
import time

import httpx
import pytest

from aero.core.official_account import (
    CloudSyncClient,
    OfficialAccountError,
    OfficialAccountSession,
    OfficialLoginRequiredError,
    OfficialSessionData,
    clear_official_session,
    load_official_session,
    save_official_session,
)


def _token_payload(*, access: str = "jwt-new", refresh: str = "rfr-new") -> dict:
    return {
        "user_id": "usr_1",
        "email": "user@example.com",
        "access_token": access,
        "refresh_token": refresh,
        "expires_in": 3600,
        "refresh_expires_in": 86400,
        "token_type": "bearer",
    }


@pytest.fixture
def secrets_path(tmp_path, monkeypatch):
    path = tmp_path / "secrets.yaml"
    monkeypatch.setenv("AERO_SECRETS_PATH", str(path))
    return path


@pytest.mark.asyncio
async def test_login_persists_tokens_without_password(secrets_path):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/auth/login"
        return httpx.Response(200, json=_token_payload(), request=request)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    session = OfficialAccountSession(base_url="https://api.test", client=client)

    result = await session.login("user@example.com", "never-save-this")

    assert result.email == "user@example.com"
    assert load_official_session().access_token == "jwt-new"
    assert "never-save-this" not in secrets_path.read_text()
    assert stat.S_IMODE(secrets_path.stat().st_mode) == 0o600
    await client.aclose()


@pytest.mark.asyncio
async def test_concurrent_refresh_is_coalesced(secrets_path):
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return httpx.Response(200, json=_token_payload(), request=request)

    save_official_session(
        OfficialSessionData(
            access_token="jwt-old",
            refresh_token="rfr-old",
            access_expires_at=time.time() - 1,
            refresh_expires_at=time.time() + 3600,
        )
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    session = OfficialAccountSession(base_url="https://api.test", client=client)

    tokens = await asyncio.gather(*(session.access_token() for _ in range(5)))

    assert tokens == ["jwt-new"] * 5
    assert calls == 1
    assert load_official_session().refresh_token == "rfr-new"
    await client.aclose()


@pytest.mark.asyncio
async def test_authenticated_request_refreshes_once_after_401(secrets_path):
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        if request.url.path == "/v1/auth/refresh":
            return httpx.Response(200, json=_token_payload(), request=request)
        if request.headers["Authorization"] == "Bearer jwt-old":
            return httpx.Response(401, json={"detail": "expired"}, request=request)
        return httpx.Response(200, json={"user_id": "usr_1"}, request=request)

    save_official_session(
        OfficialSessionData(
            access_token="jwt-old",
            refresh_token="rfr-old",
            access_expires_at=time.time() + 3600,
            refresh_expires_at=time.time() + 7200,
        )
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    session = OfficialAccountSession(base_url="https://api.test", client=client)

    response = await session.request("GET", "/v1/auth/me")

    assert response.status_code == 200
    assert paths == ["/v1/auth/me", "/v1/auth/refresh", "/v1/auth/me"]
    await client.aclose()


@pytest.mark.asyncio
async def test_expired_refresh_token_clears_session(secrets_path):
    save_official_session(
        OfficialSessionData(
            access_token="jwt-old",
            refresh_token="rfr-old",
            access_expires_at=time.time() - 2,
            refresh_expires_at=time.time() - 1,
        )
    )
    session = OfficialAccountSession(
        base_url="https://api.test",
        client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _: None)),
    )

    with pytest.raises(OfficialLoginRequiredError):
        await session.access_token()

    assert not load_official_session().is_logged_in
    clear_official_session()
    await session._client.aclose()


@pytest.mark.asyncio
async def test_cloud_sync_boundary_uses_shared_session():
    class StubSession:
        async def request(self, method, path, **kwargs):
            return method, path, kwargs

    sync = CloudSyncClient(StubSession())
    result = await sync.request("GET", "/v1/files", params={"cursor": "next"})

    assert result == ("GET", "/v1/files", {"params": {"cursor": "next"}})


@pytest.mark.asyncio
async def test_multiple_session_instances_share_rotating_token(secrets_path):
    save_official_session(OfficialSessionData(
        access_token="old", refresh_token="old-refresh", access_expires_at=1,
        refresh_expires_at=time.time() + 3600, user_id="usr_1",
    ))
    calls = []

    async def handler(request):
        calls.append(request)
        await asyncio.sleep(0.02)
        return httpx.Response(200, json=_token_payload())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        sessions = [OfficialAccountSession(client=client) for _ in range(4)]
        assert await asyncio.gather(*(s.access_token() for s in sessions)) == ["jwt-new"] * 4
    assert len(calls) == 1


@pytest.mark.asyncio
async def test_refresh_outage_does_not_log_user_out(secrets_path):
    save_official_session(OfficialSessionData(
        access_token="old", refresh_token="keep", access_expires_at=1,
        refresh_expires_at=time.time() + 3600,
    ))
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(503)
    )) as client:
        with pytest.raises(OfficialAccountError, match="暂不可用"):
            await OfficialAccountSession(client=client).access_token()
    assert load_official_session().refresh_token == "keep"


@pytest.mark.asyncio
async def test_session_sees_logout_from_another_instance(secrets_path):
    save_official_session(OfficialSessionData(
        access_token="old", refresh_token="keep", access_expires_at=time.time() + 3600,
    ))
    session = OfficialAccountSession()
    clear_official_session()
    with pytest.raises(OfficialLoginRequiredError):
        await session.access_token()
    await session.close()


@pytest.mark.asyncio
async def test_upload_streams_without_jwt_then_completes(tmp_path):
    path = tmp_path / "test.txt"
    path.write_bytes(b"hello")
    calls = []

    class Session:
        async def request(self, method, endpoint, **kwargs):
            calls.append((endpoint, kwargs))
            if endpoint.endswith("upload-url"):
                return httpx.Response(200, json={
                    "object_id": "obj_1", "upload_url": "https://oss.test/file",
                })
            return httpx.Response(200, json={"status": "active", "etag": "etag"})

    async def put(request):
        assert request.method == "PUT"
        assert "authorization" not in request.headers
        assert await request.aread() == b"hello"
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(put)) as transfer:
        result = await CloudSyncClient(Session(), transfer).upload_file(
            path, project_id="p", directory_id="d",
        )
    assert result["status"] == "active"
    assert calls[0][1]["json"]["project_id"] == "p"
    assert calls[-1][0] == "/v1/storage/obj_1/complete"


@pytest.mark.asyncio
async def test_failed_put_releases_pending_upload(tmp_path):
    path = tmp_path / "test.txt"
    path.write_bytes(b"hello")
    endpoints = []

    class Session:
        async def request(self, method, endpoint, **kwargs):
            endpoints.append(endpoint)
            return httpx.Response(200, json={
                "object_id": "obj_1", "upload_url": "https://oss.test/file",
            })

    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda _: httpx.Response(403)
    )) as transfer:
        with pytest.raises(OfficialAccountError, match="上传失败"):
            await CloudSyncClient(Session(), transfer).upload_file(path)
    assert endpoints[-1] == "/v1/storage/obj_1/fail"
