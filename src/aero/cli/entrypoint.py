"""Keep recovery/version commands independent of the heavyweight chat UI."""

from __future__ import annotations

import os
import sys


def _restart_current(arguments: list[str]) -> None:
    from aero.cli.update import rollback
    from aero.distribution import DistributionError, install_root

    launcher = str(install_root() / "current/bin/aero")
    try:
        os.execv(launcher, [launcher, *arguments])
    except OSError:
        # The smoke test has already checked the complete CLI import and entry
        # point. If exec itself still fails, use the same idle-only rollback;
        # never kill a replacement worker that has accepted a new task.
        rollback(wait=0)
        try:
            os.execv(launcher, [launcher, *arguments])
        except OSError as exc:
            raise DistributionError(
                "新旧启动入口均无法执行，请使用 aero update --rollback 检查安装。"
            ) from exc


def main() -> None:
    from aero.distribution import DistributionError
    from aero.release_environment import configure_runtime

    try:
        configure_runtime()
    except DistributionError as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    command = sys.argv[1] if len(sys.argv) > 1 else ""
    if command in {"version", "--version", "-v"}:
        from aero.version import title

        print(title())
        return
    if command == "update":
        from aero.cli.update import main as update_main

        raise SystemExit(update_main(sys.argv[2:]))
    if command == "setup":
        from aero.cli.onboarding import main as setup_main

        raise SystemExit(setup_main(sys.argv[2:]))
    # Only an explicit foreground chat launch applies a staged release.
    # Background agent/daemon startup and internal handoffs never enter here.
    if (
        command in {"", "chat"}
        and sys.stdin.isatty()
        and sys.stdout.isatty()
        and not os.environ.get("AERO_UPDATE_RESTART_TOKEN")
    ):
        from aero.cli.pending_update import apply_pending

        try:
            if apply_pending():
                _restart_current(sys.argv[1:])
        except (DistributionError, OSError, ValueError) as exc:
            message = str(exc) if isinstance(exc, DistributionError) else "待更新环境无法启动。"
            print("未应用待更新版本，继续使用当前版本：" + message, file=sys.stderr)
    if command == "serve" and os.environ.get("AERO_ENABLE_WEB_PREVIEW") != "1":
        print("Web 工作台暂未公开；请使用 aero chat。", file=sys.stderr)
        raise SystemExit(2)
    from aero.cli.main import main as chat_main
    from aero.distribution import DistributionError
    from aero.update_processes import process_record

    if command in {"", "chat", "serve"} or (
        command == "agent"
        and len(sys.argv) > 2
        and sys.argv[2] in {"run", "start"}
        and "--background" not in sys.argv
        and "-b" not in sys.argv
    ):
        try:
            with process_record():
                restart = chat_main()
        except DistributionError as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(1) from None
        if isinstance(restart, list):
            import subprocess

            from aero.distribution import install_root

            if os.name == "nt":
                launcher = install_root() / "current" / "Scripts" / "aero.exe"
                if launcher.is_file():
                    raise SystemExit(subprocess.call([str(launcher), *restart]))
                raise SystemExit(
                    subprocess.call([sys.executable, "-m", "aero.cli.entrypoint", *restart])
                )
            _restart_current(restart)
    else:
        chat_main()


if __name__ == "__main__":
    main()
