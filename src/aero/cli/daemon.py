"""Small service CLI; imports the large TUI only when explicitly requested."""

from __future__ import annotations

import argparse
import json
import webbrowser
from pathlib import Path

from aero.core.daemon import DaemonManager, run_daemon


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="aero service")
    parser.add_argument("action", choices=[
        "start", "stop", "status", "logs", "run", "web", "enable", "disable",
    ])
    parser.add_argument("--project", type=Path, default=Path.cwd())
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("port must be between 0 and 65535")
    manager = DaemonManager(args.project, args.state_dir)
    if args.action == "run":
        run_daemon(manager, port=args.port)
    elif args.action == "web":
        result = manager.start(port=args.port)
        if not args.no_open:
            webbrowser.open(manager.browser_url())
        # No bearer capability in logs or normal status output.
        print(json.dumps(result))
    elif args.action == "start":
        print(json.dumps(manager.start(port=args.port)))
    elif args.action == "logs":
        print(manager.logs())
    elif args.action in {"enable", "disable"}:
        print(json.dumps(manager.install_autostart(enabled=args.action == "enable")))
    else:
        print(json.dumps(getattr(manager, args.action)()))


if __name__ == "__main__":
    main()
