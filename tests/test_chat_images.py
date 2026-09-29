"""Conversation images are private files, independent of context synchronization."""

from __future__ import annotations

import stat
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from PIL import Image

from aero.application.chat_images import ImagePublisher, describe_images, open_image


def test_descriptors_are_stable_and_deduplicate_local_images():
    text = "![map](figures/a_b.png) ![](figures/a_b.png) ![](https://example.com/a.png)"
    images = describe_images(text, "reply")
    assert len(images) == 1
    assert images == describe_images(text, "reply")
    assert images != describe_images(text, "another-reply")
    assert images[0]["source"] == "figures/a_b.png"


def test_image_reader_rejects_paths_and_symlinks_outside_workspace(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    image = tmp_path / "private.png"
    Image.new("RGB", (2, 2)).save(image)
    (workspace / "escape.png").symlink_to(image)
    for source in (str(image), "../private.png", "escape.png"):
        with pytest.raises(ValueError, match="项目目录"):
            open_image(source, [workspace])


def test_reader_checks_bytes_not_extension(tmp_path):
    (tmp_path / "fake.png").write_text("<script>bad</script>")
    with pytest.raises(OSError):
        open_image("fake.png", [tmp_path])


def test_local_snapshot_survives_overwrite_and_restart(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    source = workspace / "map.png"
    Image.new("RGB", (2, 2), "red").save(source)
    original = source.read_bytes()
    journal = tmp_path / "state" / "images.json"
    publisher = ImagePublisher(AsyncMock(), lambda: [workspace], journal)
    descriptor = publisher.describe("![](map.png)", "event")[0]
    Image.new("RGB", (2, 2), "blue").save(source)
    restarted = ImagePublisher(AsyncMock(), lambda: [workspace], journal)
    snapshot = restarted.locations[descriptor["attachment_id"]]["snapshot"]
    assert Path(snapshot).read_bytes() == original
    assert stat.S_IMODE(Path(snapshot).stat().st_mode) == 0o600
    assert stat.S_IMODE(journal.stat().st_mode) == 0o600


@pytest.mark.asyncio
async def test_upload_is_direct_to_oss_without_relay_credentials(tmp_path, monkeypatch):
    image = tmp_path / "map.png"
    Image.new("RGB", (2, 2)).save(image)
    request = AsyncMock(side_effect=[
        {"items": [{"message_id": "msg", "attachment_id": "image", "source": "map.png"}]},
        {"upload_url": "https://oss.example/image", "upload_id": "upload"},
        {},
    ])
    calls = []

    def handler(req):
        calls.append(req)
        assert req.content == image.read_bytes()
        assert "Authorization" not in req.headers
        assert "Cookie" not in req.headers
        assert req.headers["Content-Type"] == "image/png"
        return httpx.Response(200)

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs:
                        original(transport=httpx.MockTransport(handler), **kwargs))
    publisher = ImagePublisher(request, lambda: [tmp_path], tmp_path / "state" / "images.json")
    await publisher.flush()
    assert len(calls) == 1
    assert request.await_args_list[-1].args == ("POST", "messages/msg/images/image/complete")


@pytest.mark.asyncio
async def test_quota_failure_sets_readable_image_error(tmp_path):
    Image.new("RGB", (2, 2)).save(tmp_path / "map.png")
    request = AsyncMock(side_effect=[
        {"items": [{"message_id": "msg", "attachment_id": "image", "source": "map.png"}]},
        httpx.HTTPStatusError("quota", request=httpx.Request("POST", "https://relay.test"),
                              response=httpx.Response(413)),
        {},
    ])
    publisher = ImagePublisher(request, lambda: [tmp_path], tmp_path / "state" / "images.json")
    await publisher.flush()
    assert request.await_args_list[-1].args[-1].endswith("/fail")
    assert "额度不足" in request.await_args_list[-1].kwargs["json"]["error"]


@pytest.mark.asyncio
async def test_non_https_oss_url_never_receives_image(tmp_path):
    Image.new("RGB", (2, 2)).save(tmp_path / "map.png")
    request = AsyncMock(side_effect=[
        {"items": [{"message_id": "msg", "attachment_id": "image", "source": "map.png"}]},
        {"upload_url": "http://oss.example/image"}, {},
    ])
    publisher = ImagePublisher(request, lambda: [tmp_path], tmp_path / "state" / "images.json")
    await publisher.flush()
    assert "HTTPS" in request.await_args_list[-1].kwargs["json"]["error"]


@pytest.mark.asyncio
async def test_busy_storage_keeps_image_pending_for_automatic_retry(tmp_path):
    Image.new("RGB", (2, 2)).save(tmp_path / "map.png")
    response = httpx.Response(503, json={"detail": {"code": "image_busy"}})
    request = AsyncMock(side_effect=[
        {"items": [{"message_id": "msg", "attachment_id": "image", "source": "map.png"}]},
        httpx.HTTPStatusError("busy", request=httpx.Request("POST", "https://relay.test"),
                              response=response),
    ])
    publisher = ImagePublisher(request, lambda: [tmp_path], tmp_path / "state" / "images.json")
    await publisher.flush()
    assert request.await_count == 2


@pytest.mark.asyncio
async def test_resident_reply_uses_current_local_session_events(tmp_path):
    from aero.application.cloud_runtime import CloudAgentRuntime
    from aero.application.events import RunEvent

    Image.new("RGB", (2, 2)).save(tmp_path / "map.png")

    async def events(_run):
        yield RunEvent(1, "session", "run", "assistant_delta", {"text": "画好了 ![](map.png)"})
        yield RunEvent(2, "session", "run", "run_completed", {"state": "completed"})

    session = SimpleNamespace(id="session", project_dir=tmp_path,
                              start_run=lambda _: "run", events=events,
                              run_status=lambda _: {"state": "completed"})
    transport = SimpleNamespace(agent_id="agent", request=AsyncMock(return_value={}))
    runtime = CloudAgentRuntime(session, transport, device_id="device", project_id="project",
                                account_id="user", account_identity=lambda: "user",
                                state_dir=tmp_path / "state")
    runtime._assert_lease = lambda: None
    await runtime._execute({"command_id": "command", "session_id": "session", "content": "画图"})
    reply = transport.request.await_args_list[-1].kwargs["json"]
    assert reply["content"] == "画好了 ![](map.png)"
    assert len(reply["images"]) == 1


def test_resident_foreground_reply_queues_user_and_image_without_prompts(tmp_path):
    from aero.application.cloud_runtime import CloudAgentRuntime

    Image.new("RGB", (2, 2)).save(tmp_path / "map.png")
    session = SimpleNamespace(id="session", project_dir=tmp_path,
                              run_status=lambda _: {"state": "completed"},
                              messages_view=lambda: [
                                  {"role": "user", "content": "画图"},
                                  {"role": "assistant", "content": "![](map.png)"},
                              ])
    transport = SimpleNamespace(agent_id="agent", request=AsyncMock())
    runtime = CloudAgentRuntime(session, transport, device_id="device", project_id="project",
                                account_id="user", account_identity=lambda: "user",
                                state_dir=tmp_path / "state")
    runtime._queue_foreground_messages("run")
    restarted = CloudAgentRuntime(session, transport, device_id="device", project_id="project",
                                  account_id="user", account_identity=lambda: "user",
                                  state_dir=tmp_path / "state")
    assert [event["type"] for event in restarted._conversation_events] == [
        "user_message", "message",
    ]
    assert restarted._conversation_events[1]["images"][0]["source"] == "map.png"
