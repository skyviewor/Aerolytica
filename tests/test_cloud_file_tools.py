"""Hermetic official cloud transfer and tool exposure regressions."""

from __future__ import annotations

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from aero.application.approval_details import approval_payload
from aero.core.cloud_files import CloudFileError, CloudFiles, local_parent
from aero.core.official_account import OfficialLoginRequiredError
from aero.data.modes import is_tool_allowed
from aero.toolbox.paths import use_workspace
from aero.toolbox.registry import get_registry
from aero.toolbox.tools import cloud


@pytest.fixture
def wire(tmp_path):
    calls = []
    body = b"literature review"
    settings = SimpleNamespace(short=False, bad_hash=False, fail_put=False, fail_complete=False)

    async def handler(request):
        calls.append(request)
        path = request.url.path
        if path == "/v1/storage/projects":
            return httpx.Response(200, json={"items": [{"project_id": "p", "name": "research"}]})
        if path.endswith("/snapshot"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "entity_type": "file",
                            "data": {
                                "file_id": "f",
                                "filename": "report.md",
                                "status": "active",
                                "object_key": "private-key",
                                "download_url": "secret-url",
                            },
                        }
                    ],
                    "has_more": True,
                    "next_cursor": "next",
                },
            )
        if path == "/v1/storage/v2/uploads":
            return httpx.Response(
                200,
                json={
                    "file_id": "f",
                    "upload_id": "u",
                    "upload_url": "https://oss.test/upload?sig=secret",
                },
            )
        if path.endswith("/complete"):
            return httpx.Response(
                503 if settings.fail_complete else 200,
                json={"file_id": "f", "version": 1, "status": "active"},
            )
        if path.endswith("/fail"):
            return httpx.Response(200, json={})
        if path.endswith("/revisions"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "revision_id": "r",
                            "is_current": True,
                            "size_bytes": len(body),
                            "etag": "0" * 32
                            if settings.bad_hash
                            else hashlib.md5(body).hexdigest(),
                        }
                    ]
                },
            )
        if path.endswith("/download-url"):
            assert __import__("json").loads(request.content) == {"revision_id": "r"}
            return httpx.Response(
                200, json={"download_url": "https://oss.test/download?sig=secret"}
            )
        if path == "/upload":
            assert await request.aread() == body
            return httpx.Response(500 if settings.fail_put else 200)
        if path == "/download":
            return httpx.Response(200, content=body[:-1] if settings.short else body)
        raise AssertionError(path)

    async def setup():
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        session = SimpleNamespace(
            request=lambda method, path, **kw: http.request(
                method, "https://relay.test" + path, **kw
            ),
        )
        files = CloudFiles(session)
        files.client._transfer_client = http
        return files, http

    return setup, calls, body, settings


async def test_registration_modes_approval_and_no_signed_metadata(wire):
    import aero.toolbox.builtin_tools  # noqa: F401

    for name in ("upload_cloud_file", "download_cloud_file"):
        assert get_registry().get(name).requires_confirmation
        assert not is_tool_allowed(name, "plan")
        assert not is_tool_allowed(name, "qa")
        assert is_tool_allowed(name, "execute")
    assert is_tool_allowed("list_cloud_files", "qa")
    details = approval_payload(
        {"tool": "upload_cloud_file", "args": {"file_path": "data/report.md", "project_id": "p"}}
    )
    assert details["title"] == "上传文件到官方云空间"
    assert {d["value"] for d in details["details"]} == {"data/report.md", "p"}
    files, http = await wire[0]()
    async with http:
        assert (await files.projects())[0]["project_id"] == "p"
        result = await files.listing("p", "previous")
    assert result["next_cursor"] == "next" and result["has_more"]
    assert "object_key" not in result["items"][0]
    assert "download_url" not in result["items"][0]
    assert wire[1][-1].url.params["cursor"] == "previous"


def test_terminal_approval_shows_target_and_scope():
    from aero.agent.system_prompt import build_system_prompt
    from aero.cli.main import AeroApp
    from aero.core.config import AeroConfig

    app = SimpleNamespace(config=SimpleNamespace(language="zh"))
    message = AeroApp._build_confirm_message(app, "upload_cloud_file", {
        "file_path": "data/paper/summary.md", "project_id": "project_research",
    })
    assert "data/paper/summary.md" in message and "project_research" in message
    assert "不会覆盖" in message and "不开启目录自动同步" in message
    for language in ("zh", "en"):
        prompt = build_system_prompt(AeroConfig(), language=language)
        assert "upload_cloud_file" in prompt and "download_cloud_file" in prompt


async def test_upload_data_report_commits_only_after_put(wire, tmp_path):
    files, http = await wire[0]()
    (tmp_path / "data").mkdir()
    (tmp_path / "data/report.md").write_bytes(wire[2])
    with use_workspace(tmp_path):
        async with http:
            result = await files.upload("data/report.md", "p", None)
    assert result["file_id"] == "f" and "upload_url" not in result
    assert [r.url.path for r in wire[1]] == [
        "/v1/storage/v2/uploads",
        "/upload",
        "/v1/storage/v2/uploads/u/complete",
    ]


@pytest.mark.parametrize("failure", ["fail_put", "fail_complete"])
async def test_failed_upload_does_not_retry_or_fail_ambiguous_commit(wire, tmp_path, failure):
    files, http = await wire[0]()
    setattr(wire[3], failure, True)
    (tmp_path / "report.md").write_bytes(wire[2])
    with use_workspace(tmp_path), pytest.raises(Exception):
        async with http:
            await files.upload("report.md", "p", None)
    paths = [r.url.path for r in wire[1]]
    assert sum(p == "/upload" for p in paths) == 1
    assert ("/v1/storage/v2/uploads/u/fail" in paths) == (failure == "fail_put")


@pytest.mark.parametrize(
    "path",
    ["../escape.md", ".env", ".aero/secret.yaml", "aero.yaml", "credentials/key.md", "a.pem"],
)
def test_unsafe_paths_blocked(tmp_path, path):
    with use_workspace(tmp_path), pytest.raises(ValueError):
        with local_parent(path):
            pytest.fail("unsafe path admitted")


@pytest.mark.parametrize("kind", ["symlink", "parent_symlink", "hardlink", "secret", "late_secret"])
async def test_upload_rejects_links_and_credentials_before_network(wire, tmp_path, kind):
    files, http = await wire[0]()
    original = tmp_path / "original.md"
    original.write_bytes(wire[2])
    path = "report.md"
    if kind == "symlink":
        (tmp_path / path).symlink_to(original)
    elif kind == "parent_symlink":
        (tmp_path / "alias").symlink_to(tmp_path, target_is_directory=True)
        path = "alias/original.md"
    elif kind == "hardlink":
        (tmp_path / path).hardlink_to(original)
    else:
        (tmp_path / path).write_text(
            ("a\n" * 600000 if kind == "late_secret" else "")
            + "api_key=sk-abcdefghijklmnop123456789"
        )
    with use_workspace(tmp_path), pytest.raises((ValueError, OSError)):
        async with http:
            await files.upload(path, "p", None)
    assert not wire[1]


@pytest.mark.parametrize("failure", [None, "short", "bad_hash", "exists"])
async def test_download_integrity_atomic_and_no_overwrite(wire, tmp_path, failure):
    files, http = await wire[0]()
    target = tmp_path / "report.md"
    if failure == "exists":
        target.write_text("keep me")
    elif failure:
        setattr(wire[3], failure, True)
    with use_workspace(tmp_path):
        async with http:
            if failure:
                with pytest.raises(ValueError):
                    await files.download("f", "report.md")
            else:
                await files.download("f", "report.md")
    assert not list(tmp_path.glob(".aero-download-*"))
    if not failure:
        assert target.read_bytes() == wire[2]
    elif failure == "exists":
        assert target.read_text() == "keep me"
    else:
        assert not target.exists()


async def test_missing_login_does_not_transfer_or_expose_credentials(monkeypatch):
    session = SimpleNamespace(
        access_token=AsyncMock(side_effect=OfficialLoginRequiredError("token=private")),
        close=AsyncMock(),
    )
    monkeypatch.setattr(cloud, "OfficialAccountSession", lambda: session)
    result = await cloud.upload_cloud_file("data/report.md", "p")
    assert result["status"] == "error" and "/login" in result["message"]
    assert "private" not in str(result)
    session.close.assert_awaited_once()


@pytest.mark.parametrize("status", [401, 403, 404, 409, 413, 429, 503])
async def test_service_failures_keep_status_but_not_private_body(status):
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(status, json={"detail": "credential=private"})
        )
    )
    files = CloudFiles(
        SimpleNamespace(
            request=lambda method, path, **kw: http.request(
                method, "https://relay.test" + path, **kw
            )
        )
    )
    async with http:
        with pytest.raises(CloudFileError) as caught:
            await files.listing("p")
    assert caught.value.status_code == status
    assert "private" not in str(caught.value)
