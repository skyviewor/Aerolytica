"""Exercise the standalone installer's shell setup without touching real dotfiles."""

from __future__ import annotations

import builtins
import io
import os
import select
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def helper():
    source = (ROOT / "install.sh").read_text().split("# BEGIN AERO SHELL SETUP\n", 1)[1]
    source = source.split("# END AERO SHELL SETUP", 1)[0]
    namespace = {"__name__": "installer_shell_test"}
    exec(compile(source, "installer_shell_setup", "exec"), namespace)
    return namespace


@pytest.mark.parametrize("shell,name", [
    ("zsh", ".zshrc"), ("sh", ".profile"), ("dash", ".profile"),
    ("fish", ".config/fish/config.fish"),
])
def test_default_rc_selection(helper, tmp_path, shell, name):
    assert helper["rc_files"](shell, tmp_path, {}) == [tmp_path / name]


def test_custom_zdotdir_and_fish_xdg_are_respected(helper, tmp_path):
    assert helper["rc_files"]("zsh", tmp_path, {"ZDOTDIR": str(tmp_path / "z")}) == [
        tmp_path / "z/.zshrc"
    ]
    assert helper["rc_files"]("fish", tmp_path, {"XDG_CONFIG_HOME": str(tmp_path / "f")}) == [
        tmp_path / "f/fish/config.fish"
    ]


def test_bash_preserves_login_priority_and_never_masks_existing_profile(helper, tmp_path):
    profile = tmp_path / ".profile"
    profile.write_text("# existing profile\n")
    assert helper["rc_files"]("bash", tmp_path, {}) == [tmp_path / ".bashrc", profile]
    assert not (tmp_path / ".bash_profile").exists()
    login = tmp_path / ".bash_login"
    login.touch()
    assert helper["rc_files"]("bash", tmp_path, {})[-1] == login
    preferred = tmp_path / ".bash_profile"
    preferred.touch()
    assert helper["rc_files"]("bash", tmp_path, {})[-1] == preferred


def test_unknown_shell_is_not_treated_as_bash_and_override_is_validated(helper, tmp_path):
    assert helper["rc_files"]("unknown", tmp_path, {}) == []
    with pytest.raises(ValueError):
        helper["rc_files"]("bash", tmp_path, {"AERO_SHELL_RC_FILE": "relative"})


@pytest.mark.parametrize("shell", ["sh", "bash", "zsh"])
def test_path_block_is_executable_quoted_and_idempotent(helper, tmp_path, shell):
    binary = shutil.which(shell)
    if not binary:
        pytest.skip(f"{shell} is unavailable")
    directory = tmp_path / "space ' quote $HOME * [a]"
    block = helper["path_block"](directory, shell, "production")
    result = subprocess.run(
        [binary, "-c", block + "\n" + block + '\nprintf "%s" "$PATH"'],
        env={**os.environ, "PATH": "/usr/bin:/bin"}, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == str(directory) + ":/usr/bin:/bin"


def test_backup_idempotence_and_replacement_preserve_unrelated_config(helper, tmp_path):
    path = tmp_path / ".zshrc"
    original = "# personal shell settings\nexport SOME_USER_SETTING=1\n"
    path.write_text(original)
    block = helper["path_block"](tmp_path / "bin", "zsh", "production")
    backup = helper["update_rc"](path, block, "production")
    assert Path(backup).read_text() == original
    assert path.read_text().startswith(original)
    assert helper["update_rc"](path, block, "production") is None
    changed = helper["path_block"](tmp_path / "new bin", "zsh", "production")
    helper["update_rc"](path, changed, "production")
    assert path.read_text().count("# >>> Aerolytica production PATH >>>") == 1
    assert path.read_text().startswith(original)
    testing = helper["path_block"](tmp_path / "test bin", "zsh", "testing")
    helper["update_rc"](path, testing, "testing")
    assert changed in path.read_text() and testing in path.read_text()


def test_symlink_dotfile_keeps_link_and_backs_up_real_target(helper, tmp_path):
    real = tmp_path / "dotfiles/zshrc"
    real.parent.mkdir()
    real.write_text("# tracked dotfile\n")
    link = tmp_path / ".zshrc"
    link.symlink_to(real)
    backup = helper["update_rc"](link, "# new block", "production")
    assert link.is_symlink()
    assert real.read_text().endswith("# new block\n")
    assert Path(backup).read_text() == "# tracked dotfile\n"


def test_read_only_and_broken_managed_blocks_are_not_overwritten(helper, tmp_path):
    path = tmp_path / ".zshrc"
    path.write_text("# keep\n")
    path.chmod(0o400)
    with pytest.raises(ValueError, match="只读"):
        helper["update_rc"](path, "# new", "production")
    assert path.read_text() == "# keep\n"
    path.chmod(0o600)
    old = "# >>> Aerolytica production PATH >>>\n# incomplete"
    path.write_text(old)
    with pytest.raises(ValueError, match="标记"):
        helper["update_rc"](path, "# new", "production")
    assert path.read_text() == old
    old = "# <<< Aerolytica production PATH <<<\n# >>> Aerolytica production PATH >>>"
    path.write_text(old)
    with pytest.raises(ValueError, match="顺序"):
        helper["update_rc"](path, "# new", "production")
    assert path.read_text() == old


def setup_env(tmp_path, **extra):
    return {"HOME": str(tmp_path), "AERO_BIN_DIR": str(tmp_path / "bin"),
            "AERO_ENVIRONMENT": "production", "AERO_INSTALL_SHELL": "zsh",
            "AERO_INSTALL_YES": "1", **extra}


@pytest.mark.parametrize("extra", [{}, {"AERO_SHELL_INIT": "no"}, {"CI": "true"}])
def test_noninteractive_or_refusal_does_not_write_rc(helper, tmp_path, capsys, extra):
    helper["main"](setup_env(tmp_path, **extra))
    assert not (tmp_path / ".zshrc").exists()
    output = capsys.readouterr().out
    assert "未修改 shell" in output
    assert "未配置 PATH，以下命令使用完整路径" in output
    assert f"  {tmp_path}/bin/aero setup" in output
    assert "  aero setup" not in output


@pytest.mark.parametrize("environment,command", [("production", "aero"),
                                               ("testing", "aero-testing")])
def test_configured_path_guides_loading_before_short_commands(
    helper, tmp_path, capsys, environment, command
):
    helper["main"](setup_env(tmp_path, AERO_SHELL_INIT="yes", AERO_ENVIRONMENT=environment))
    output = capsys.readouterr().out
    assert f"  source {tmp_path}/.zshrc" in output
    assert "完成上述配置加载，或重新打开 zsh 终端后执行" in output
    assert output.index("  source ") < output.index(f"  {command} setup")
    for action in ("setup", "init", "chat"):
        assert f"  {command} {action}" in output
        assert f"{tmp_path}/bin/{command} {action}" not in output
    assert "完整路径也可直接运行" not in output


@pytest.mark.parametrize("environment,command", [("production", "aero"),
                                               ("testing", "aero-testing")])
@pytest.mark.parametrize("configured", [True, False])
def test_next_steps_explain_purpose_and_when_each_command_is_needed(
    helper, tmp_path, capsys, environment, command, configured
):
    helper["main"](setup_env(
        tmp_path, AERO_SHELL_INIT="yes" if configured else "no", AERO_ENVIRONMENT=environment
    ))
    output = capsys.readouterr().out
    executable = command if configured else str(tmp_path / "bin" / command)
    assert "前面安装的是客户端本身" in output
    assert "不是重新安装客户端，无需登录" in output
    assert "不会修改系统 Python 或你的 Conda 环境" in output
    assert "首次使用时执行，已准备好可跳过" in output
    assert "每个新研究目录执行一次，已初始化可跳过" in output
    assert "请先进入你的研究目录" in output
    assert "创建项目配置及数据、图表等工作目录" in output
    assert "打开交互对话界面，用自然语言描述你要完成的任务" in output
    assert (
        output.index("1. 准备科研运行环境") < output.index(f"  {executable} setup")
        < output.index("2. 初始化研究项目") < output.index(f"  {executable} init")
        < output.index("3. 开始 AI 科研对话") < output.index(f"  {executable} chat")
    )


def test_explicit_automation_consent_and_bash_both_startup_modes(helper, tmp_path):
    helper["main"](setup_env(tmp_path, AERO_SHELL_INIT="yes", AERO_INSTALL_SHELL="bash"))
    assert (tmp_path / ".bashrc").is_file()
    assert (tmp_path / ".bash_profile").is_file()


def test_ci_never_opens_terminal_or_modifies_rc(helper, tmp_path, monkeypatch):
    def no_terminal(*args, **kwargs):
        raise AssertionError("CI must not open a controlling terminal")

    monkeypatch.setattr(builtins, "open", no_terminal)
    helper["main"](setup_env(tmp_path, AERO_INSTALL_YES="0", CI="true"))
    assert not (tmp_path / ".zshrc").exists()


def test_relative_zdotdir_is_not_silently_written_into_current_project(
    helper, tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    helper["main"](setup_env(tmp_path, AERO_SHELL_INIT="yes", ZDOTDIR="relative"))
    assert not (tmp_path / ".zshrc").exists()
    assert not (tmp_path / "relative").exists()
    output = capsys.readouterr().out
    assert f"  {tmp_path}/bin/aero setup" in output
    assert "  aero setup" not in output


def test_noninteractive_install_paths_are_configurable_without_downloads(tmp_path):
    source = (ROOT / "install.sh").read_text().split('\nUV_VERSION=', 1)[0]
    source += '\nprintf "ROOT=%s\\nBIN=%s\\n" "$AERO_INSTALL_ROOT" "$AERO_BIN_DIR"\n'
    prefix = tmp_path / "custom aero"
    directory = tmp_path / "selected bin"
    result = subprocess.run(
        ["sh", "-c", source, "installer", "--yes", "--prefix", str(prefix),
         "--bin-dir", str(directory), "--shell", "bash", "--no-shell-init"],
        env={**os.environ, "CI": "true"}, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    assert f"ROOT={prefix}/client\nBIN={directory}\n" in result.stdout
    assert not prefix.exists()


@pytest.mark.parametrize("answers,created", [("\n\n", ".zshrc"), ("bash\n\n", ".bashrc"),
                                            ("\nn\n", None)])
def test_interactive_default_shell_override_and_consent(
    helper, tmp_path, monkeypatch, capsys, answers, created
):
    original_open = builtins.open
    prompts = []

    class Writer(io.StringIO):
        def close(self):
            prompts.append(self.getvalue())
            super().close()

    def terminal(file, mode="r", **kwargs):
        if file == "/dev/tty":
            return io.StringIO(answers) if mode == "r" else Writer()
        return original_open(file, mode, **kwargs)

    monkeypatch.setattr(builtins, "open", terminal)
    helper["main"](setup_env(tmp_path, AERO_INSTALL_YES="0"))
    assert "检测到当前 shell：zsh" in "".join(prompts)
    if created:
        assert (tmp_path / created).is_file()
        assert "  aero setup" in capsys.readouterr().out
    else:
        assert not (tmp_path / ".zshrc").exists()
        assert f"  {tmp_path}/bin/aero setup" in capsys.readouterr().out


def test_piped_script_reads_path_confirmation_from_controlling_terminal(tmp_path):
    # pytest's earlier app tests may leave threads alive; fork a PTY only in a
    # fresh single-threaded interpreter, never in the macOS pytest process.
    result = subprocess.run(
        [sys.executable, __file__, "--pty-probe", str(tmp_path)],
        capture_output=True, text=True, timeout=25,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _piped_installer_probe(tmp_path):
    import pty

    script = (ROOT / "install.sh").read_text().split('\nUV_VERSION=', 1)[0]
    script += '\nprintf "SELECTED=%s\\n" "$AERO_BIN_DIR"\n'
    reader, writer = os.pipe()
    os.write(writer, script.encode())
    os.close(writer)
    pid, master = pty.fork()
    if pid == 0:
        os.dup2(reader, 0)
        os.close(reader)
        env = {k: v for k, v in os.environ.items() if k not in {
            "CI", "GITHUB_ACTIONS", "AERO_INSTALL_YES", "AERO_BIN_DIR", "AERO_INSTALL_ROOT"
        }}
        env.update(AERO_HOME=str(tmp_path / "aero"), SHELL="/bin/zsh")
        os.execvpe("sh", ["sh"], env)
    os.close(reader)
    output = b""
    sent_path = sent_yes = False
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if not select.select([master], [], [], 0.2)[0]:
                continue
            try:
                chunk = os.read(master, 65536)
            except OSError:
                break
            if not chunk:
                break
            output += chunk
            if "回车使用默认值".encode() in output and not sent_path:
                os.write(master, b"\n")
                sent_path = True
            if b"[Y/n]" in output and not sent_yes:
                os.write(master, b"\n")
                sent_yes = True
            if b"SELECTED=" in output:
                break
        assert "请输入安装地址".encode() in output, output.decode()
        assert ("SELECTED=" + str(tmp_path / "aero/bin")).encode() in output, output.decode()
    finally:
        os.close(master)
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        os.waitpid(pid, 0)


if __name__ == "__main__" and sys.argv[1:2] == ["--pty-probe"]:
    _piped_installer_probe(Path(sys.argv[2]))
