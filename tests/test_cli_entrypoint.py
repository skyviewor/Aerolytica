"""Public command availability for the TUI release."""

import os
import subprocess
import sys
from pathlib import Path


def test_public_cli_hides_web_preview():
    environment = {**os.environ, "AERO_ENABLE_WEB_PREVIEW": ""}
    result = subprocess.run(
        [sys.executable, "-m", "aero.cli.entrypoint", "serve", "--no-open"],
        cwd=Path(__file__).resolve().parents[1],
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 2
    assert "请使用 aero chat" in result.stderr
