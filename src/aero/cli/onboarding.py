"""Explicit setup: reuse the account wizard without launching an Agent."""

from __future__ import annotations

import argparse
import sys


def configure_account() -> bool:
    from textual.app import App

    from aero.cli.main import _config_needs_llm_setup, _load_config
    from aero.cli.setup_wizard import FirstRunSetupScreen

    config = _load_config()
    if not _config_needs_llm_setup(config):
        print("账户/模型配置已就绪，保留现有设置。")
        return True

    class SetupApp(App):
        def on_mount(self):
            self.push_screen(FirstRunSetupScreen(), self.complete)

        def complete(self, result):
            if result is None:
                self.exit(False)
                return
            # Persistence is factored into a standalone helper in main.py.
            from aero.cli.main import persist_first_run_setup

            persist_first_run_setup(config, result, save_workspace=False)
            self.exit(True)

    return bool(SetupApp().run())


def main(args: list[str]) -> int:
    from aero.cli.init_runtime import setup_runtime

    parser = argparse.ArgumentParser(prog="aero setup")
    parser.add_argument("--full", action="store_true")
    parser.add_argument("--runtime-only", action="store_true")
    parser.add_argument("--yes", "-y", action="store_true")
    parser.add_argument("--china-mirror", action="store_true")
    options = parser.parse_args(args)
    if not options.runtime_only:
        if not sys.stdin.isatty():
            print("账户初始化需要交互终端；自动化请使用 aero setup --runtime-only --yes。")
            return 2
        if not configure_account():
            print("初始化已取消，已有设置保留。")
            return 1
    if not setup_runtime(
        full=options.full, assume_yes=options.yes, china_mirror=options.china_mirror
    ):
        return 1
    print("初始化完成。进入研究目录后运行 aero init，再运行 aero chat。")
    return 0
