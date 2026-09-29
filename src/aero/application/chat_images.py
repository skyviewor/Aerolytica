"""Publish referenced workspace images directly to signed private OSS URLs."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

from aero.cli.image_widget import extract_image_paths
from aero.core.daemon import private_directory, private_write

FORMATS = {"PNG": "image/png", "JPEG": "image/jpeg", "GIF": "image/gif", "WEBP": "image/webp"}


def describe_images(content: str, event_id: str) -> list[dict[str, str]]:
    sources = dict.fromkeys(extract_image_paths(content))
    return [{"attachment_id": hashlib.sha256(f"{event_id}:{source}".encode()).hexdigest()[:32],
             "source": source} for source in list(sources)[:32]
            if Path(source).suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".webp"}]


def workspace_roots(project_dir: Path) -> list[Path]:
    from aero.experiments import ExperimentManager

    manager = ExperimentManager(project_dir)
    experiment = manager.active()
    workspace = manager.workspace_path(experiment) if experiment else project_dir
    return [workspace, workspace / "data", project_dir, project_dir / "data"]


def open_image(source: str, roots: list[Path]):
    """Open only regular images within approved roots, including symlink resolution."""
    roots = [root.resolve() for root in roots]
    path = Path(source).expanduser()
    for candidate in ([path] if path.is_absolute() else [root / path for root in roots]):
        resolved = candidate.resolve()
        if not any(resolved.is_relative_to(root) for root in roots) or not resolved.is_file():
            continue
        descriptor = os.open(resolved, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        handle = os.fdopen(descriptor, "rb")
        try:
            with (
                os.fdopen(os.dup(handle.fileno()), "rb") as inspection,
                Image.open(inspection) as image,
            ):
                content_type = FORMATS.get(image.format or "")
                if content_type is None:
                    raise ValueError("只支持 PNG、JPEG、GIF 和 WebP 图片")
                image.verify()
            handle.seek(0)
            return handle, content_type, os.fstat(handle.fileno()).st_size
        except BaseException:
            handle.close()
            raise
    raise ValueError("图片文件不存在或不在当前项目目录内")


class ImagePublisher:
    def __init__(self, request: Callable[..., Awaitable[dict[str, Any]]],
                 roots: Callable[[], list[Path]], journal: Path):
        self.request = request
        self.roots = roots
        self.journal = journal
        self.lock = asyncio.Lock()
        try:
            self.locations = json.loads(journal.read_text()) if journal.exists() else {}
        except (OSError, ValueError):
            self.locations = {}

    def describe(self, content: str, event_id: str) -> list[dict[str, str]]:
        result = describe_images(content, event_id)
        if result:
            roots = [str(path.resolve()) for path in self.roots()]
            for item in result:
                identifier = item["attachment_id"]
                if identifier in self.locations:
                    continue
                location = {"roots": roots}
                # Freeze the local bytes too: later runs may overwrite the same filename.
                handle = None
                snapshot = None
                created = False
                try:
                    handle, _, _ = open_image(item["source"], [Path(path) for path in roots])
                    directory = private_directory(self.journal.parent / "image-files")
                    snapshot = directory / f"{identifier}{Path(item['source']).suffix.lower()}"
                    descriptor = os.open(snapshot, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    created = True
                    with os.fdopen(descriptor, "wb") as output:
                        shutil.copyfileobj(handle, output)
                    location["snapshot"] = str(snapshot)
                except (OSError, ValueError):
                    if created and snapshot is not None and snapshot.exists():
                        snapshot.unlink()
                    elif snapshot is not None and snapshot.is_file() and not snapshot.is_symlink():
                        location["snapshot"] = str(snapshot)
                finally:
                    if handle is not None:
                        handle.close()
                self.locations[identifier] = location
            private_write(self.journal, json.dumps(self.locations, ensure_ascii=False))
        return result

    async def flush(self) -> None:
        if self.lock.locked():
            return
        async with self.lock:
            result = await self.request("GET", "images/pending")
            for item in result.get("items", [])[:4]:
                await self._upload(item)

    async def run(self) -> None:
        while True:
            try:
                await self.flush()
            except (httpx.HTTPError, OSError, ValueError, RuntimeError):
                # Connectivity and ownership are handled by the presence/runtime task.
                pass
            await asyncio.sleep(5)

    async def _upload(self, item: dict) -> None:
        prefix = f"messages/{item['message_id']}/images/{item['attachment_id']}"
        location = self.locations.get(item["attachment_id"], {})
        roots = [Path(path) for path in location.get("roots", [])]
        roots = roots or self.roots()
        source = item["source"]
        if location.get("snapshot"):
            source = location["snapshot"]
            roots = [self.journal.parent / "image-files"]
        handle = None
        try:
            handle, content_type, size = await asyncio.to_thread(open_image, source, roots)
            upload = await self.request("POST", f"{prefix}/upload", json={
                "size_bytes": size, "content_type": content_type,
            })
            if upload.get("completed"):
                self._release_snapshot(item["attachment_id"])
                return
            url = httpx.URL(upload["upload_url"])
            if url.scheme != "https" or url.userinfo:
                raise ValueError("云存储上传地址必须使用 HTTPS")

            async def chunks():
                while chunk := await asyncio.to_thread(handle.read, 256 * 1024):
                    yield chunk

            # A separate client ensures no Relay authorization/cookies reach OSS.
            async with httpx.AsyncClient(timeout=60, follow_redirects=False) as http:
                response = await http.put(url, content=chunks(), headers={
                    "Content-Type": content_type, "Content-Length": str(size),
                })
                response.raise_for_status()
            await self.request("POST", f"{prefix}/complete", json={})
            self._release_snapshot(item["attachment_id"])
        except (httpx.HTTPError, OSError, ValueError, RuntimeError) as exc:
            reason = "图片上传失败，请检查连接后重试"
            if isinstance(exc, (ValueError, OSError)):
                reason = str(exc) if isinstance(exc, ValueError) else "无法读取本地图片文件"
            elif isinstance(exc, httpx.HTTPStatusError):
                if exc.response.status_code == 503:
                    try:
                        detail = exc.response.json().get("detail", {})
                    except ValueError:
                        detail = {}
                    if isinstance(detail, dict) and detail.get("code") == "image_busy":
                        return
                if exc.response.status_code == 413:
                    reason = "云空间额度不足，请释放空间后重试"
                elif exc.response.status_code == 403:
                    reason = "云空间当前不可写，请检查会员状态后重试"
                elif exc.response.status_code == 400:
                    reason = "图片格式校验失败"
            try:
                await self.request("POST", f"{prefix}/fail", json={"error": reason})
            except (httpx.HTTPError, RuntimeError):
                pass
        finally:
            if handle is not None:
                handle.close()

    def _release_snapshot(self, identifier: str) -> None:
        location = self.locations.get(identifier, {})
        snapshot = location.get("snapshot")
        if snapshot:
            path = Path(snapshot)
            if path.resolve().parent == (self.journal.parent / "image-files").resolve():
                path.unlink(missing_ok=True)
            location.pop("snapshot", None)
            private_write(self.journal, json.dumps(self.locations, ensure_ascii=False))
