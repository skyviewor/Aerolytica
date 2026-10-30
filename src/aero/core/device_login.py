"""Consent-driven browser authorization, shared by TUI and the local Web service."""

from __future__ import annotations

import asyncio
import inspect
import math
import os
import platform
import sys
import time
import uuid
import webbrowser
from collections.abc import Awaitable, Callable
from urllib.parse import urlencode, urlsplit

import httpx

from aero.core.official_account import (
    OfficialAccountError,
    OfficialAccountSession,
    OfficialSessionData,
    _session_from_tokens,
    account_lock,
    save_official_session,
)
from aero.version import get_version

TERMINAL_STATES = {"approved", "denied", "expired", "cancelled", "failed"}


def login_recommendation() -> tuple[str, str]:
    """Heuristics recommend; only an explicit user action ever opens a browser."""
    if any(os.environ.get(key) for key in ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY")):
        return "manual", "检测到 SSH 连接，推荐使用网址和授权码。"
    if os.environ.get("CI") or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return "manual", "检测到非交互环境，推荐使用网址和授权码。"
    system = platform.system()
    if (
        (system == "Darwin" and bool(os.environ.get("TERM_PROGRAM")))
        or (system == "Windows" and os.environ.get("SESSIONNAME", "").lower() == "console")
        or (system == "Linux" and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")))
    ):
        return "browser", "检测到本机桌面终端，可在本机浏览器授权。"
    return "manual", "无法确认桌面浏览器可用，推荐使用网址和授权码。"


class DeviceLoginController:
    def __init__(
        self,
        account: OfficialAccountSession,
        *,
        commit: Callable[[OfficialSessionData], Awaitable[bool]] | None = None,
    ) -> None:
        self.account = account
        self._commit = commit
        self.flow_id = uuid.uuid4().hex
        self._private_code = ""
        self._cancelled = False
        self._cancel_sent = False
        self._waiting = False
        self._deadline = 0.0
        self._interval = 5.0
        self._state: dict = {"state": "idle", "flow_id": self.flow_id}

    def public_state(self) -> dict:
        state = dict(self._state)
        state["expires_in"] = max(0, int(self._deadline - time.monotonic()))
        return state

    async def _post(self, action: str, body: dict) -> httpx.Response:
        return await self.account._client.post(
            self.account.base_url + "/v1/auth/device-login/" + action,
            json=body,
            headers={"Cache-Control": "no-store"},
            timeout=15,
        )

    async def start(self, mode: str, *, open_browser: bool = True) -> dict:
        if mode not in {"browser", "manual"}:
            raise OfficialAccountError("请先选择登录方式。")
        if self._state["state"] != "idle":
            raise OfficialAccountError("本次授权已开始，请勿重复创建。")
        self._state["state"] = "starting"
        try:
            response = await self._post(
                "start",
                {
                    "client_version": get_version(),
                    "os_type": platform.system(),
                    "device_name": "Aerolytica CLI",
                },
            )
            if response.status_code >= 400:
                raise OfficialAccountError("账户服务暂不支持网页授权或请求过于频繁，请稍后重试。")
            data = response.json()
            if not isinstance(data, dict) or not isinstance(data.get("verification_uri"), str):
                raise ValueError("invalid_authorization_response")
            private = data["device_code"]
            url, short = data["verification_uri"], data["user_code"]
            parsed = urlsplit(url)
            if (
                not isinstance(private, str)
                or not 32 <= len(private) <= 128
                or not isinstance(short, str)
                or not 8 <= len(short) <= 16
                or not short.replace("-", "").isalnum()
                or not parsed.netloc
                or parsed.username
                or parsed.password
                or parsed.query
                or parsed.fragment
                or (
                    parsed.scheme != "https"
                    and not (
                        parsed.scheme == "http"
                        and parsed.hostname in {"localhost", "127.0.0.1", "::1"}
                    )
                )
            ):
                raise ValueError("invalid_authorization_response")
            interval, ttl = float(data["interval"]), float(data["expires_in"])
            if not math.isfinite(interval) or not math.isfinite(ttl):
                raise ValueError("invalid_authorization_response")
            self._private_code = private
            self._interval = max(5.0, min(60.0, interval))
            self._deadline = time.monotonic() + max(1, min(1800, int(ttl)))
            self._state.update(
                state="pending",
                mode=mode,
                verification_uri=url,
                user_code=short,
                verification_uri_complete=url + "?" + urlencode({"user_code": short}),
                message="请在官网确认授权，正在等待…",
            )
            if self._cancelled:
                await self.cancel()
            elif mode == "browser" and open_browser:
                await self.open_browser()
            return self.public_state()
        except (httpx.HTTPError, ValueError, KeyError, TypeError, OfficialAccountError) as exc:
            self._state.update(
                state="failed", message="无法开始网页授权，请检查服务版本及网络后重试。"
            )
            raise OfficialAccountError(self._state["message"]) from exc

    async def open_browser(self) -> None:
        if self._cancelled or self._state["state"] in TERMINAL_STATES:
            return
        try:
            opened = await asyncio.wait_for(
                asyncio.to_thread(webbrowser.open, self._state["verification_uri_complete"]),
                timeout=3,
            )
        except Exception:
            opened = False
        self._state["message"] = (
            "已尝试打开浏览器；也可手动访问网址并输入授权码。"
            if opened
            else "未能打开浏览器，请手动访问网址并输入授权码。"
        )

    async def cancel(self) -> None:
        if self._state["state"] == "approved":
            return
        self._cancelled = True
        self._state.update(state="cancelled", message="登录已取消，已有账户保持不变。")
        if self._private_code and not self._cancel_sent:
            self._cancel_sent = True
            try:
                await self.account._client.post(
                    self.account.base_url + "/v1/auth/device-login/cancel",
                    json={"device_code": self._private_code},
                    timeout=3,
                )
            except httpx.HTTPError:
                self._state["message"] = "已停止等待；服务端取消状态未确认，请勿继续批准该授权码。"

    async def wait(
        self, on_update: Callable[[dict], object] | None = None
    ) -> OfficialSessionData | None:
        if self._waiting:
            raise OfficialAccountError("本次授权已有等待任务。")
        self._waiting = True
        failures = 0
        try:
            while not self._cancelled and time.monotonic() < self._deadline:
                try:
                    response = await self._post("token", {"device_code": self._private_code})
                    if response.status_code >= 500:
                        raise OfficialAccountError("授权服务暂时不可用。")
                    payload = response.json()
                    if not isinstance(payload, dict):
                        raise ValueError("invalid_authorization_response")
                    error = payload.get("error")
                    if response.status_code == 200 and payload.get("access_token"):
                        if self._cancelled:
                            return None
                        if time.monotonic() >= self._deadline:
                            self._state.update(
                                state="expired", message="授权码已过期，请重新获取。"
                            )
                            await self._revoke_failed()
                            return None
                        data = _session_from_tokens(payload)
                        if (
                            not data.user_id
                            or data.refresh_expires_at <= time.time()
                            or data.access_expires_at <= time.time()
                            or not math.isfinite(data.access_expires_at)
                            or not math.isfinite(data.refresh_expires_at)
                        ):
                            raise OfficialAccountError("登录凭据不完整。")
                        committed = await (
                            self._commit(data) if self._commit else self.commit_session(data)
                        )
                        if not committed:
                            await self.cancel()
                            return None
                        return data
                    if error == "slow_down" or response.status_code == 429:
                        self._interval = max(
                            self._interval + 5, min(60, float(payload.get("interval", 0)))
                        )
                    elif error not in {"authorization_pending"}:
                        state = (
                            "denied"
                            if error == "access_denied"
                            else "expired"
                            if error == "expired_token"
                            else "failed"
                        )
                        self._state.update(
                            state=state,
                            message={
                                "denied": "网页已拒绝授权。",
                                "expired": "授权码已过期，请重新获取。",
                                "failed": "授权结果无法确认或已被使用，请重新获取授权码。",
                            }[state],
                        )
                        await self._revoke_failed()
                        return None
                    if failures:
                        self._state["message"] = "连接已恢复，正在等待官网授权…"
                    failures = 0
                except OSError:
                    self._state.update(state="failed", message="登录凭据未能保存，请检查本地权限。")
                    await self._revoke_failed()
                    return None
                except (httpx.HTTPError, ValueError, KeyError, TypeError, OfficialAccountError):
                    failures += 1
                    self._state["message"] = "连接暂时中断，正在等待网络恢复…"
                    if failures >= 3:
                        self._state.update(state="failed", message="网络结果未确认，请重新授权。")
                        await self._revoke_failed()
                        return None
                if self._cancelled:
                    return None
                if on_update:
                    result = on_update(self.public_state())
                    if inspect.isawaitable(result):
                        await result
                await asyncio.sleep(
                    min(self._interval * 2**failures, max(0, self._deadline - time.monotonic()))
                )
            if not self._cancelled:
                self._state.update(state="expired", message="授权码已过期，请重新获取。")
                await self._revoke_failed()
            return None
        except asyncio.CancelledError:
            await self.cancel()
            raise
        finally:
            self._waiting = False

    async def _revoke_failed(self) -> None:
        previous = dict(self._state)
        await self.cancel()
        self._state = previous

    async def commit_session(self, data: OfficialSessionData) -> bool:
        async with account_lock():
            if self._cancelled:
                return False
            save_official_session(data)
            self.account._session = data
            self._state.update(state="approved", message="登录成功。")
            return True
