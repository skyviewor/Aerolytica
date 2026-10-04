"""Keep recovery/version commands independent of the heavyweight chat UI."""

from __future__ import annotations

import sys


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
            import os

            from aero.distribution import install_root

            launcher = str(install_root() / "current/bin/aero")
            os.execv(launcher, [launcher, *restart])
    else:
        chat_main()


if __name__ == "__main__":
    main()
