"""Detached resident worker with a process-lock startup handshake."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import select
import signal
import socket
import stat
import subprocess
import sys
import time
from pathlib import Path

from aero.core.daemon import private_directory
from aero.core.remote_agent import AgentProcessLock, RemoteAgentError
from aero.release_environment import user_home


def control_path(agent_id: str) -> Path:
    directory = private_directory(user_home() / "agent-runtime" / "background")
    filename = hashlib.sha256(agent_id.encode()).hexdigest()[:24] + ".sock"
    if len(os.fsencode(directory / filename)) >= 100:
        # macOS UNIX sockets have a short path limit. Keep the fallback private.
        scope = hashlib.sha256(str(user_home()).encode()).hexdigest()[:12]
        directory = private_directory(Path("/tmp") / f"aero-agent-{os.getuid()}-{scope}")
    return directory / filename


def stop_background_agent(agent_id: str, *, timeout: float = 10) -> bool:
    """Stop through an owner-private socket, never signal a remembered PID."""
    path = control_path(agent_id)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(2)
            connection.connect(str(path))
            connection.sendall(b"stop\n")
            if connection.recv(64) != b"stopping\n":
                raise RemoteAgentError("后台控制服务返回无效响应，未确认停止。")
    except (FileNotFoundError, ConnectionRefusedError):
        # Distinguish an already stopped worker from a foreground/older worker.
        probe = AgentProcessLock(agent_id)
        try:
            probe.acquire()
        except RemoteAgentError as exc:
            raise RemoteAgentError(
                "该 Agent 正由前台或旧版进程运行，请在原终端按 Ctrl+C 停止。"
            ) from exc
        finally:
            probe.release()
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        probe = AgentProcessLock(agent_id)
        try:
            probe.acquire()
        except RemoteAgentError:
            time.sleep(0.1)
        else:
            return True
        finally:
            probe.release()
    raise RemoteAgentError("已请求停止，但进程尚未退出，请查看日志；未强制杀进程。")


async def serve_worker(agent_id: str, ready_fd: int, lock: AgentProcessLock) -> int:
    from aero.cli.agent_runner import _run_service

    path = control_path(agent_id)
    if path.exists():
        info = path.lstat()
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
            raise RemoteAgentError("后台控制路径不安全，拒绝覆盖。")
        path.unlink()

    runtime = None

    def bind_runtime(value) -> None:
        nonlocal runtime
        runtime = value

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            command = await asyncio.wait_for(reader.readline(), timeout=2)
            if command in {b"status\n", b"drain\n", b"resume\n", b"update-stop\n"}:
                if runtime is None:
                    payload = {"protocol": 1, "update_busy": True}
                else:
                    if command in {b"drain\n", b"resume\n"}:
                        runtime.update_paused = command == b"drain\n"
                        runtime.session.update_paused = runtime.update_paused
                    payload = {"protocol": 1, **runtime.status()}
                    if command == b"update-stop\n":
                        payload["stopping"] = runtime.update_paused and not payload["update_busy"]
                writer.write(json.dumps(payload).encode() + b"\n")
                await writer.drain()
                if payload.get("stopping"):
                    os.kill(os.getpid(), signal.SIGTERM)
            elif command == b"stop\n":
                writer.write(b"stopping\n")
                await writer.drain()
                # This targets only the server's own process, avoiding PID reuse.
                os.kill(os.getpid(), signal.SIGTERM)
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_unix_server(handle, path=str(path))
    path.chmod(0o600)
    try:

        def ready() -> None:
            nonlocal ready_fd
            os.write(ready_fd, b"1")
            os.close(ready_fd)
            ready_fd = -1

        return await _run_service(
            agent_id, process_lock=lock, on_ready=ready, on_runtime=bind_runtime
        )
    finally:
        if ready_fd >= 0:
            os.close(ready_fd)
        server.close()
        await server.wait_closed()
        path.unlink(missing_ok=True)


def launch_background_agent(
    agent_id: str,
    project_dir: Path,
    *,
    interpreter: str | None = None,
) -> tuple[int, Path]:
    directory = private_directory(user_home() / "agent-runtime" / "background")
    key = hashlib.sha256(agent_id.encode()).hexdigest()[:24]
    log_path = directory / f"{key}.log"
    read_fd, write_fd = os.pipe()
    process = None
    try:
        fd = os.open(log_path, os.O_CREAT | os.O_APPEND | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "a") as log:
            process = subprocess.Popen(
                [
                    interpreter or sys.executable,
                    "-u",
                    "-m",
                    "aero.cli.agent_background",
                    agent_id,
                    "--ready-fd",
                    str(write_fd),
                ],
                cwd=project_dir.resolve(),
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
                close_fds=True,
                pass_fds=(write_fd,),
            )
        os.close(write_fd)
        write_fd = -1
        readable, _, _ = select.select([read_fd], [], [], 15)
        if not readable or os.read(read_fd, 1) != b"1" or process.poll() is not None:
            raise RemoteAgentError(
                f"后台进程未能启动，可能已有相同 Agent 运行。请查看日志：{log_path}"
            )
        return process.pid, log_path
    except BaseException:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        raise
    finally:
        os.close(read_fd)
        if write_fd >= 0:
            os.close(write_fd)


def main() -> int:
    import argparse

    from aero.release_environment import configure_runtime

    configure_runtime()

    parser = argparse.ArgumentParser()
    parser.add_argument("agent_id")
    parser.add_argument("--ready-fd", type=int, required=True)
    args = parser.parse_args()
    lock = AgentProcessLock(args.agent_id)
    try:
        lock.acquire()
        from aero.update_processes import process_record

        with process_record("agent", agent_id=args.agent_id):
            return asyncio.run(serve_worker(args.agent_id, args.ready_fd, lock))
    except (RemoteAgentError, OSError, ValueError) as exc:
        print(f"后台智能体启动失败：{exc}", flush=True)
        return 1
    finally:
        lock.release()


if __name__ == "__main__":
    raise SystemExit(main())
