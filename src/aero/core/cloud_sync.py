"""Conservative, restartable workspace synchronization against Relay storage v2.

No background task is started implicitly. The caller owns the execution/busy guard
and the lifetime of ``run``. Only the four research-document trees are eligible.
"""

from __future__ import annotations

import asyncio
import copy
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

from aero.core.official_account import CloudSyncClient, OfficialAccountSession

ALLOWED_ROOTS = frozenset({"papers", "literature", "scripts", "plans"})
EXCLUDED = frozenset({"data", "tmp", "creds", "credentials", "internal", "__pycache__"})
CHUNK_SIZE = 1024 * 1024


class CloudSyncError(RuntimeError):
    """A sync operation could not safely proceed."""


class SyncConflictError(CloudSyncError):
    """Neither side may be overwritten automatically."""


class SyncDeferredError(CloudSyncError):
    """The caller is busy, paused, or the account binding changed."""


SyncConflict = SyncConflictError
SyncDeferred = SyncDeferredError


def _id(value: str) -> str:
    return quote(str(value), safe="")


def _eligible(path: str) -> bool:
    parts = PurePosixPath(path).parts
    return bool(
        parts
        and not path.startswith("/")
        and "\\" not in path
        and "/".join(parts) == path
        and all(
            p not in {".", ".."}
            and not p.startswith(".")
            and p.casefold() not in EXCLUDED
            and not re.search(r"(?i)(secret|credential|token|password|\.pem$|\.key$)", p)
            for p in parts
        )
    )


def _deleted(record: dict | None) -> bool:
    return record is None or bool(record.get("deleted_at")) or record.get("status") == "deleted"


def _same(a: dict | None, b: dict | None) -> bool:
    if a is None or b is None:
        return a is b
    return a["kind"] == b["kind"] and a.get("sha256") == b.get("sha256")


class CloudSyncEngine:
    """Bind a local workspace to one account/project/cloud directory.

    Public methods return JSON-compatible status. ``busy`` must synchronously
    reflect project execution; callers must also serialize starting execution
    with local sync writes. Network waits always recheck this guard.
    """

    def __init__(
        self,
        root: Path,
        session: OfficialAccountSession,
        *,
        busy: Callable[[], bool] | None = None,
    ) -> None:
        self.root = Path(os.path.abspath(root))
        if self.root.resolve() != self.root or not self.root.is_dir():
            raise CloudSyncError("Workspace must be an existing, non-symlink directory")
        self.session = session
        self.client = CloudSyncClient(session)
        self.busy = busy or (lambda: False)
        self._mutex = asyncio.Lock()
        self._state_dir = self.root / ".aero" / "cloud-sync"
        for directory in (self.root / ".aero", self._state_dir):
            if directory.is_symlink():
                raise CloudSyncError("Sync metadata must not be a symlink")
            directory.mkdir(mode=0o700, exist_ok=True)
        self._state: dict[str, Any] = self._load()
        self._paused = bool(self._state.get("paused", False))
        self._last_error: str | None = None
        self._failures = 0

    def _check_metadata(self) -> None:
        if self.root.resolve() != self.root or self._state_dir.resolve() != self._state_dir:
            raise CloudSyncError("Workspace or sync metadata path changed")

    def _load(self) -> dict:
        self._check_metadata()
        path = self._state_dir / "state.json"
        if path.is_symlink():
            raise CloudSyncError("Sync state must not be a symlink")
        if not path.exists():
            return {
                "schema": 1,
                "binding": None,
                "baseline": {},
                "conflicts": {},
                "pending": {},
                "remote_entities": {},
                "remote_cursor": None,
            }
        state = json.loads(path.read_text())
        if state.get("schema") != 1:
            raise CloudSyncError("Unsupported sync state schema")
        return state

    def _save(self) -> None:
        self._check_metadata()
        self._state["paused"] = self._paused
        fd, name = tempfile.mkstemp(dir=self._state_dir, prefix="state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as stream:
                json.dump(self._state, stream, sort_keys=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self._state_dir / "state.json")
            self._fsync_dir(self._state_dir)
        finally:
            Path(name).unlink(missing_ok=True)

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @contextmanager
    def _locked(self):
        self._check_metadata()
        fd = os.open(self._state_dir / "lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise SyncDeferred("Another engine is syncing this workspace") from exc
            self._state = self._load()
            yield
        finally:
            os.close(fd)

    def _guard(self) -> None:
        if self._paused or self.busy():
            raise SyncDeferred("Sync paused or project execution is busy")
        binding = self._state["binding"]
        if not binding:
            raise SyncDeferred("Workspace is not bound")
        if (
            str(self.session.data.user_id) != binding["user_id"]
            or self.session.base_url != binding["base_url"]
            or str(self.root) != binding["local_root"]
        ):
            raise SyncDeferred("Account, API origin, or workspace no longer matches binding")
        self._check_metadata()

    def _eligible_path(self, path: str) -> bool:
        """Apply the persisted user scope on top of the mandatory safety rules."""
        if not _eligible(path):
            return False
        binding = self._state.get("binding") or {}
        includes = binding.get("includes") or sorted(ALLOWED_ROOTS)
        normalized = [str(item).strip("/") for item in includes if str(item).strip("/")]
        return any(
            path == item.removesuffix("/**")
            or path.startswith(item.removesuffix("/**") + "/")
            for item in normalized
        )

    def _configured_roots(self) -> set[str]:
        binding = self._state.get("binding") or {}
        includes = binding.get("includes") or sorted(ALLOWED_ROOTS)
        return {
            str(item).strip("/").split("/", 1)[0]
            for item in includes
            if str(item).strip("/")
        }

    @staticmethod
    def _validate_includes(includes: list[str] | None) -> list[str]:
        values = list(includes or sorted(ALLOWED_ROOTS))
        if not values:
            raise CloudSyncError("At least one sync path is required")
        result: list[str] = []
        for raw in values:
            raw_value = str(raw).strip()
            if "*" in raw_value and not raw_value.endswith("/**"):
                raise CloudSyncError(f"Invalid sync path: {raw}")
            value = raw_value.strip("/")
            if value.endswith("/**"):
                value = value[:-3]
            if not value or not _eligible(value):
                raise CloudSyncError(f"Invalid sync path: {raw}")
            if value not in result:
                result.append(value + "/**" if str(raw).strip().endswith("/**") else value)
        return result

    def status(self) -> dict[str, Any]:
        binding = copy.deepcopy(self._state["binding"])
        enabled = bool(binding) and not self._paused
        return {
            "binding": binding,
            "enabled": enabled,
            "state": "paused" if binding and self._paused else ("bound" if binding else "unbound"),
            "project_id": binding.get("project_id") if binding else None,
            "directory_id": binding.get("root_directory_id") if binding else None,
            "includes": binding.get("includes", []) if binding else [],
            "last_sync_at": self._state.get("last_sync_at"),
            "paused": self._paused,
            "busy": bool(self.busy()),
            "running": self._mutex.locked(),
            "conflicts": copy.deepcopy(self._state["conflicts"]),
            "pending_uploads": len(self._state["pending"]),
            "tracked_paths": len(self._state["baseline"]),
            "last_error": self._last_error,
            "retry_seconds": min(30 * 2 ** min(self._failures, 5), 900),
        }

    async def bind(
        self, user_id: str, project_id: str, root_directory_id: str | None = None,
        includes: list[str] | None = None,
    ) -> dict[str, Any]:
        async with self._mutex:
            with self._locked():
                binding = {
                    "user_id": str(user_id),
                    "project_id": str(project_id),
                    "root_directory_id": root_directory_id,
                    "local_root": str(self.root),
                    "base_url": self.session.base_url,
                    "includes": self._validate_includes(includes),
                }
                if self._state["binding"] and self._state["binding"] != binding:
                    raise CloudSyncError("Already bound; use another workspace for another scope")
                if str(self.session.data.user_id) != str(user_id):
                    raise CloudSyncError("Binding user does not match the authenticated account")
                if self._state.get("binding") != binding:
                    self._state["remote_entities"] = {}
                    self._state["remote_cursor"] = None
                self._state["binding"] = binding
                capabilities = await self._api("GET", "/capabilities")
                if capabilities.get("version") != 2:
                    raise CloudSyncError("Relay storage protocol v2 is required for sync")
                # Validate scope without creating or changing anything in the cloud.
                await self._remote()
                self._save()
        return self.status()

    def pause(self, paused: bool = True) -> dict[str, Any]:
        self._paused = paused
        if not self._mutex.locked():
            with self._locked():
                self._save()
        return self.status()

    async def _api(self, method: str, path: str, **kwargs: Any) -> dict:
        self._guard()
        response = await self.session.request(method, "/v1/storage/v2" + path, **kwargs)
        self._guard()
        if response.status_code == 409:
            raise SyncConflict("Cloud version or path changed (409)")
        if response.status_code >= 400:
            # Never persist error payloads, which can contain signed URLs or credentials.
            raise CloudSyncError(f"Cloud storage request failed ({response.status_code})")
        return response.json()

    @staticmethod
    def _entity_key(item: dict) -> str:
        return f"{item['entity_type']}:{item['entity_id']}"

    def _remote_from_items(self, items: list[dict]) -> dict[str, dict]:
        binding = self._state["binding"]
        directories = {
            str(i["data"]["directory_id"]): i["data"]
            for i in items if i["entity_type"] == "directory"
        }
        scope = binding["root_directory_id"]
        if scope and (scope not in directories or _deleted(directories[scope])):
            raise CloudSyncError("Bound cloud root is missing or deleted; rebind explicitly")

        def relative(directory_id: str | None, visited: frozenset = frozenset()) -> str | None:
            if directory_id == scope:
                return ""
            if directory_id is None or directory_id in visited:
                return None
            row = directories.get(directory_id)
            if not row or str(row["project_id"]) != binding["project_id"]:
                return None
            parent = relative(row.get("parent_directory_id"), visited | {directory_id})
            if parent is None or "/" in row["name"] or "\\" in row["name"]:
                return None
            return "/".join(filter(None, (parent, row["name"])))

        result: dict[str, dict] = {}
        folded: dict[str, str] = {}
        for item in items:
            row = item["data"]
            if str(row["project_id"]) != binding["project_id"]:
                raise CloudSyncError("Cloud snapshot escaped project scope")
            kind = "dir" if item["entity_type"] == "directory" else "file"
            parent = relative(row.get("directory_id"))
            path = parent if kind == "dir" else (
                "/".join(filter(None, (parent, row["filename"]))) if parent is not None else None
            )
            if kind == "file" and ("/" in row["filename"] or "\\" in row["filename"]):
                raise CloudSyncError("Invalid cloud filename")
            if not path or not self._eligible_path(path):
                continue
            record = {**row, "kind": kind}
            previous = result.get(path)
            # A restored name may also have an older tombstone with a different ID.
            if previous and not _deleted(previous) and not _deleted(record):
                raise CloudSyncError("Ambiguous cloud path")
            if previous and not _deleted(previous):
                continue
            folded_path = folded.get(path.casefold())
            if folded_path and folded_path != path:
                raise CloudSyncError("Case-colliding cloud paths")
            folded[path.casefold()] = path
            result[path] = record
        return result

    async def _remote_snapshot(self) -> dict[str, dict]:
        binding = self._state["binding"]
        items: list[dict] = []
        cursor = None
        seen: set[str] = set()
        checkpoint = None
        while True:
            params = {"project_id": binding["project_id"], "limit": 200}
            if cursor:
                params["cursor"] = cursor
            page = await self._api("GET", "/snapshot", params=params)
            items.extend(page["items"])
            checkpoint = page.get("cursor") or checkpoint
            cursor = page.get("next_cursor")
            if not cursor:
                if page.get("has_more"):
                    raise CloudSyncError("Incomplete cloud snapshot")
                break
            if cursor in seen:
                raise CloudSyncError("Repeated cloud snapshot cursor")
            seen.add(cursor)
        self._state["remote_entities"] = {
            self._entity_key(item): item["data"] for item in items
        }
        self._state["remote_cursor"] = checkpoint
        return self._remote_from_items(items)

    async def _remote_changes(self) -> dict[str, dict]:
        binding = self._state["binding"]
        entities = dict(self._state.get("remote_entities") or {})
        cursor = self._state.get("remote_cursor")
        seen: set[str] = set()
        while True:
            page = await self._api("GET", "/changes", params={
                "project_id": binding["project_id"], "limit": 200, "cursor": cursor,
            })
            for item in page["items"]:
                entities[self._entity_key(item)] = item["data"]
            next_cursor = page.get("next_cursor") or page.get("cursor")
            if not page.get("has_more"):
                cursor = next_cursor
                break
            if not next_cursor or next_cursor in seen:
                raise CloudSyncError("Repeated cloud changes cursor")
            seen.add(next_cursor)
            cursor = next_cursor
        self._state["remote_entities"] = entities
        self._state["remote_cursor"] = cursor
        return self._remote_from_items([
            {"entity_type": key.split(":", 1)[0],
             "entity_id": key.split(":", 1)[1], "operation": "upsert", "data": data}
            for key, data in entities.items()
        ])

    async def _remote(self) -> dict[str, dict]:
        if self._state.get("remote_cursor") and isinstance(
            self._state.get("remote_entities"), dict
        ):
            return await self._remote_changes()
        return await self._remote_snapshot()

    def _path(self, path: str) -> Path:
        if not self._eligible_path(path):
            raise CloudSyncError("Path is outside the sync allowlist")
        current = self.root
        for part in PurePosixPath(path).parts:
            current = current / part
            if current.is_symlink():
                raise SyncConflict("Symlinks are never synchronized")
        if self.root.resolve() != self.root:
            raise SyncConflict("Workspace path changed")
        return current

    def _fingerprint(self, path: str) -> dict | None:
        target = self._path(path)
        try:
            info = target.lstat()
        except FileNotFoundError:
            return None
        if stat.S_ISDIR(info.st_mode):
            return {"kind": "dir"}
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise SyncConflict("Only regular, non-hardlinked files may be synchronized")
        sha = hashlib.sha256()
        md5 = hashlib.md5(usedforsecurity=False)
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            before = os.fstat(stream.fileno())
            if (before.st_dev, before.st_ino) != (info.st_dev, info.st_ino):
                raise SyncConflict("Local file changed during scan")
            while block := stream.read(CHUNK_SIZE):
                sha.update(block)
                md5.update(block)
            after = os.fstat(stream.fileno())
        if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ):
            raise SyncConflict("Local file changed during scan")
        return {"kind": "file", "sha256": sha.hexdigest(), "md5": md5.hexdigest(),
                "size": after.st_size}

    def _local(self) -> dict[str, dict]:
        result = {}
        for top in sorted(self._configured_roots()):
            target = self.root / top
            if not target.exists() and not target.is_symlink():
                continue
            for directory, dirs, files in os.walk(target, followlinks=False):
                rel = Path(directory).relative_to(self.root).as_posix()
                if not self._eligible_path(rel) or Path(directory).is_symlink():
                    dirs[:] = []
                    continue
                result[rel] = {"kind": "dir"}
                dirs[:] = [d for d in dirs if self._eligible_path(f"{rel}/{d}")
                           and not (Path(directory) / d).is_symlink()]
                for name in files:
                    path = f"{rel}/{name}"
                    if self._eligible_path(path):
                        try:
                            value = self._fingerprint(path)
                            if value:
                                result[path] = value
                        except SyncConflict as exc:
                            self._conflict(path, str(exc))
        folded: set[str] = set()
        for path in result:
            if path.casefold() in folded:
                raise CloudSyncError("Case-colliding local paths")
            folded.add(path.casefold())
        return result

    def _conflict(self, path: str, reason: str) -> None:
        self._state["conflicts"][path] = {"path": path, "reason": reason}
        self._save()

    def _baseline(self, path: str, local: dict | None, remote: dict | None) -> None:
        self._state["baseline"][path] = {"local": local, "remote": remote}
        self._state["conflicts"].pop(path, None)
        self._save()

    @staticmethod
    def _matches(local: dict | None, remote: dict | None) -> bool:
        if local is None or _deleted(remote):
            return local is None and _deleted(remote)
        if local["kind"] != remote["kind"]:
            return False
        if local["kind"] == "dir":
            return True
        etag = str(remote.get("etag", "")).strip('"').lower()
        return local["size"] == remote["size_bytes"] and (
            local["md5"] == etag or local["sha256"] == remote.get("sha256")
        )

    async def sync_once(self) -> dict[str, Any]:
        async with self._mutex:
            with self._locked():
                try:
                    self._guard()
                    await self._recover()
                    remote = await self._remote()
                    local = self._local()
                    paths = set(local) | set(remote) | set(self._state["baseline"])
                    # Parent creations first; deletions after children.
                    ordered = sorted(paths, key=lambda p: (p.count("/"), p))
                    deletes = [p for p in ordered if p not in local or _deleted(remote.get(p))]
                    normal = [p for p in ordered if p not in deletes]
                    # New local-only directories must precede their files.
                    creates = [p for p in deletes if p in local and p not in remote]
                    ordered = sorted(normal + creates, key=lambda p: (p.count("/"), p))
                    ordered += sorted(set(deletes) - set(creates),
                                      key=lambda p: (-p.count("/"), p))
                    for path in ordered:
                        self._guard()
                        if path in self._state["conflicts"] or not self._eligible_path(path):
                            continue
                        try:
                            await self._reconcile(path, remote)
                        except SyncConflict as exc:
                            self._conflict(path, str(exc))
                    self._last_error = None
                    self._failures = 0
                    self._state["last_sync_at"] = dt.datetime.now(dt.timezone.utc).isoformat()
                except SyncDeferred as exc:
                    self._last_error = str(exc)
                except Exception:
                    self._failures += 1
                    self._last_error = "Sync failed; pending operations retained for retry"
                    raise
                finally:
                    self._save()
        return self.status()

    async def _reconcile(self, path: str, remote: dict[str, dict]) -> None:
        local = self._fingerprint(path)
        cloud = remote.get(path)
        base = self._state["baseline"].get(path)
        if self._matches(local, cloud):
            self._baseline(path, local, cloud)
            return
        if base is None:
            if local is None and not _deleted(cloud):
                await self._pull(path, cloud, local)
            elif local is not None and cloud is None:
                await self._push(path, local, cloud, remote)
            else:
                raise SyncConflict("Initial merge differs; no initial deletion or overwrite")
            return
        local_changed = not _same(local, base["local"])
        previous = base["remote"]
        cloud_changed = (
            (cloud or {}).get("version") != (previous or {}).get("version")
            or (cloud or {}).get("file_id", (cloud or {}).get("directory_id"))
            != (previous or {}).get("file_id", (previous or {}).get("directory_id"))
        )
        if local_changed and cloud_changed:
            raise SyncConflict("Both local and cloud changed")
        if local_changed:
            await self._push(path, local, cloud, remote)
        elif cloud_changed:
            if cloud is None:
                raise SyncConflict("Tracked cloud path disappeared without a tombstone")
            await self._pull(path, cloud, local)

    def _unchanged(self, path: str, expected: dict | None) -> None:
        self._guard()
        if not _same(self._fingerprint(path), expected):
            raise SyncConflict("Local content changed while sync was in flight")

    def _trash(self, path: str, expected: dict | None) -> None:
        self._unchanged(path, expected)
        if expected is None:
            return
        source = self._path(path)
        if expected["kind"] == "dir" and any(source.iterdir()):
            raise SyncConflict("Directory contains local or excluded children; preserved")
        trash = self._state_dir / "trash"
        if trash.is_symlink():
            raise SyncConflict("Invalid trash directory")
        trash.mkdir(mode=0o700, exist_ok=True)
        destination = trash / (uuid.uuid4().hex + "-" + source.name)
        os.replace(source, destination)
        self._fsync_dir(source.parent)
        self._fsync_dir(trash)

    async def _pull(self, path: str, cloud: dict, expected: dict | None) -> None:
        if _deleted(cloud):
            self._trash(path, expected)
            self._baseline(path, None, cloud)
            return
        if cloud["kind"] == "dir":
            self._unchanged(path, expected)
            if expected is not None and expected["kind"] != "dir":
                raise SyncConflict("File/directory type conflict")
            self._path(path).mkdir(exist_ok=True)
            self._baseline(path, {"kind": "dir"}, cloud)
            return
        if expected and expected["kind"] != "file":
            raise SyncConflict("File/directory type conflict")
        payload = await self._api("POST", f"/files/{_id(cloud['file_id'])}/download-url",
                                  json={"revision_id": cloud["revision_id"]})
        fd, name = tempfile.mkstemp(dir=self._state_dir, prefix="download-")
        sha = hashlib.sha256()
        md5 = hashlib.md5(usedforsecurity=False)
        size = 0
        try:
            with os.fdopen(fd, "wb") as stream:
                async with self.client.transfer() as client:
                    download_url = self.client.check_url(payload["download_url"])
                    async with client.stream("GET", download_url) as response:
                        response.raise_for_status()
                        async for chunk in response.aiter_bytes(CHUNK_SIZE):
                            self._guard()
                            size += len(chunk)
                            if size > cloud["size_bytes"]:
                                raise SyncConflict("Download exceeded declared size")
                            stream.write(chunk)
                            sha.update(chunk)
                            md5.update(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            local = {"kind": "file", "sha256": sha.hexdigest(), "md5": md5.hexdigest(),
                     "size": size}
            if not self._matches(local, cloud):
                raise SyncConflict("Downloaded content checksum/size could not be verified")
            self._unchanged(path, expected)
            # No await between final local check, trash, and atomic installation.
            self._trash(path, expected)
            destination = self._path(path)
            os.replace(name, destination)
            self._fsync_dir(destination.parent)
            self._baseline(path, local, cloud)
        finally:
            Path(name).unlink(missing_ok=True)

    async def _push(
        self, path: str, local: dict | None, cloud: dict | None, remote: dict[str, dict]
    ) -> None:
        self._unchanged(path, local)
        if local is None:
            if _deleted(cloud):
                self._baseline(path, None, cloud)
                return
            entity = "directories" if cloud["kind"] == "dir" else "files"
            key = "directory_id" if cloud["kind"] == "dir" else "file_id"
            deleted = await self._api("DELETE", f"/{entity}/{_id(cloud[key])}",
                                      params={"expected_version": cloud["version"]})
            deleted["kind"] = cloud["kind"]
            remote[path] = deleted
            self._baseline(path, None, deleted)
            return
        if cloud and cloud["kind"] != local["kind"]:
            raise SyncConflict("File/directory type conflict")
        parent_path = str(PurePosixPath(path).parent)
        parent = remote.get(parent_path)
        if parent_path == ".":
            directory_id = self._state["binding"]["root_directory_id"]
        elif not parent or _deleted(parent) or parent["kind"] != "dir":
            raise SyncConflict("Cloud parent is missing or conflicted")
        else:
            directory_id = parent["directory_id"]
        if local["kind"] == "dir":
            if cloud and _deleted(cloud):
                record = await self._api(
                    "POST", f"/directories/{_id(cloud['directory_id'])}/restore",
                    json={"expected_version": cloud["version"]},
                )
            else:
                record = await self._api(
                    "POST", f"/projects/{_id(self._state['binding']['project_id'])}/directories",
                    json={"name": PurePosixPath(path).name, "parent_directory_id": directory_id},
                )
            record["kind"] = "dir"
            remote[path] = record
            self._baseline(path, local, record)
            return
        if cloud and _deleted(cloud):
            cloud = await self._api("POST", f"/files/{_id(cloud['file_id'])}/restore",
                                    json={"expected_version": cloud["version"]})
            cloud["kind"] = "file"
            remote[path] = cloud
        snapshot = self._snapshot(path, local)
        pending = {"path": path, "snapshot": snapshot.name, "local": local,
                   "expected_version": cloud["version"] if cloud else 0,
                   "request": {"filename": PurePosixPath(path).name,
                               "size_bytes": local["size"],
                               "expected_version": cloud["version"] if cloud else 0,
                               "project_id": self._state["binding"]["project_id"],
                               "directory_id": directory_id,
                               "content_type": "application/octet-stream"},
                   "stage": "allocate"}
        if cloud:
            pending["request"]["file_id"] = cloud["file_id"]
        self._state["pending"][path] = pending
        self._save()
        record = await self._finish_upload(pending)
        remote[path] = record

    def _snapshot(self, path: str, expected: dict) -> Path:
        self._unchanged(path, expected)
        fd, name = tempfile.mkstemp(dir=self._state_dir, prefix="upload-")
        sha = hashlib.sha256()
        try:
            with os.fdopen(fd, "wb") as target:
                source_fd = os.open(self._path(path), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                with os.fdopen(source_fd, "rb") as source:
                    if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                        raise SyncConflict("Upload source is no longer a regular file")
                    while chunk := source.read(CHUNK_SIZE):
                        target.write(chunk)
                        sha.update(chunk)
                target.flush()
                os.fsync(target.fileno())
            if sha.hexdigest() != expected["sha256"]:
                raise SyncConflict("Local file changed while taking upload snapshot")
            self._unchanged(path, expected)
            return Path(name)
        except BaseException:
            Path(name).unlink(missing_ok=True)
            raise

    async def _finish_upload(self, pending: dict) -> dict:
        path = pending["path"]
        snapshot = self._state_dir / pending["snapshot"]
        if snapshot.parent != self._state_dir or snapshot.is_symlink():
            raise CloudSyncError("Invalid upload snapshot")
        if pending["stage"] == "allocate":
            # Lost allocation responses can leave an expiring reservation, never a commit.
            payload = await self._api("POST", "/uploads", json=pending["request"])
            pending["upload_id"] = payload["upload_id"]
            pending["stage"] = "put"
            self._save()  # Record ID before the first PUT; never persist signed URLs.
            async def chunks():
                with snapshot.open("rb") as stream:
                    while chunk := stream.read(CHUNK_SIZE):
                        self._guard()
                        yield chunk

            async with self.client.transfer() as client:
                uploaded = await client.put(
                    self.client.check_url(payload["upload_url"]), content=chunks(),
                    headers={"Content-Type": "application/octet-stream",
                             "Content-Length": str(pending["local"]["size"])},
                )
                uploaded.raise_for_status()
            self._guard()
            pending["stage"] = "complete"
            self._save()
        record = await self._api("POST", f"/uploads/{_id(pending['upload_id'])}/complete",
                                 json={"expected_version": pending["expected_version"]})
        record["kind"] = "file"
        self._baseline(path, pending["local"], record)
        del self._state["pending"][path]
        self._save()
        snapshot.unlink(missing_ok=True)
        return record

    async def _recover(self) -> None:
        for path, pending in list(self._state["pending"].items()):
            if path in self._state["conflicts"]:
                continue
            try:
                await self._finish_upload(pending)
            except SyncConflict:
                # Never guess whether a timed-out completion committed. Keep the
                # snapshot and upload ID available for explicit resolution/retry.
                self._conflict(path, "Pending upload could not complete; inspect cloud version")

    async def resolve(self, path: str, choice: str) -> dict[str, Any]:
        if choice not in {"local", "cloud", "both"}:
            raise ValueError("choice must be local, cloud, or both")
        async with self._mutex:
            with self._locked():
                self._guard()
                if path not in self._state["conflicts"]:
                    raise CloudSyncError("Path has no recorded conflict")
                remote = await self._remote()
                cloud = remote.get(path)
                local = self._fingerprint(path)
                if path in self._state["pending"]:
                    pending = self._state["pending"][path]
                    # Fail is safe for pending uploads; committed uploads are immutable.
                    if pending.get("upload_id"):
                        await self._api("POST", f"/uploads/{_id(pending['upload_id'])}/fail")
                    del self._state["pending"][path]
                    self._save()
                if choice == "both":
                    if not local or local["kind"] != "file" or not cloud or _deleted(cloud):
                        raise SyncConflict("Keep-both requires two live files")
                    source = PurePosixPath(path)
                    alternate = str(source.with_name(
                        f"{source.stem}.local-{uuid.uuid4().hex[:12]}{source.suffix}"
                    ))
                    snapshot = self._snapshot(path, local)
                    try:
                        self._unchanged(alternate, None)
                        os.replace(snapshot, self._path(alternate))
                        self._fsync_dir(self._path(alternate).parent)
                    finally:
                        snapshot.unlink(missing_ok=True)
                    await self._push(alternate, local, None, remote)
                    await self._pull(path, cloud, local)
                elif choice == "local":
                    await self._push(path, local, cloud, remote)
                elif cloud is None:
                    raise SyncConflict("Missing cloud record is not a verified deletion")
                else:
                    await self._pull(path, cloud, local)
        return self.status()

    async def run(self, stop: asyncio.Event, *, interval: float = 30.0) -> None:
        """Caller-owned automatic loop. Cancellation propagates; failures back off."""
        if interval <= 0:
            raise ValueError("interval must be positive")
        while not stop.is_set():
            try:
                await self.sync_once()
            except (OSError, CloudSyncError):
                pass
            delay = min(interval * 2 ** min(self._failures, 5), 900)
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                pass
