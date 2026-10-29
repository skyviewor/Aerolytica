"""Explicit, single-file official cloud transfers; not workspace synchronization."""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
import os
import re
import stat
import uuid
from contextlib import contextmanager, suppress
from pathlib import Path

import httpx

from aero.core.official_account import CloudSyncClient, OfficialAccountError
from aero.toolbox.paths import find_workspace_dir


def identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise ValueError("请使用云空间查询返回的有效 ID。")
    return value


class CloudFileError(OfficialAccountError):
    def __init__(self, status_code: int):
        self.status_code = status_code
        super().__init__(
            {
                401: "官方账户未登录或已过期，请使用 /login 登录。",
                403: "云空间订阅或目标权限不允许本次操作。",
                404: "云端文件或目录不存在，或服务端尚不支持云空间 v2，请重新查询。",
                409: "云端同名文件或版本冲突，不会覆盖；请重新查询并选择新的文件名或目录。",
                413: "云空间额度不足，未完成上传。",
                429: "云空间请求受限，请稍后查询状态。",
            }.get(status_code, "官方云空间服务暂不可用，请查询状态后再决定是否重试。")
        )


@contextmanager
def local_parent(file_path: str):
    """Pin each directory without following symlinks, including during transfers."""
    root = find_workspace_dir().resolve()
    path = Path(file_path)
    if not path.is_absolute():
        path = root / path
    try:
        parts = path.relative_to(root).parts
    except ValueError:
        raise ValueError("只允许传输当前工作区内的文件。") from None
    if not parts or any(
        p in {".", ".."}
        or p.startswith(".")
        or re.search(r"(?i)(secret|credential|password|token|\.pem$|\.key$)", p)
        or p.casefold() in {"aero.yaml", "aero.yml", "creds", "keys.json"}
        for p in parts
    ):
        raise ValueError("禁止传输隐藏文件、配置凭据或不安全路径。")
    fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        yield fd, parts[-1], path
    finally:
        os.close(fd)


def safe_fields(item: dict) -> dict:
    return {
        key: item[key]
        for key in (
            "file_id",
            "directory_id",
            "project_id",
            "project_name",
            "filename",
            "name",
            "relative_path",
            "path",
            "size_bytes",
            "version",
            "revision_id",
            "status",
        )
        if key in item
    }


class CloudFiles:
    def __init__(self, session):
        self.client = CloudSyncClient(session)

    async def api(self, method: str, path: str, **kwargs) -> dict:
        response = await self.client.request(method, "/v1/storage/v2" + path, **kwargs)
        if response.is_error:
            raise CloudFileError(response.status_code)
        return response.json()

    async def projects(self) -> list[dict]:
        return [
            {k: row[k] for k in ("project_id", "name", "is_default") if k in row}
            for row in await self.client.projects()
        ]

    async def quota(self) -> dict:
        """Account-wide capacity, using the server's upload admission accounting."""
        payload = await self.api("GET", "/quota")
        result = {}
        for key in ("limit_bytes", "used_bytes", "reserved_bytes"):
            value = payload.get(key)
            if type(value) is not int or value < 0:
                raise ValueError("云空间容量响应异常，无法确认剩余空间。")
            result[key] = value
        for key in (
            "history_bytes",
            "file_bytes",
            "agent_context_bytes",
            "agent_context_current_bytes",
            "agent_context_archive_bytes",
            "agent_context_staging_bytes",
            "agent_context_legacy_bytes",
            "base_limit_bytes",
            "expansion_bytes",
        ):
            if key in payload:
                value = payload[key]
                if type(value) is not int or value < 0:
                    raise ValueError("云空间容量响应异常，无法确认占用明细。")
                result[key] = value
        for key, default in (("enabled", False), ("read_only", False), ("over_quota", False)):
            value = payload.get(key, default)
            if type(value) is not bool:
                raise ValueError("云空间状态响应异常。")
            result[key] = value
        result["available_bytes"] = (
            max(result["limit_bytes"] - result["used_bytes"] - result["reserved_bytes"], 0)
            if result["enabled"] and not result["read_only"]
            else 0
        )
        result["scope"] = "account"
        result["accounting_note"] = (
            "已用空间包含普通文件及 Agent 上下文；预占为待完成上传。"
            "可上传剩余空间按总容量减已用和预占计算；只读或未开通时为零。"
            "history_bytes 单独展示，不在上述公式中重复扣除；结果为查询时快照。"
        )
        return result

    async def listing(self, project_id: str, cursor: str | None = None) -> dict:
        identifier(project_id)
        page = await self.api(
            "GET",
            "/snapshot",
            params={
                "project_id": project_id,
                "limit": 200,
                **({"cursor": cursor} if cursor else {}),
            },
        )
        return {
            "items": [
                {"kind": row["entity_type"], **safe_fields(row["data"])}
                for row in page["items"]
                if row["data"].get("status") == "active" and not row["data"].get("deleted_at")
            ],
            "next_cursor": page.get("next_cursor"),
            "has_more": page.get("has_more", False),
        }

    async def prepare_delete(self, project_id: str, files: list[dict]) -> dict:
        """Resolve a bounded, explicit manifest. Never accept a directory or wildcard."""
        identifier(project_id)
        if not isinstance(files, list) or not 1 <= len(files) <= 5:
            raise ValueError("每次只能审批删除 1–5 个明确的云文件，不支持目录或通配符。")
        wanted = {}
        for item in files:
            if not isinstance(item, dict):
                raise ValueError("删除清单必须包含文件 ID、版本、完整相对路径和大小。")
            file_id = identifier(item.get("file_id"))
            path = item.get("relative_path")
            if (
                file_id in wanted
                or not isinstance(path, str)
                or not 1 <= len(path) <= 255
                or any(not c.isprintable() for c in path)
                or "*" in path
            ):
                raise ValueError("删除清单路径无效、过长或包含重复文件。")
            for field in ("expected_version", "size_bytes"):
                if type(item.get(field)) is not int or item[field] < 0:
                    raise ValueError("删除清单必须使用查询返回的版本和大小。")
            wanted[file_id] = item
        found, cursor, seen = {}, None, set()
        for _ in range(100):
            page = await self.listing(project_id, cursor)
            for row in page["items"]:
                file_id = row.get("file_id")
                if row.get("kind") == "file" and file_id in wanted:
                    expected = wanted[file_id]
                    if (
                        row.get("project_id") != project_id
                        or row.get("version") != expected["expected_version"]
                        or row.get("relative_path") != expected["relative_path"]
                        or row.get("size_bytes") != expected["size_bytes"]
                    ):
                        raise ValueError("文件路径、大小或版本已变化，请重新查询并重新审批。")
                    found[file_id] = {
                        "file_id": file_id,
                        "relative_path": row["relative_path"],
                        "expected_version": row["version"],
                        "size_bytes": row["size_bytes"],
                    }
            if len(found) == len(wanted):
                return {"project_id": project_id, "files": [found[k] for k in wanted]}
            if not page["has_more"]:
                break
            cursor = page.get("next_cursor")
            if not cursor or cursor in seen:
                raise ValueError("云文件列表分页异常，未执行删除。")
            seen.add(cursor)
        raise ValueError("未能完整核对删除清单，未执行删除；请重新查询文件。")

    async def delete(self, project_id: str, files: list[dict]) -> dict:
        # Recheck after approval, and let the server CAS each approved version.
        manifest = await self.prepare_delete(project_id, files)
        deleted = []
        for index, item in enumerate(manifest["files"]):
            try:
                response = await self.api(
                    "DELETE",
                    f"/files/{identifier(item['file_id'])}",
                    params={"expected_version": item["expected_version"]},
                )
                if response.get("status") != "deleted":
                    raise ValueError("服务端未确认删除结果。")
                deleted.append(item)
            except (CloudFileError, httpx.HTTPError, ValueError):
                return {
                    "status": "partial" if deleted else "error",
                    "deleted": deleted,
                    "unconfirmed": item,
                    "not_attempted": manifest["files"][index + 1 :],
                    "message": "已停止后续删除。当前文件未确认成功，请重新查询；不得自动重试。",
                }
        return {
            "status": "success",
            "deleted": deleted,
            "message": (
                "这些云文件已标记删除，未直接改动本地文件、目录或对话。"
                "若已开启双向同步，后续同步可能将云端删除反映到本地。"
            ),
        }

    async def upload(self, file_path: str, project_id: str, directory_id: str | None) -> dict:
        identifier(project_id)
        if directory_id:
            identifier(directory_id)
        with local_parent(file_path) as (parent, name, _):
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
            with os.fdopen(fd, "rb") as source:
                before = os.fstat(source.fileno())
                if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                    raise ValueError("只允许上传普通文件，不支持符号链接或硬链接。")
                mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
                # Keep common pasted credentials out of text/report uploads.
                tail = b""
                while sample := await asyncio.to_thread(source.read, 1024 * 1024):
                    scanned = tail + sample
                    if re.search(
                        rb"-----BEGIN [^-]*PRIVATE KEY|\bsk-[A-Za-z0-9_-]{16,}|"
                        rb"(?im:^[^\n]*(?:api[_-]?key|password|secret)\s*[:=]\s*\S+)",
                        scanned,
                    ):
                        raise ValueError("文件疑似包含凭据，请先移除敏感内容再上传。")
                    tail = scanned[-4096:]
                source.seek(0)
                payload = await self.api(
                    "POST",
                    "/uploads",
                    json={
                        "project_id": project_id,
                        "directory_id": directory_id,
                        "filename": name,
                        "size_bytes": before.st_size,
                        "content_type": mime,
                        "expected_version": 0,
                    },
                )
                upload_id = identifier(payload["upload_id"])

                async def chunks():
                    while chunk := await asyncio.to_thread(source.read, 1024 * 1024):
                        yield chunk

                try:
                    async with self.client.transfer() as transfer:
                        response = await transfer.put(
                            self.client.check_url(payload["upload_url"]),
                            content=chunks(),
                            headers={"Content-Type": mime, "Content-Length": str(before.st_size)},
                        )
                        response.raise_for_status()
                    after = os.fstat(source.fileno())
                    if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                        after.st_size,
                        after.st_mtime_ns,
                        after.st_ctime_ns,
                    ):
                        raise ValueError("上传期间本地文件发生变化，未提交云端文件。")
                except BaseException:
                    with suppress(Exception):
                        await self.api("POST", f"/uploads/{upload_id}/fail")
                    raise
                # Ambiguous completion must never be marked failed or auto-retried.
                result = await self.api(
                    "POST", f"/uploads/{upload_id}/complete", json={"expected_version": 0}
                )
                return {
                    "filename": name,
                    "project_id": project_id,
                    "directory_id": directory_id,
                    "size_bytes": before.st_size,
                    "file_id": payload["file_id"],
                    **safe_fields(result),
                }

    async def download(self, file_id: str, file_path: str) -> dict:
        # The revision endpoint supplies authoritative size/hash for complete-transfer checks.
        file_id = identifier(file_id)
        revisions = await self.api("GET", f"/files/{file_id}/revisions")
        revision = next((r for r in revisions["items"] if r.get("is_current")), None)
        if revision is None:
            raise OfficialAccountError("云端文件没有可下载的当前版本。")
        with local_parent(file_path) as (parent, name, path):
            try:
                os.stat(name, dir_fd=parent, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ValueError("本地目标已存在，不会覆盖；请选择新的文件名。")
            payload = await self.api(
                "POST",
                f"/files/{file_id}/download-url",
                json={"revision_id": revision["revision_id"]},
            )
            temp = f".aero-download-{uuid.uuid4().hex}"
            size, digest = 0, hashlib.md5(usedforsecurity=False)
            try:
                fd = os.open(
                    temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent
                )
                with os.fdopen(fd, "wb") as target:
                    async with self.client.transfer() as transfer:
                        async with transfer.stream(
                            "GET", self.client.check_url(payload["download_url"])
                        ) as response:
                            response.raise_for_status()
                            async for chunk in response.aiter_bytes(1024 * 1024):
                                size += len(chunk)
                                if size > revision["size_bytes"]:
                                    raise ValueError("下载内容超过声明大小。")
                                digest.update(chunk)
                                target.write(chunk)
                    target.flush()
                    os.fsync(target.fileno())
                if size != revision["size_bytes"]:
                    raise ValueError("下载内容不完整，未保存目标文件。")
                etag = str(revision.get("etag") or "").strip('"').lower()
                if re.fullmatch(r"[0-9a-f]{32}", etag) and digest.hexdigest() != etag:
                    raise ValueError("下载校验失败，未保存目标文件。")
                # Atomic no-clobber publication, even if another writer appeared meanwhile.
                os.link(temp, name, src_dir_fd=parent, dst_dir_fd=parent, follow_symlinks=False)
            finally:
                with suppress(FileNotFoundError):
                    os.unlink(temp, dir_fd=parent)
        return {"file_id": file_id, "file_path": str(path), "size_bytes": size}
