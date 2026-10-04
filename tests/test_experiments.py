"""Tests for isolated experiment workspaces."""

import pytest


def test_title_scheduling_failure_does_not_create_unscheduled_coroutine():
    from types import SimpleNamespace
    from unittest.mock import Mock

    from aero.cli.main import AeroApp

    app = SimpleNamespace(
        _session_title_workers=set(),
        _generate_session_title=Mock(),
        run_worker=Mock(side_effect=RuntimeError("UI is not running")),
    )
    AeroApp._schedule_session_title_generation(app, "session-1")

    app._generate_session_title.assert_not_called()
    assert app._session_title_workers == set()


def test_create_experiment_builds_workspace_and_tracks_active(tmp_path):
    from aero.experiments import EXPERIMENT_DIRECTORIES, ExperimentManager

    manager = ExperimentManager(tmp_path)
    experiment = manager.create("臭氧敏感性", base_checkpoint="checkpoint-1")
    workspace = manager.workspace_path(experiment)

    assert experiment["id"].startswith("exp-")
    assert experiment["base_checkpoint"] == "checkpoint-1"
    assert manager.active()["id"] == experiment["id"]
    assert workspace.parent == tmp_path / "experiments"
    for directory in EXPERIMENT_DIRECTORIES:
        assert (workspace / directory).is_dir()


def test_nested_experiment_completion_keeps_current_experiment_active(tmp_path):
    from aero.experiments import ExperimentManager

    manager = ExperimentManager(tmp_path)
    parent = manager.create("父实验")
    child = manager.create("子实验")

    completed = manager.complete(child["id"], "# 子实验报告\n\n结论。")

    assert completed["status"] == "completed"
    assert manager.active()["id"] == child["id"]
    assert manager.load(parent["id"])["status"] == "active"
    report = tmp_path / completed["report"]
    assert report.read_text(encoding="utf-8").startswith("# 子实验报告")


def test_switch_and_leave_experiment(tmp_path):
    from aero.experiments import ExperimentManager

    manager = ExperimentManager(tmp_path)
    first = manager.create("实验一")
    second = manager.create("实验二")

    assert manager.switch(first["id"])["name"] == "实验一"
    manager.leave()
    assert manager.active() is None
    assert {item["id"] for item in manager.list()} == {first["id"], second["id"]}


def test_artifacts_exclude_final_report(tmp_path):
    from aero.experiments import ExperimentManager

    manager = ExperimentManager(tmp_path)
    experiment = manager.create("产物测试")
    workspace = manager.workspace_path(experiment)
    (workspace / "scripts" / "run.py").write_text("print('ok')\n", encoding="utf-8")
    manager.complete(experiment["id"], "# 报告")

    assert manager.artifacts(experiment) == ["scripts/run.py"]


def test_delete_active_experiment_removes_workspace_and_returns_to_parent(tmp_path):
    from aero.experiments import ExperimentManager

    manager = ExperimentManager(tmp_path)
    parent = manager.create("父实验")
    child = manager.create("待删除实验")
    workspace = manager.workspace_path(child)
    (workspace / "outputs" / "result.txt").write_text("result", encoding="utf-8")

    deleted = manager.delete(child["id"])

    assert deleted["id"] == child["id"]
    assert not workspace.exists()
    assert manager.load(child["id"]) is None
    assert manager.active()["id"] == parent["id"]


def test_clear_experiments_preserves_main_project_files(tmp_path):
    from aero.experiments import ExperimentManager

    main_file = tmp_path / "main.py"
    main_file.write_text("print('main')\n", encoding="utf-8")
    manager = ExperimentManager(tmp_path)
    first = manager.create("实验一")
    second = manager.create("实验二")
    (manager.workspace_path(first) / "scripts" / "a.py").write_text("a", encoding="utf-8")
    (manager.workspace_path(second) / "figures" / "b.png").write_bytes(b"png")

    removed = manager.clear()

    assert {item["id"] for item in removed} == {first["id"], second["id"]}
    assert manager.list() == []
    assert manager.active() is None
    assert not (tmp_path / "experiments").exists()
    assert main_file.read_text(encoding="utf-8") == "print('main')\n"


def test_workspace_context_keeps_project_root_and_routes_relative_paths(tmp_path):
    from aero.toolbox.paths import (
        find_project_dir,
        find_workspace_dir,
        resolve_project_path,
        use_workspace,
    )

    project = tmp_path / "project"
    workspace = project / "experiments" / "trial"
    workspace.mkdir(parents=True)

    with use_workspace(project, workspace):
        assert find_project_dir() == project
        assert find_workspace_dir() == workspace
        assert resolve_project_path("figures/result.png") == workspace / "figures/result.png"


@pytest.mark.asyncio
async def test_file_tool_writes_inside_active_experiment(tmp_path):
    from aero.toolbox.file_access import READ_FILES
    from aero.toolbox.paths import use_workspace
    from aero.toolbox.tools.files import write_file

    project = tmp_path / "project"
    workspace = project / "experiments" / "trial"
    workspace.mkdir(parents=True)
    READ_FILES.clear()

    with use_workspace(project, workspace):
        result = await write_file("scripts/analyze.py", "print('experiment')\n")

    assert result["status"] == "success"
    assert (workspace / "scripts" / "analyze.py").is_file()
    assert not (project / "scripts" / "analyze.py").exists()


def test_experiment_report_prompt_uses_conversation_and_artifacts():
    from aero.cli.main import _experiment_report_prompt
    from aero.core.types import Message

    prompt = _experiment_report_prompt(
        {"name": "排放敏感性"},
        [
            Message(role="user", content="比较两种排放方案"),
            Message(role="assistant", content="方案 A 的臭氧峰值更低"),
        ],
        ["figures/comparison.png", "scripts/analyze.py"],
    )

    assert "排放敏感性" in prompt
    assert "方案 A 的臭氧峰值更低" in prompt
    assert "figures/comparison.png" in prompt
    assert "不得编造" in prompt


def test_finish_command_shows_progress_before_scheduling_worker():
    from types import SimpleNamespace

    from aero.cli.main import AeroApp

    app = AeroApp.__new__(AeroApp)
    experiment = {"id": "exp-1", "name": "臭氧实验"}
    app._experiment_finish_worker = None
    app._get_experiment_mgr = lambda: SimpleNamespace(active=lambda: experiment)
    shown = []
    footer = []
    app._show_checkpoint_message = lambda message, **kwargs: shown.append((message, kwargs))
    app._set_footer_status = footer.append

    def schedule(coroutine, **kwargs):
        coroutine.close()
        return SimpleNamespace(is_running=True)

    app.run_worker = schedule

    app._start_finishing_active_experiment()

    assert shown == [("正在总结实验 **臭氧实验** 并生成文字报告…", {"force_scroll": True})]
    assert footer == ["正在完成实验：臭氧实验"]
    assert app._experiment_finish_worker.is_running is True


def test_cli_checkpoint_manager_follows_active_experiment(tmp_path):
    from aero.cli.main import AeroApp
    from aero.experiments import ExperimentManager

    app = AeroApp.__new__(AeroApp)
    app._project_dir = tmp_path
    app._checkpoint_mgr = None
    app._experiment_mgr = ExperimentManager(tmp_path)
    experiment = app._experiment_mgr.create("边界层实验")

    experiment_manager = app._get_checkpoint_mgr()

    assert experiment_manager.project_dir == app._experiment_mgr.workspace_path(experiment)
    assert experiment_manager.experiment_id == experiment["id"]

    app._experiment_mgr.leave()
    assert app._get_checkpoint_mgr().project_dir == tmp_path
    assert app._get_checkpoint_mgr().experiment_id is None


def test_finish_creates_related_checkpoint_in_main_scope(tmp_path):
    from types import SimpleNamespace

    from aero.cli.main import AeroApp

    (tmp_path / "main.py").write_text("print('main')\n", encoding="utf-8")
    app = AeroApp.__new__(AeroApp)
    app._project_dir = tmp_path
    app._checkpoint_mgr = None
    app._session_id = None
    app.config = SimpleNamespace(
        llm=SimpleNamespace(provider="deepseek", model="model"),
        vision=SimpleNamespace(model="vision"),
        mode="execute",
    )
    app._get_experiment_session_mgr = lambda: SimpleNamespace(snapshot=lambda _slot: None)

    checkpoint = app._create_experiment_finish_checkpoint(
        {
            "id": "exp-1",
            "name": "臭氧实验",
            "report": "experiments/ozone/reports/final-report.md",
        }
    )

    assert checkpoint["name"] == "实验结束：臭氧实验"
    assert checkpoint["kind"] == "experiment-finish"
    assert checkpoint["scope"] == "main"
    assert checkpoint["related_experiment"]["id"] == "exp-1"


def test_experiment_list_forces_chat_to_scroll_to_result():
    from types import SimpleNamespace

    from aero.cli.main import AeroApp

    app = AeroApp.__new__(AeroApp)
    experiment = {
        "id": "exp-1",
        "name": "臭氧实验",
        "status": "active",
        "updated_at": 1,
    }
    manager = SimpleNamespace(
        list=lambda: [experiment],
        active=lambda: experiment,
    )
    app._get_experiment_mgr = lambda: manager
    shown = []
    app._show_checkpoint_message = lambda message, **kwargs: shown.append((message, kwargs))

    app._show_experiment_list()

    assert "臭氧实验" in shown[0][0]
    assert shown[0][1] == {"force_scroll": True}


@pytest.mark.asyncio
async def test_experiment_list_is_restored_with_continued_session(tmp_path):
    from aero.agent.session import SessionManager
    from aero.cli.main import AeroApp, ChatMarkdown
    from aero.core.config import AeroConfig
    from aero.core.types import Message
    from aero.experiments import ExperimentManager

    experiment_manager = ExperimentManager(tmp_path)
    experiment_manager.create("臭氧敏感性实验")
    experiment_manager.leave()
    session_manager = SessionManager(tmp_path / "sessions")

    first = AeroApp(AeroConfig(), persist_config=False)
    first._project_dir = tmp_path
    first._experiment_mgr = experiment_manager
    first._session_mgr = session_manager
    async with first.run_test(size=(100, 30)) as pilot:
        # Display-only interactions are intentionally not standalone persisted chats.
        first.agent.messages.extend(
            [
                Message("user", "继续研究"),
                Message("assistant", "好的，查看已有实验。"),
            ]
        )
        first._show_experiment_list()
        await pilot.pause(0.2)
        first._auto_save_session()
        session_id = first._session_id

        assert session_id is not None
        assert all("臭氧敏感性实验" not in message.content for message in first.agent.messages)

    continued = AeroApp(
        AeroConfig(),
        persist_config=False,
        resume_last_session=True,
    )
    continued._project_dir = tmp_path
    continued._experiment_mgr = experiment_manager
    continued._session_mgr = session_manager
    async with continued.run_test(size=(100, 30)) as pilot:
        await pilot.pause(0.2)
        markdown = "\n".join(
            str(getattr(widget, "_markdown", "")) for widget in continued.query(ChatMarkdown)
        )

        assert continued._session_id == session_id
        assert "臭氧敏感性实验" in markdown
        assert all("臭氧敏感性实验" not in message.content for message in continued.agent.messages)
