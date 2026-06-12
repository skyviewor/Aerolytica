"""Runtime for executing agent tools — direct callables and subprocess."""

import asyncio
import inspect
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

import structlog

from aero.core.debug_log import debug_exception, debug_log

logger = structlog.get_logger()


@dataclass
class ExecutionResult:
    success: bool
    result: dict | None = None
    error: str | None = None
    stdout: str = ""
    stderr: str = ""
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    exit_code: int = 0
    duration_ms: float = 0


class _TailBuffer:
    def __init__(self, limit: int):
        self.limit = max(0, limit)
        self.total_bytes = 0
        self._text = ""

    def append(self, data: bytes) -> str:
        self.total_bytes += len(data)
        text = data.decode(errors="replace")
        if self.limit > 0:
            self._text = (self._text + text)[-self.limit:]
        return text

    @property
    def text(self) -> str:
        if self.total_bytes > len(self._text.encode(errors="replace")) and self._text:
            return f"... 输出已截断，仅保留最后 {self.limit} 字符\n{self._text}"
        return self._text


def _progress_line(label: str, text: str, limit: int = 240) -> str:
    compact = " ".join(str(text).split())
    if len(compact) > limit:
        compact = "..." + compact[-limit:]
    return f"{label}: {compact}"


class Runtime:
    """Execute tools supporting both sync/async functions and subprocess."""

    async def execute(self, tool_func, args: dict) -> ExecutionResult:
        try:
            result = tool_func(**args)
            if inspect.iscoroutine(result):
                result = await result
            return ExecutionResult(success=True, result=result)
        except Exception as e:
            logger.error("runtime.execute.error", error=str(e))
            debug_exception(
                "runtime.execute_error",
                e,
                tool=getattr(tool_func, "__name__", str(tool_func)),
                arg_keys=sorted(args.keys()),
            )
            return ExecutionResult(success=False, error=str(e), stderr=str(e))

    def run_subprocess(
        self,
        command: str,
        workdir: str = ".",
        timeout_ms: int = 120000,
    ) -> ExecutionResult:
        """Execute a shell command via subprocess and capture output.

        Args:
            command: shell command string
            workdir: working directory
            timeout_ms: timeout in milliseconds
        """
        start = time.monotonic()
        try:
            bash = Path("/bin/bash")
            run_args: list[str] | str = command
            use_shell = True
            if bash.exists():
                run_args = [str(bash), "-o", "pipefail", "-c", command]
                use_shell = False
            result = subprocess.run(
                run_args,
                shell=use_shell,
                cwd=workdir,
                capture_output=True,
                text=True,
                timeout=timeout_ms / 1000,
                env=self._build_exec_env(),
            )
            duration = (time.monotonic() - start) * 1000
            return ExecutionResult(
                success=result.returncode == 0,
                stdout=result.stdout,
                stderr=result.stderr,
                stdout_bytes=len(result.stdout.encode(errors="replace")),
                stderr_bytes=len(result.stderr.encode(errors="replace")),
                exit_code=result.returncode,
                duration_ms=duration,
            )
        except subprocess.TimeoutExpired as e:
            duration = (time.monotonic() - start) * 1000
            logger.warning("runtime.subprocess.timeout", command=command[:80])
            debug_log(
                "runtime.subprocess_timeout",
                command=command,
                workdir=workdir,
                timeout_ms=timeout_ms,
                stdout=e.stdout or "",
                stderr=e.stderr or "",
                duration_ms=duration,
            )
            return ExecutionResult(
                success=False,
                error=f"命令超时（>{timeout_ms}ms）已终止",
                stdout=e.stdout or "",
                stderr=e.stderr or "",
                stdout_bytes=len((e.stdout or "").encode(errors="replace")),
                stderr_bytes=len((e.stderr or "").encode(errors="replace")),
                exit_code=-1,
                duration_ms=duration,
            )
        except Exception as e:
            duration = (time.monotonic() - start) * 1000
            logger.error("runtime.subprocess.error", error=str(e))
            debug_exception(
                "runtime.subprocess_error",
                e,
                command=command,
                workdir=workdir,
                timeout_ms=timeout_ms,
                duration_ms=duration,
            )
            return ExecutionResult(
                success=False,
                error=str(e),
                exit_code=-1,
                duration_ms=duration,
            )

    async def run_subprocess_streaming(
        self,
        command: str,
        workdir: str = ".",
        timeout_ms: int = 120000,
        output_limit: int = 20000,
    ) -> ExecutionResult:
        """Execute a shell command while streaming stdout/stderr to progress."""
        from aero.agent.progress import cancel_requested, emit_progress

        start = time.monotonic()
        stdout_buf = _TailBuffer(output_limit)
        stderr_buf = _TailBuffer(output_limit)
        proc: asyncio.subprocess.Process | None = None

        try:
            bash = Path("/bin/bash")
            if bash.exists():
                proc = await asyncio.create_subprocess_exec(
                    str(bash),
                    "-o",
                    "pipefail",
                    "-c",
                    command,
                    cwd=workdir,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=self._build_exec_env(),
                    start_new_session=True,
                )
            else:
                proc = await asyncio.create_subprocess_shell(
                    command,
                    cwd=workdir,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=self._build_exec_env(),
                    start_new_session=True,
                )

            async def read_stream(
                stream: asyncio.StreamReader | None,
                label: str,
                buffer: _TailBuffer,
            ) -> None:
                if stream is None:
                    return
                pending = ""
                last_suppressed = ""
                last_emit = 0.0
                while True:
                    chunk = await stream.read(4096)
                    if not chunk:
                        tail = pending.strip()
                        if tail:
                            emit_progress(_progress_line(label, tail))
                        elif last_suppressed:
                            emit_progress(_progress_line(label, last_suppressed))
                        return
                    text = buffer.append(chunk)
                    pending += text.replace("\r", "\n")
                    lines = pending.split("\n")
                    pending = lines.pop() if lines else ""
                    now = time.monotonic()
                    complete = [line.strip() for line in lines if line.strip()]
                    if complete and now - last_emit >= 0.25:
                        emit_progress(_progress_line(label, complete[-1]))
                        last_emit = now
                        last_suppressed = ""
                    elif complete:
                        last_suppressed = complete[-1]
                    elif len(pending) > 240 and now - last_emit >= 0.5:
                        emit_progress(_progress_line(label, pending.strip()))
                        last_emit = now
                        last_suppressed = ""

            stdout_task = asyncio.create_task(read_stream(proc.stdout, "stdout", stdout_buf))
            stderr_task = asyncio.create_task(read_stream(proc.stderr, "stderr", stderr_buf))

            timeout_s = timeout_ms / 1000
            while True:
                if cancel_requested():
                    await self._terminate_process_group(proc)
                    duration = (time.monotonic() - start) * 1000
                    await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
                    return ExecutionResult(
                        success=False,
                        error="命令已取消",
                        stdout=stdout_buf.text,
                        stderr=stderr_buf.text,
                        stdout_bytes=stdout_buf.total_bytes,
                        stderr_bytes=stderr_buf.total_bytes,
                        exit_code=-2,
                        duration_ms=duration,
                    )
                if proc.returncode is not None:
                    break
                elapsed = time.monotonic() - start
                if elapsed > timeout_s:
                    await self._terminate_process_group(proc)
                    duration = (time.monotonic() - start) * 1000
                    await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
                    logger.warning("runtime.subprocess.timeout", command=command[:80])
                    debug_log(
                        "runtime.subprocess_timeout",
                        command=command,
                        workdir=workdir,
                        timeout_ms=timeout_ms,
                        stdout=stdout_buf.text,
                        stderr=stderr_buf.text,
                        duration_ms=duration,
                    )
                    return ExecutionResult(
                        success=False,
                        error=f"命令超时（>{timeout_ms}ms）已终止",
                        stdout=stdout_buf.text,
                        stderr=stderr_buf.text,
                        stdout_bytes=stdout_buf.total_bytes,
                        stderr_bytes=stderr_buf.total_bytes,
                        exit_code=-1,
                        duration_ms=duration,
                    )
                await asyncio.sleep(0.1)

            await proc.wait()
            await asyncio.gather(stdout_task, stderr_task, return_exceptions=True)
            duration = (time.monotonic() - start) * 1000
            return ExecutionResult(
                success=proc.returncode == 0,
                stdout=stdout_buf.text,
                stderr=stderr_buf.text,
                stdout_bytes=stdout_buf.total_bytes,
                stderr_bytes=stderr_buf.total_bytes,
                exit_code=int(proc.returncode or 0),
                duration_ms=duration,
            )
        except Exception as e:
            if proc is not None and proc.returncode is None:
                await self._terminate_process_group(proc)
            duration = (time.monotonic() - start) * 1000
            logger.error("runtime.subprocess.error", error=str(e))
            debug_exception(
                "runtime.subprocess_error",
                e,
                command=command,
                workdir=workdir,
                timeout_ms=timeout_ms,
                duration_ms=duration,
            )
            return ExecutionResult(
                success=False,
                error=str(e),
                stdout=stdout_buf.text,
                stderr=stderr_buf.text,
                stdout_bytes=stdout_buf.total_bytes,
                stderr_bytes=stderr_buf.total_bytes,
                exit_code=-1,
                duration_ms=duration,
            )

    async def _terminate_process_group(self, proc: asyncio.subprocess.Process) -> None:
        if proc.returncode is not None:
            return
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except Exception:
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except asyncio.TimeoutError:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                proc.kill()
            await proc.wait()

    def detect_python_env(self, workdir: str = ".") -> str:
        """Detect the appropriate python command for the project environment.

        Priority:
        1. pixi.toml exists → "pixi run python"
        2. .venv/bin/python exists → absolute path
        3. sys.executable → fallback
        """
        root = Path(workdir)
        for parent in [root, *root.parents]:
            if (parent / "pixi.toml").exists():
                return "pixi run python"
            venv_python = parent / ".venv" / "bin" / "python"
            if venv_python.exists():
                return str(venv_python)
            if path := self._find_conda_python():
                return path
        return str(sys_executable())

    def run_python(
        self,
        script_path: str,
        workdir: str = ".",
        timeout_ms: int = 120000,
    ) -> ExecutionResult:
        """Execute a Python script using the detected environment."""
        python = self.detect_python_env(workdir)
        return self.run_subprocess(f"{python} {script_path}", workdir, timeout_ms)

    @staticmethod
    def _build_exec_env() -> dict:
        env = os.environ.copy()
        env.pop("PYTHONHOME", None)
        env.pop("PIXI_IN_SHELL", None)
        path_parts: list[str] = []
        for root in _conda_roots(env):
            tool_bin = root / "envs" / "aero-agent" / "bin"
            if tool_bin.exists():
                path_parts.append(str(tool_bin))
            base_bin = root / "bin"
            if base_bin.exists():
                path_parts.append(str(base_bin))
        if path_parts:
            existing = env.get("PATH", "")
            env["PATH"] = os.pathsep.join([*path_parts, existing] if existing else path_parts)
        return env

    @staticmethod
    def _find_conda_python() -> str | None:
        for path_env in ["CONDA_PREFIX", "MAMBA_PREFIX"]:
            prefix = os.environ.get(path_env)
            if prefix:
                py = Path(prefix) / "bin" / "python"
                if py.exists():
                    return str(py)
        return None


def sys_executable() -> str:
    return getattr(sys, "executable", "") or sys.executable


def _conda_roots(env: dict[str, str]) -> list[Path]:
    roots: list[Path] = []
    for key in ("CONDA_PREFIX", "MAMBA_PREFIX"):
        value = env.get(key)
        if value:
            prefix = Path(value)
            roots.append(prefix.parent.parent if prefix.parent.name == "envs" else prefix)
    conda_exe = env.get("CONDA_EXE") or env.get("MAMBA_EXE")
    if conda_exe:
        roots.append(Path(conda_exe).resolve().parent.parent)
    roots.extend(
        [
            Path.home() / "miniconda3",
            Path.home() / "anaconda3",
            Path.home() / "mambaforge",
            Path.home() / "micromamba",
        ]
    )
    unique: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        key = str(root)
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique
