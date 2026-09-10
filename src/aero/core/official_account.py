"""Official Aerolytica account authentication and platform API access."""

from __future__ import annotations

import asyncio
import fcntl
import mimetypes
import os
import time
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from aero.core.config import load_user_secrets, save_user_secrets, user_secrets_path

DEFAULT_PLATFORM_API_URL = "https://api.aerolytica.skyviewor.team"
DEFAULT_RELAY_LLM_URL = "https://llm.aerolytica.skyviewor.team/v1"
_REFRESH_SKEW_SECONDS = 60


@asynccontextmanager
async def account_lock():
    """Serialize rotating refresh tokens across TUI, Web and resident workers."""
    path = user_secrets_path().with_suffix(".account.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as handle:
        while True:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                await asyncio.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class OfficialAccountError(RuntimeError):
    """A user-facing official-account error."""


class OfficialLoginRequiredError(OfficialAccountError):
    """The saved official session cannot be used or refreshed."""


@dataclass(frozen=True)
class OfficialSessionData:
    access_token: str = ""
    refresh_token: str = ""
    access_expires_at: float = 0.0
    refresh_expires_at: float = 0.0
    user_id: str = ""
    email: str = ""

    @property
    def is_logged_in(self) -> bool:
        return bool(self.access_token and self.refresh_token)


def platform_api_url() -> str:
    return os.environ.get("AERO_OFFICIAL_API_URL", DEFAULT_PLATFORM_API_URL).rstrip("/")


def relay_llm_url() -> str:
    return os.environ.get("AERO_OFFICIAL_LLM_URL", DEFAULT_RELAY_LLM_URL).rstrip("/")


def load_official_session() -> OfficialSessionData:
    data = load_user_secrets().get("official_account")
    if not isinstance(data, dict):
        return OfficialSessionData()
    return OfficialSessionData(
        access_token=str(data.get("access_token") or ""),
        refresh_token=str(data.get("refresh_token") or ""),
        access_expires_at=float(data.get("access_expires_at") or 0.0),
        refresh_expires_at=float(data.get("refresh_expires_at") or 0.0),
        user_id=str(data.get("user_id") or ""),
        email=str(data.get("email") or ""),
    )


def save_official_session(session: OfficialSessionData) -> None:
    secrets = load_user_secrets()
    secrets["official_account"] = {
        "access_token": session.access_token,
        "refresh_token": session.refresh_token,
        "access_expires_at": session.access_expires_at,
        "refresh_expires_at": session.refresh_expires_at,
        "user_id": session.user_id,
        "email": session.email,
    }
    save_user_secrets(secrets)


def clear_official_session() -> None:
    secrets = load_user_secrets()
    secrets.pop("official_account", None)
    save_user_secrets(secrets)


def _session_from_tokens(
    payload: dict[str, Any],
    previous: OfficialSessionData | None = None,
) -> OfficialSessionData:
    now = time.time()
    prior = previous or OfficialSessionData()
    access_token = str(payload.get("access_token") or "")
    refresh_token = str(payload.get("refresh_token") or "")
    if not access_token or not refresh_token:
        raise OfficialAccountError("官方账户服务返回了不完整的登录凭证。")
    return OfficialSessionData(
        access_token=access_token,
        refresh_token=refresh_token,
        access_expires_at=float(
            payload.get("expires_at") or now + float(payload.get("expires_in") or 0)
        ),
        refresh_expires_at=float(
            payload.get("refresh_expires_at")
            or now + float(payload.get("refresh_expires_in") or 0)
        ),
        user_id=str(payload.get("user_id") or prior.user_id),
        email=str(payload.get("email") or prior.email),
    )


def _error_message(response: httpx.Response, fallback: str) -> str:
    try:
        payload = response.json()
    except ValueError:
        return fallback
    if not isinstance(payload, dict):
        return fallback
    error = payload.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or payload.get("message") or fallback)
    detail = payload.get("detail")
    if isinstance(detail, str):
        return detail
    return str(payload.get("message") or fallback)


class OfficialAccountSession:
    """Shared JWT session for the platform, Relay LLM, and future cloud services."""

    def __init__(
        self,
        *,
        base_url: str | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = (base_url or platform_api_url()).rstrip("/")
        self._client = client or httpx.AsyncClient(timeout=30)
        self._owns_client = client is None
        self._refresh_lock = asyncio.Lock()
        self._session = load_official_session()
        self._refreshing = False

    @property
    def data(self) -> OfficialSessionData:
        self._session = load_official_session()
        return self._session

    @property
    def state(self) -> str:
        self._session = load_official_session()
        if self._refreshing:
            return "refreshing"
        if not self._session.is_logged_in:
            return "logged_out"
        if self._session.refresh_expires_at and self._session.refresh_expires_at <= time.time():
            return "login_required"
        return "logged_in"

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def login(self, email: str, password: str) -> OfficialSessionData:
        async with self._refresh_lock, account_lock():
            return await self._login(email, password)

    async def _login(self, email: str, password: str) -> OfficialSessionData:
        try:
            response = await self._client.post(
                f"{self.base_url}/v1/auth/login",
                json={"email": email.strip(), "password": password},
                headers={"Cache-Control": "no-store"},
            )
        except httpx.HTTPError as exc:
            raise OfficialAccountError("无法连接 Aerolytica 官方账户服务。") from exc
        if response.status_code >= 400:
            raise OfficialAccountError(_error_message(response, "邮箱或密码错误。"))
        self._session = _session_from_tokens(response.json())
        save_official_session(self._session)
        return self._session

    async def access_token(self, *, force_refresh: bool = False) -> str:
        session = self.data
        if not session.is_logged_in:
            raise OfficialLoginRequiredError("请先登录 Aerolytica 官方账户。")
        if (
            not force_refresh
            and session.access_expires_at > time.time() + _REFRESH_SKEW_SECONDS
        ):
            return session.access_token
        return (await self.refresh(force=force_refresh)).access_token

    async def refresh(self, *, force: bool = False) -> OfficialSessionData:
        observed_token = self._session.access_token
        async with self._refresh_lock, account_lock():
            current = self.data
            if (
                (not force or current.access_token != observed_token)
                and current.access_token
                and current.access_expires_at > time.time() + _REFRESH_SKEW_SECONDS
            ):
                return current
            if not current.refresh_token:
                raise OfficialLoginRequiredError("官方账户登录已失效，请重新登录。")
            if current.refresh_expires_at and current.refresh_expires_at <= time.time():
                clear_official_session()
                self._session = OfficialSessionData()
                raise OfficialLoginRequiredError("官方账户登录已过期，请重新登录。")
            self._refreshing = True
            try:
                response = await self._client.post(
                    f"{self.base_url}/v1/auth/refresh",
                    json={"refresh_token": current.refresh_token},
                    headers={"Cache-Control": "no-store"},
                )
            except httpx.HTTPError as exc:
                raise OfficialAccountError("刷新官方账户登录失败，请检查网络。") from exc
            finally:
                self._refreshing = False
            if response.status_code >= 400:
                if response.status_code not in {400, 401, 403}:
                    raise OfficialAccountError("账户服务暂不可用，登录凭证已保留，请稍后重试。")
                clear_official_session()
                self._session = OfficialSessionData()
                raise OfficialLoginRequiredError(
                    _error_message(response, "官方账户登录已失效，请重新登录。")
                )
            self._session = _session_from_tokens(response.json(), current)
            save_official_session(self._session)
            return self._session

    async def request(
        self,
        method: str,
        path: str,
        *,
        retry_unauthorized: bool = True,
        **kwargs: Any,
    ) -> httpx.Response:
        owner = self.data.user_id
        token = await self.access_token()
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Authorization"] = f"Bearer {token}"
        try:
            response = await self._client.request(
                method, f"{self.base_url}{path}", headers=headers, **kwargs
            )
        except httpx.HTTPError as exc:
            raise OfficialAccountError("无法连接 Aerolytica 官方账户服务。") from exc
        if response.status_code == 401 and retry_unauthorized:
            if owner and self.data.user_id != owner:
                raise OfficialLoginRequiredError("账户已切换，请重新打开当前云端操作。")
            token = await self.access_token(force_refresh=True)
            if owner and self.data.user_id != owner:
                raise OfficialLoginRequiredError("账户已切换，请重新打开当前云端操作。")
            headers["Authorization"] = f"Bearer {token}"
            try:
                response = await self._client.request(
                    method, f"{self.base_url}{path}", headers=headers, **kwargs
                )
            except httpx.HTTPError as exc:
                raise OfficialAccountError("无法连接 Aerolytica 官方账户服务。") from exc
        return response

    async def me(self) -> dict[str, Any]:
        response = await self.request("GET", "/v1/auth/me")
        if response.status_code >= 400:
            raise OfficialAccountError(_error_message(response, "无法读取官方账户信息。"))
        return response.json()

    async def credits(self) -> dict[str, Any]:
        response = await self.request("GET", "/v1/credits")
        if response.status_code >= 400:
            raise OfficialAccountError(_error_message(response, "无法读取账户额度。"))
        return response.json()

    async def logout(self) -> bool:
        async with self._refresh_lock, account_lock():
            return await self._logout()

    async def _logout(self) -> bool:
        refresh_token = self.data.refresh_token
        revoked = False
        try:
            if refresh_token:
                response = await self._client.post(
                    f"{self.base_url}/v1/auth/logout",
                    json={"refresh_token": refresh_token},
                    headers={"Cache-Control": "no-store"},
                )
                revoked = response.status_code < 400
        finally:
            clear_official_session()
            self._session = OfficialSessionData()
        return revoked


class CloudSyncClient:
    """Cloud-file API using the same official-account JWT session."""

    def __init__(
        self, session: OfficialAccountSession, transfer_client: httpx.AsyncClient | None = None
    ) -> None:
        self.session = session
        self._transfer_client = transfer_client

    async def request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        return await self.session.request(method, path, **kwargs)

    async def json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        response = await self.request(method, "/v1/storage" + path, **kwargs)
        if response.status_code >= 400:
            raise OfficialAccountError(_error_message(response, "云空间操作失败。"))
        return response.json()

    async def projects(self) -> list[dict[str, Any]]:
        return (await self.json("GET", "/projects"))["items"]

    async def directories(self, project_id: str) -> list[dict[str, Any]]:
        return (await self.json("GET", f"/projects/{project_id}/directories"))["items"]

    async def objects(self, project_id: str) -> dict[str, Any]:
        return await self.json("GET", "/objects", params={"project_id": project_id})

    @asynccontextmanager
    async def transfer(self):
        if self._transfer_client is not None:
            yield self._transfer_client
        else:
            async with httpx.AsyncClient(timeout=120) as client:
                yield client

    @staticmethod
    def check_url(url: str) -> str:
        if httpx.URL(url).scheme != "https":
            raise OfficialAccountError("云存储签名地址必须使用 HTTPS。")
        return url

    async def upload_file(
        self, path: str | os.PathLike[str], *, project_id: str | None = None,
        directory_id: str | None = None, filename: str | None = None,
        replaces: dict[str, Any] | None = None, exclusive: bool = False,
    ) -> dict[str, Any]:
        file_path = os.fspath(path)
        size_bytes = os.path.getsize(file_path)
        content_type = mimetypes.guess_type(filename or file_path)[0] or "application/octet-stream"
        response = await self.request(
            "POST",
            "/v1/storage/upload-url",
            json={
                "filename": filename or os.path.basename(file_path),
                "size_bytes": size_bytes,
                "content_type": content_type,
                "project_id": project_id,
                "directory_id": directory_id,
            },
        )
        if response.status_code >= 400:
            raise OfficialAccountError(_error_message(response, "获取云文件上传地址失败。"))
        payload = response.json()
        async def chunks():
            with open(file_path, "rb") as stream:
                while chunk := await asyncio.to_thread(stream.read, 1024 * 1024):
                    yield chunk

        try:
            async with self.transfer() as client:
                uploaded = await client.put(
                    self.check_url(payload["upload_url"]), content=chunks(),
                    headers={"Content-Type": content_type, "Content-Length": str(size_bytes)},
                )
                uploaded.raise_for_status()
        except (httpx.HTTPError, OSError, OfficialAccountError):
            with suppress(Exception):
                await self.json("POST", f"/{payload['object_id']}/fail")
            raise OfficialAccountError("云文件上传失败，未修改原有云端文件。") from None
        # Completion is separate from PUT. Never mark an ambiguous completion failed:
        # it may already have committed, and its signed key can be the active revision.
        completed = await self.json("POST", f"/{payload['object_id']}/complete", json={
            "replaces_object_id": replaces["object_id"] if replaces else None,
            "expected_updated_at": replaces["updated_at"] if replaces else None,
            "exclusive": exclusive,
        })
        return {**payload, **completed}

    async def download_file(self, object_id: str, destination: Path) -> None:
        url = self.check_url(await self.download_url(object_id))
        async with self.transfer() as client:
            async with client.stream("GET", url) as response:
                response.raise_for_status()
                with destination.open("wb") as handle:
                    async for chunk in response.aiter_bytes():
                        handle.write(chunk)

    async def download_url(self, object_id: str) -> str:
        response = await self.request(
            "POST", "/v1/storage/download-url", json={"object_id": object_id}
        )
        if response.status_code >= 400:
            raise OfficialAccountError(_error_message(response, "获取云文件下载地址失败。"))
        return str(response.json()["download_url"])

    async def delete(self, object_id: str, *, expected_updated_at: str | None = None) -> bool:
        response = await self.request("DELETE", f"/v1/storage/{object_id}",
                                      params={"expected_updated_at": expected_updated_at}
                                      if expected_updated_at else {})
        if response.status_code >= 400:
            raise OfficialAccountError(_error_message(response, "删除云文件失败。"))
        return True
