from __future__ import annotations

from unittest.mock import AsyncMock

import httpx
import pytest

from aero.application.approval_details import approval_payload
from aero.data.modes import is_tool_allowed
from aero.toolbox import builtin_tools  # noqa: F401
from aero.toolbox.registry import get_registry
from aero.toolbox.tools import benefits

CID = "cpn_" + "a" * 32
COUPON = {
    "coupon_id": CID,
    "name": "补偿券",
    "status": "pending",
    "kind": "credits",
    "credits": 200,
    "validity_days": 15,
    "activation_deadline": "2026-12-01T00:00:00Z",
}


@pytest.fixture
def account(monkeypatch):
    account = AsyncMock()
    account.request.return_value = httpx.Response(200, json=COUPON)
    monkeypatch.setattr(benefits, "OfficialAccountSession", lambda: account)
    return account


async def test_queries_require_only_official_login_not_execution_or_official_model(account):
    for tool in (
        "list_my_coupons",
        "get_my_invitation",
        "activate_my_coupon",
        "check_client_update",
        "prepare_client_update",
        "get_client_update_status",
    ):
        assert get_registry().get(tool)
        assert is_tool_allowed(tool, "qa") and is_tool_allowed(tool, "plan")
    await benefits.get_my_invitation()
    account.request.assert_awaited_once_with("GET", "/v1/referrals/me", params={"page": 1})
    assert all("/code" not in str(call) for call in account.request.call_args_list)
    account.close.assert_awaited_once()


async def test_activation_needs_fresh_server_preview_and_discloses_timer(account):
    assert get_registry().get("activate_my_coupon").requires_confirmation
    assert (await benefits.activate_my_coupon(CID))["status"] == "error"
    assert all(call.args[0] == "GET" for call in account.request.call_args_list)
    preview = (await benefits.prepare_coupon_activation(CID))["result"]
    payload = approval_payload({"tool": "activate_my_coupon", "args": preview})
    assert "200 点" in str(payload) and "15 × 24 小时" in str(payload)
    assert "qwen3.8-flash" in payload["description"]
    await benefits.activate_my_coupon(**preview)
    assert account.request.call_args.args == ("POST", "/v1/coupons/" + CID + "/activate")
    assert account.request.call_args.kwargs["headers"]["X-Aerolytica-Benefit-Action"] == "1"


async def test_changed_or_revoked_coupon_cannot_use_old_approval(account):
    preview = (await benefits.prepare_coupon_activation(CID))["result"]
    account.request.return_value = httpx.Response(200, json={**COUPON, "credits": 201})
    assert (await benefits.activate_my_coupon(**preview))["status"] == "error"
    assert all(call.args[0] == "GET" for call in account.request.call_args_list)
    account.request.return_value = httpx.Response(200, json={**COUPON, "status": "revoked"})
    assert (await benefits.activate_my_coupon(**preview))["status"] == "error"


async def test_no_auth_or_ambiguous_response_never_claims_success(account):
    account.request.return_value = httpx.Response(401)
    assert "/login" in (await benefits.list_my_coupons())["message"]
    account.request.side_effect = httpx.ReadTimeout("network")
    assert (await benefits.list_my_coupons())["status"] == "unknown"
    assert (await benefits.prepare_coupon_activation("https://evil.test"))["status"] == "error"
