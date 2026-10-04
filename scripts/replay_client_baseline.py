"""Replay tests with pre-change client implementations without changing the checkout.

Existing dirty model-catalog edits, dependencies and test environment remain intact.
Tests themselves are current: use the documented failure selectors for comparison.
"""

from __future__ import annotations

import argparse
import importlib.abc
import importlib.util
import subprocess
import sys
from pathlib import Path

MODULES = {
    "aero.agent.llm_client",
    "aero.agent.loop",
    "aero.agent.official_vision_client",
    "aero.application.chat_presence",
    "aero.application.cloud_runtime",
    "aero.application.local_session",
    "aero.cli.agent_runner",
    "aero.cli.main",
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", default="59573032d5c760837f5e52e554c419306384db77")
    parser.add_argument("tests", nargs="+")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    revision = subprocess.check_output(
        ["git", "rev-parse", "--verify", args.ref + "^{commit}"],
        cwd=root,
        text=True,
    ).strip()

    class BaselineLoader(importlib.abc.Loader):
        def __init__(self, name):
            self.name = name

        def create_module(self, spec):
            return None

        def exec_module(self, module):
            path = "src/" + self.name.replace(".", "/") + ".py"
            source = subprocess.check_output(
                ["git", "show", revision + ":" + path],
                cwd=root,
                text=True,
            )
            module.__file__ = str(root / path)
            exec(compile(source, module.__file__, "exec"), module.__dict__)

    class BaselineFinder(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname in MODULES:
                return importlib.util.spec_from_loader(fullname, BaselineLoader(fullname))
            return None

    sys.meta_path.insert(0, BaselineFinder())
    print(f"Client implementation baseline: {revision}")
    import pytest

    raise SystemExit(pytest.main(["-q", "--tb=no", *args.tests]))


if __name__ == "__main__":
    main()
