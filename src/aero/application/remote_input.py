"""Await ordinary input without replaying the current tool or user command."""

import asyncio
import time
import uuid

import httpx

from aero.toolbox.user_input import validate_values


async def request_remote_input(send, description: dict) -> dict:
    request_id = uuid.uuid4().hex
    path = f"inputs/{request_id}"
    deadline = time.monotonic() + 900

    async def retry(endpoint, **kwargs):
        while time.monotonic() < deadline:
            try:
                return await send(endpoint, **kwargs)
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500:
                    raise
                await asyncio.sleep(1)
        raise TimeoutError("input_request_expired")

    try:
        await retry("inputs", json={"request_id": request_id, "description": description})
        while time.monotonic() < deadline:
            result = await retry(f"{path}/claim")
            state = result["request"]["status"]
            if result.get("cancel_requested") or state in {"canceled", "expired"}:
                return {"status": state}
            if state == "submitted":
                values = validate_values(description, result["values"])
                await retry(f"{path}/ack")
                await retry(f"{path}/result", json={"status": "success"})
                return {"status": "submitted", "request_id": request_id, "values": values}
            await asyncio.sleep(1)
        return {"status": "expired"}
    except (httpx.HTTPError, TimeoutError, RuntimeError):
        return {"status": "unavailable", "message": "输入请求已失效或连接不可用，任务未自动重跑。"}
