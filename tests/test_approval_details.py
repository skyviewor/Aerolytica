import asyncio
from types import SimpleNamespace

import pytest

from aero.application.approval_details import approval_payload
from aero.application.cloud_runtime import CloudAgentRuntime


def test_shell_approval_describes_operation_and_redacts_credentials():
    payload = approval_payload(
        {
            "tool": "run_shell",
            "args": {
                "command": (
                    "OPENAI_API_KEY=hidden-value python -m pip ins"
                    "tall cnmaps --password 'hidden pass'"
                ),
                "workdir": "/tmp/project",
                "content": "private file",
                "token": "private token",
            },
        }
    )
    assert payload["title"] == "执行本地命令"
    text = str(payload)
    assert "python -m pip install cnmaps" in text
    assert "/tmp/project" in text
    assert "hidden-value" not in text
    assert "hidden pass" not in text
    assert "private file" not in text
    assert "private token" not in text


def test_batch_file_targets_are_separate_and_file_contents_never_leave_client():
    payload = approval_payload(
        {
            "tool": "write_file",
            "batch_args": [
                {"file_path": "a.py", "content": "secret contents"},
                {"file_path": "b.py", "content": "other contents"},
            ],
        }
    )
    assert [row["value"] for row in payload["details"]] == ["a.py", "b.py"]
    assert "contents" not in str(payload)
    assert "切换" in approval_payload({"tool": "propose_execution"})["description"]


@pytest.mark.asyncio
async def test_resident_approval_posts_operation_before_waiting_for_user():
    runtime = object.__new__(CloudAgentRuntime)
    posted = []

    async def request(method, endpoint, **kwargs):
        if method == "POST":
            posted.append(kwargs["json"])
            return {}
        return {"approvals": [{"request_id": "run:event", "status": "approved"}]}

    runtime.transport = SimpleNamespace(request=request)
    runtime._stop = asyncio.Event()
    runtime._assert_lease = lambda: None
    decisions = []
    runtime.session = SimpleNamespace(confirm=lambda *args: decisions.append(args))
    await runtime._approval(
        "cmd",
        "run",
        SimpleNamespace(
            id="event",
            data={
                "tool": "run_shell",
                "args": {"command": "python -m pip install cnmaps"},
            },
        ),
    )
    assert posted[0]["payload"]["details"][0]["value"] == "python -m pip install cnmaps"
    assert decisions == [("run", "allow")]
