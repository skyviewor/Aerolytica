"""Opt-in, per-workspace resident Web runtime (never installs on import)."""

from __future__ import annotations

import hashlib
import json
import os
import plistlib
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx


def private_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.stat().st_uid != os.getuid():
        raise RuntimeError("unsafe_state_directory")
    path.chmod(0o700)
    return path


def private_write(path: Path, data: str) -> None:
    """Atomic owner-only state; never follow an existing destination symlink."""
    private_directory(path.parent)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class WorkspaceLock:
    """OS lock survives neither a crash nor process exit; PID files are not locks."""

    def __init__(self, path: Path):
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        import fcntl

        private_directory(self.path.parent)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd

    def release(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None


class DaemonManager:
    """Manage the existing Web app in a detached, authenticated loopback process."""

    def __init__(self, project_dir: Path | str, state_dir: Path | None = None):
        self.project_dir = Path(project_dir).resolve()
        self.workspace_id = hashlib.sha256(str(self.project_dir).encode()).hexdigest()[:20]
        self.state_dir = state_dir or Path.home() / ".aero" / "daemons" / self.workspace_id
        self.state_path = self.state_dir / "state.json"
        self.log_path = self.state_dir / "daemon.log"

    def _read(self) -> dict[str, Any]:
        try:
            if self.state_path.is_symlink():
                return {}
            stat = self.state_path.stat()
            if stat.st_uid != os.getuid() or stat.st_mode & 0o077:
                return {}
            state = json.loads(self.state_path.read_text())
            if state.get("project_dir") != str(self.project_dir):
                return {}
            port = int(state["port"])
            if not 1 <= port <= 65535:
                return {}
            return state
        except (OSError, ValueError, KeyError, TypeError):
            return {}

    def status(self) -> dict[str, Any]:
        state = self._read()
        if not state:
            return {"running": False, "project_dir": str(self.project_dir)}
        try:
            response = httpx.get(
                f"http://127.0.0.1:{state['port']}/api/v1/daemon/status",
                headers={"Authorization": f"Bearer {state['token']}"},
                timeout=1, trust_env=False,
            )
            response.raise_for_status()
            if response.json().get("instance_id") != state.get("instance_id"):
                raise ValueError("wrong_daemon")
        except (httpx.HTTPError, ValueError):
            return {"running": False, "project_dir": str(self.project_dir)}
        return {"running": True, "pid": state["pid"],
                "url": f"http://127.0.0.1:{state['port']}/",
                "project_dir": str(self.project_dir)}

    def browser_url(self) -> str:
        if not self.status()["running"]:
            raise RuntimeError("daemon_not_running")
        state = self._read()
        return f"http://127.0.0.1:{state['port']}/?token={state['token']}"

    def connection_info(self) -> dict[str, str]:
        """Return the authenticated loopback connection for local clients."""
        if not self.status()["running"]:
            raise RuntimeError("daemon_not_running")
        state = self._read()
        return {
            "base_url": f"http://127.0.0.1:{state['port']}",
            "token": str(state["token"]),
            "project_dir": str(self.project_dir),
        }

    def start(self, *, port: int = 0, timeout: float = 15) -> dict[str, Any]:
        private_directory(self.state_dir)
        launch_lock = WorkspaceLock(self.state_dir / "launch.lock")
        launch_lock.acquire()
        try:
            status = self.status()
            if status["running"]:
                return status
            fd = os.open(self.log_path, os.O_APPEND | os.O_CREAT | os.O_WRONLY
                         | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "a") as log:
                process = subprocess.Popen(
                    [sys.executable, "-m", "aero.cli.daemon", "run",
                     "--project", str(self.project_dir), "--state-dir", str(self.state_dir),
                     "--port", str(port)],
                    cwd=self.project_dir, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                    start_new_session=True, close_fds=True,
                )
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                status = self.status()
                if status["running"]:
                    return status
                if process.poll() is not None:
                    raise RuntimeError("daemon_start_failed; inspect aero service logs")
                time.sleep(0.1)
            raise TimeoutError("daemon_start_timeout; inspect aero service status/logs")
        finally:
            launch_lock.release()

    def stop(self) -> dict[str, Any]:
        status = self.status()
        if not status["running"]:
            return status
        state = self._read()
        # Authenticated shutdown avoids signalling a recycled PID.
        response = httpx.post(
            f"http://127.0.0.1:{state['port']}/api/v1/daemon/stop",
            headers={"Authorization": f"Bearer {state['token']}"},
            timeout=5, trust_env=False,
        )
        response.raise_for_status()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if not self.status()["running"]:
                return {"running": False, "project_dir": str(self.project_dir)}
            time.sleep(0.1)
        return {**status, "stopping": True}

    def logs(self, *, lines: int = 100) -> str:
        if not self.log_path.exists():
            return ""
        with self.log_path.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            handle.seek(max(0, handle.tell() - 128 * 1024))
            return "\n".join(handle.read().decode(errors="replace").splitlines()[-lines:])

    def autostart_spec(self, platform: str | None = None) -> tuple[Path, str]:
        """Return OS configuration without writing it or invoking OS tools."""
        platform = platform or sys.platform
        label = f"com.aerolytica.daemon.{self.workspace_id}"
        command = [sys.executable, "-m", "aero.cli.daemon", "run", "--project",
                   str(self.project_dir), "--state-dir", str(self.state_dir)]
        if platform == "darwin":
            path = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
            content = plistlib.dumps({
                "Label": label, "ProgramArguments": command, "RunAtLoad": True,
                "WorkingDirectory": str(self.project_dir),
                "StandardOutPath": str(self.log_path), "StandardErrorPath": str(self.log_path),
                "KeepAlive": {"SuccessfulExit": False},
            }).decode()
        elif platform.startswith("linux"):
            path = Path.home() / ".config" / "systemd" / "user" / f"{label}.service"
            def quote(value: str) -> str:
                if "\n" in value or "\r" in value:
                    raise ValueError("invalid_service_path")
                return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace(
                    "%", "%%").replace("$", "$$") + '"'
            content = ("[Unit]\nDescription=Aerolytica workspace service\n"
                       "[Service]\nType=simple\nUMask=0077\nExecStart="
                       + " ".join(quote(arg) for arg in command)
                       + "\nRestart=on-failure\n[Install]\nWantedBy=default.target\n")
        else:
            raise RuntimeError("autostart_unsupported; use detached start")
        return path, content

    def install_autostart(self, *, enabled: bool = True) -> dict[str, Any]:
        """Install and activate the user-scoped startup entry explicitly."""
        path, content = self.autostart_spec()
        if enabled:
            private_directory(self.state_dir)
            # OS config parent may be shared with other applications: do not chmod it.
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.is_symlink():
                raise RuntimeError("unsafe_autostart_path")
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(content)
            if sys.platform == "darwin":
                uid = str(os.getuid())
                result = subprocess.run(
                    ["launchctl", "bootstrap", f"gui/{uid}", str(path)],
                    capture_output=True, text=True, check=False,
                )
                if result.returncode not in {0, 37}:  # 37 means already bootstrapped.
                    raise RuntimeError(f"launchctl bootstrap failed: {result.stderr.strip()}")
            elif sys.platform.startswith("linux"):
                result = subprocess.run(
                    ["systemctl", "--user", "daemon-reload"],
                    capture_output=True, text=True, check=False,
                )
                if result.returncode == 0:
                    result = subprocess.run(
                        ["systemctl", "--user", "enable", "--now", path.name],
                        capture_output=True, text=True, check=False,
                    )
                if result.returncode != 0:
                    raise RuntimeError(f"systemctl --user failed: {result.stderr.strip()}")
        else:
            if sys.platform == "darwin":
                subprocess.run(
                    ["launchctl", "bootout", f"gui/{os.getuid()}/{path.stem}"],
                    capture_output=True, text=True, check=False,
                )
            elif sys.platform.startswith("linux"):
                subprocess.run(
                    ["systemctl", "--user", "disable", "--now", path.name],
                    capture_output=True, text=True, check=False,
                )
            path.unlink(missing_ok=True)
        return {"enabled": enabled, "path": str(path), "activation_required": False}


def run_daemon(manager: DaemonManager, *, port: int = 0) -> None:
    """Serve the normal UI/API, with a small authenticated process-control surface."""
    import uvicorn
    from fastapi import HTTPException, Request

    from aero.server.app import create_app

    lock = WorkspaceLock(manager.state_dir / "daemon.lock")
    lock.acquire()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
        token = secrets.token_urlsafe(32)
        instance_id = secrets.token_hex(16)
        app, runtime = create_app(manager.project_dir, launch_token=token)
        server = uvicorn.Server(uvicorn.Config(app, access_log=False, log_level="warning"))

        def authenticate(request: Request) -> None:
            supplied = request.headers.get("authorization", "")
            if not secrets.compare_digest(supplied, f"Bearer {token}"):
                raise HTTPException(401, "unauthorized")
            if request.headers.get("origin"):
                raise HTTPException(403, "browser_control_forbidden")

        # Register concrete annotations: Request is deliberately imported lazily.
        async def status(request):
            authenticate(request)
            return {"running": True, "instance_id": instance_id}

        async def stop(request):
            authenticate(request)
            server.should_exit = True
            return {"stopping": True}

        status.__annotations__["request"] = Request
        stop.__annotations__["request"] = Request
        app.add_api_route("/api/v1/daemon/status", status, methods=["GET"])
        app.add_api_route("/api/v1/daemon/stop", stop, methods=["POST"])
        private_write(manager.state_path, json.dumps({
            "pid": os.getpid(), "port": sock.getsockname()[1], "token": token,
            "instance_id": instance_id, "project_dir": str(manager.project_dir),
        }))
        server.run(sockets=[sock])
    finally:
        manager.state_path.unlink(missing_ok=True)
        sock.close()
        lock.release()
