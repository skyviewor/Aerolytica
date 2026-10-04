"""HTTPS installer bootstrap, run by the downloaded private Python."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--uv", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="aero-bootstrap-") as directory:
        module_path = Path(directory) / "distribution.py"
        with zipfile.ZipFile(args.wheel) as wheel:
            module_path.write_bytes(wheel.read("aero/distribution.py"))
        spec = importlib.util.spec_from_file_location("aero_install_distribution", module_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        try:
            with module.install_lock():
                envelope = json.loads(args.manifest.read_text())
                environment = module.stage_release(envelope, args.uv, args.base, bootstrap=True)
                current = module.current_environment()
                if current and current != environment.resolve():
                    raise module.DistributionError(
                        "已有官网安装，请运行 aero update 安全升级；未切换版本。"
                    )
                launcher = module.install_launcher(
                    Path(os.environ.get("AERO_BIN_DIR", "~/.local/bin")).expanduser()
                )
                module.activate(environment)
                print(f"客户端已安装：{launcher}")
        except module.DistributionError as exc:
            print(f"安装失败：{exc}", file=sys.stderr)
            raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
