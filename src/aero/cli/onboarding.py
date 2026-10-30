"""Prepare the private scientific runtime without account or model setup."""

from __future__ import annotations

import argparse

from aero.release_environment import environment_name


def main(args: list[str]) -> int:
    from aero.cli.init_runtime import setup_runtime

    command = "aero-testing" if environment_name() == "testing" else "aero"
    parser = argparse.ArgumentParser(
        prog=f"{command} setup", description="安装或修复私有科研运行环境，不登录或配置模型。"
    )
    parser.add_argument("--full", action="store_true")
    parser.add_argument(
        "--runtime-only", action="store_true", help="兼容旧命令；setup 默认只准备运行时"
    )
    parser.add_argument("--yes", "-y", action="store_true")
    parser.add_argument("--china-mirror", action="store_true")
    options = parser.parse_args(args)
    if not setup_runtime(
        full=options.full, assume_yes=options.yes, china_mirror=options.china_mirror
    ):
        return 1
    print(f"科研运行环境已就绪。进入研究目录后运行 {command} init，再运行 {command} chat。")
    print(f"setup 不要求登录；使用官方模型时可运行 {command} login，或在首次 chat 中授权。")
    return 0
