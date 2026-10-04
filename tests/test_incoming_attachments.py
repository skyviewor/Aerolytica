"""Private incoming attachment staging, without prompt expansion or credential leakage."""

from __future__ import annotations

import io
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image

from aero.application import incoming_attachments


def command(data: bytes, *, filename="notes.txt", content_type="text/plain"):
    return {
        "command_id": "command",
        "message_id": "message",
        "content": "inspect",
        "attachments": [
            {
                "attachment_id": "attachment",
                "filename": filename,
                "content_type": content_type,
                "size_bytes": len(data),
            }
        ],
    }


def mock_download(monkeypatch, data):
    requests = []
    original = httpx.AsyncClient

    def serve(request):
        requests.append(request)
        return httpx.Response(200, content=data)

    monkeypatch.setattr(
        incoming_attachments.httpx,
        "AsyncClient",
        lambda **kwargs: original(**kwargs, transport=httpx.MockTransport(serve)),
    )
    return requests


@pytest.mark.asyncio
async def test_text_stays_in_private_file_not_model_prompt(tmp_path, monkeypatch):
    data = b"FILE CONTENT MUST NOT BE IN PROMPT"
    requests = mock_download(monkeypatch, data)
    request = AsyncMock(return_value={"url": "https://oss.test/private"})
    prompt = await incoming_attachments.stage_command(command(data), request, tmp_path, "session")
    assert data.decode() not in prompt
    manifest = json.loads(prompt.split("\n")[3])
    from pathlib import Path

    path = Path(manifest[0]["path"])
    assert path.read_bytes() == data
    assert path.stat().st_mode & 0o777 == 0o600
    assert path.parent.stat().st_mode & 0o777 == 0o700
    assert requests[0].headers.get("Authorization") is None
    assert requests[0].headers.get("Cookie") is None
    request.assert_awaited_once_with(
        "POST", "messages/message/attachments/attachment/download-url", json={}
    )


@pytest.mark.asyncio
async def test_image_follows_vision_path(tmp_path, monkeypatch):
    buffer = io.BytesIO()
    Image.new("RGB", (2, 2)).save(buffer, format="PNG")
    data = buffer.getvalue()
    mock_download(monkeypatch, data)
    prompt = await incoming_attachments.stage_command(
        command(data, filename="image.png", content_type="image/png"),
        AsyncMock(return_value={"url": "https://oss.test/image"}),
        tmp_path,
        "session",
    )
    assert '"kind": "image"' in prompt
    assert "视觉分析工具" in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["../escape.txt", "nested/file.txt", "x\\escape.txt"])
async def test_rejects_remote_paths(tmp_path, filename):
    request = AsyncMock()
    with pytest.raises(ValueError, match="filename"):
        await incoming_attachments.stage_command(
            command(b"x", filename=filename), request, tmp_path, "session"
        )
    request.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["http://oss.test/file", "https://user:secret@oss.test/file"])
async def test_rejects_unsafe_download_url(tmp_path, url):
    with pytest.raises(ValueError, match="https"):
        await incoming_attachments.stage_command(
            command(b"x"), AsyncMock(return_value={"url": url}), tmp_path, "session"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [b"too many bytes", b"", b"\x00", b"\xff"])
async def test_rejects_size_or_binary_mismatch(tmp_path, monkeypatch, data):
    mock_download(monkeypatch, data)
    with pytest.raises((ValueError, UnicodeDecodeError)):
        await incoming_attachments.stage_command(
            command(b"x"),
            AsyncMock(return_value={"url": "https://oss.test/file"}),
            tmp_path,
            "session",
        )
    assert not list(tmp_path.rglob("*.txt"))


@pytest.mark.asyncio
async def test_rejects_symlink_staging_root(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    root = tmp_path / "staging"
    root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(RuntimeError, match="unsafe_state_directory"):
        await incoming_attachments.stage_command(command(b"x"), AsyncMock(), root, "session")


@pytest.mark.asyncio
async def test_legacy_text_command_needs_no_download(tmp_path):
    request = AsyncMock()
    assert (
        await incoming_attachments.stage_command({"content": "hello"}, request, tmp_path, "session")
        == "hello"
    )
    request.assert_not_called()
