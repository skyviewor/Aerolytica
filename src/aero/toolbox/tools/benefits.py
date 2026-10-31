"""Official account benefits, independent of model provider and project mode."""

from __future__ import annotations

import hashlib
import json
import re

import httpx

from aero.core.official_account import (
    OfficialAccountError,
    OfficialAccountSession,
    OfficialLoginRequiredError,
)
from aero.toolbox.registry import register_tool


async def _request(method: str, path: str, **kwargs) -> dict:
    session = OfficialAccountSession()
    try:
        response = await session.request(method, path, **kwargs)
        if response.status_code == 401:
            return {"status": "error", "message": "请使用 /login 登录官方账户；不要提供密码。"}
        if response.is_error:
            detail = response.json().get("detail")
            return {
                "status": "error",
                "message": detail
                if isinstance(detail, str)
                else "权益请求未获确认，请刷新状态；不要声称已激活。",
            }
        return {"status": "success", "result": response.json()}
    except OfficialLoginRequiredError:
        return {"status": "error", "message": "请使用 /login 登录官方账户；不要提供密码。"}
    except (OfficialAccountError, httpx.HTTPError, ValueError):
        return {
            "status": "unknown",
            "message": "未取得服务端确认，请重新查询状态，不要自动重试激活。",
        }
    finally:
        await session.close()


def _coupon_path(coupon_id: str) -> str:
    if not isinstance(coupon_id, str) or not re.fullmatch(r"cpn_[a-f0-9]{32}", coupon_id):
        raise ValueError("请先查询我的券，并使用服务端返回的券编号。")
    return "/v1/coupons/" + coupon_id


@register_tool(
    name="list_my_coupons",
    description="查询官方账户的券和状态。只读，不激活，不要求执行模式；自备模型也可用。",
    parameters={"type": "object", "properties": {"page": {"type": "integer", "minimum": 1}}},
)
async def list_my_coupons(page: int = 1) -> dict:
    if type(page) is not int or page < 1:
        return {"status": "error", "message": "页码必须为正整数。"}
    return await _request("GET", "/v1/coupons", params={"page": page})


@register_tool(
    name="get_my_invitation",
    description=(
        "查询本人已经生成的邀请码、邀请链接、直属拉新名单、邀请人和奖励。"
        "只读，不生成邀请码，不自动补绑；生成或补绑请引导官网邀请好友页面。"
        "不要求官方模型或执行模式。"
    ),
    parameters={"type": "object", "properties": {"page": {"type": "integer", "minimum": 1}}},
)
async def get_my_invitation(page: int = 1) -> dict:
    if type(page) is not int or page < 1:
        return {"status": "error", "message": "页码必须为正整数。"}
    return await _request("GET", "/v1/referrals/me", params={"page": page})


async def prepare_coupon_activation(coupon_id: str) -> dict:
    try:
        result = await _request("GET", _coupon_path(coupon_id))
    except ValueError as exc:
        return {"status": "error", "message": str(exc)}
    if result["status"] != "success":
        return result
    coupon = result["result"]
    if not isinstance(coupon, dict) or coupon.get("status") != "pending":
        return {"status": "error", "message": "此券不处于待激活状态，请重新查询。"}
    digest = hashlib.sha256(json.dumps(coupon, sort_keys=True).encode()).hexdigest()
    return {
        "status": "success",
        "result": {
            "coupon_id": coupon_id,
            "_coupon_preview": coupon,
            "_coupon_fingerprint": digest,
        },
    }


@register_tool(
    name="activate_my_coupon",
    description=(
        "激活本人券。先查询券，用户想激活时调用，每次必须经系统审批展示权益、"
        "激活限制和连续计时规则。不能会话级放行；免费用户激活额度券仍只可用"
        "qwen3.8-flash，空间券仅有效会员可激活。不要求项目执行模式。"
        "仅服务端确认 active 才可说激活成功，结果不明先查询，不重复投递。"
    ),
    parameters={
        "type": "object",
        "properties": {"coupon_id": {"type": "string"}},
        "required": ["coupon_id"],
        "additionalProperties": False,
    },
    requires_confirmation=True,
)
async def activate_my_coupon(coupon_id: str, **preview) -> dict:
    checked = await prepare_coupon_activation(coupon_id)
    if checked["status"] != "success":
        return checked
    if checked["result"]["_coupon_fingerprint"] != preview.get("_coupon_fingerprint"):
        return {"status": "error", "message": "券状态或审批信息已变化，请重新查询并批准。"}
    result = await _request(
        "POST", _coupon_path(coupon_id) + "/activate", headers={"X-Aerolytica-Benefit-Action": "1"}
    )
    if result["status"] == "success" and (
        not isinstance(result.get("result"), dict) or result["result"].get("status") != "active"
    ):
        return {"status": "unknown", "message": "未确认券已激活，请重新查询状态，不要自动重试。"}
    return result
