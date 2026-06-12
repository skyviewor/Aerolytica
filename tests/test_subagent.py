from aero.agent.subagent import (
    SubAgentManager,
    cancel_subagent_from_context,
    launch_subagent_from_context,
    query_subagents_from_context,
    use_subagent_canceller,
    use_subagent_launcher,
    use_subagent_status_provider,
)
from aero.cli.main import (
    _collect_artifact_paths,
    _requests_background_execution,
    _subagent_title_from_text,
)


def test_subagent_manager_rotates_one_footer_task_at_a_time():
    manager = SubAgentManager()
    for index in range(5):
        task = manager.create(title=f"任务 {index}", description="下载并绘图")
        manager.update(task.id, f"步骤 {index}")

    text = manager.footer_text(frame=0)

    assert "#1 步骤 0" in text
    assert "#2" not in text
    assert "#5" not in text
    assert "|" not in text

    rotated = manager.footer_text(frame=12)
    assert "#2 步骤 1" in rotated


def test_subagent_manager_finishes_and_removes_from_footer():
    manager = SubAgentManager()
    task = manager.create(title="下载 ERA5", description="下载")

    manager.finish(task.id, result_summary="完成", artifacts=["figures/a.png"])

    assert manager.active() == []
    assert manager.footer_text(0) == ""
    assert manager.get(task.id).artifacts == ["figures/a.png"]


def test_subagent_manager_snapshot_reports_live_status():
    manager = SubAgentManager()
    task = manager.create(title="下载 ERA5", description="下载")
    manager.update(task.id, "下载 40%")

    snapshot = manager.snapshot()

    assert snapshot["status"] == "success"
    assert snapshot["active_count"] == 1
    assert snapshot["tasks"][0]["latest_status"] == "下载 40%"
    assert snapshot["tasks"][0]["requires_confirmation"] is False


def test_launch_subagent_bridge_uses_context_launcher():
    calls = []

    def launcher(title, task, success_criteria, context_summary):
        calls.append((title, task, success_criteria, context_summary))
        return {"status": "started", "task_id": "1"}

    with use_subagent_launcher(launcher):
        result = launch_subagent_from_context("下载", "下载数据", "生成图", "上下文")

    assert result == {"status": "started", "task_id": "1"}
    assert calls == [("下载", "下载数据", "生成图", "上下文")]


def test_launch_subagent_bridge_reports_unavailable_without_context():
    result = launch_subagent_from_context("下载", "下载数据", "", "")

    assert result["status"] == "unavailable"


def test_query_subagent_bridge_uses_context_provider():
    with use_subagent_status_provider(lambda task_id=None: {"task_id": task_id}):
        result = query_subagents_from_context("7")

    assert result == {"task_id": "7"}


def test_query_subagent_bridge_reports_unavailable_without_context():
    result = query_subagents_from_context()

    assert result["status"] == "unavailable"


def test_cancel_subagent_bridge_uses_context_canceller():
    with use_subagent_canceller(lambda task_id: {"cancelled": task_id}):
        result = cancel_subagent_from_context("3")

    assert result == {"cancelled": "3"}


def test_cancel_subagent_bridge_reports_unavailable_without_context():
    result = cancel_subagent_from_context("3")

    assert result["status"] == "unavailable"


def test_subagent_text_helpers_extract_artifacts_and_titles():
    text = (
        "已保存 figures/plot.png，并下载 data/era5/sample.nc。\n"
        "文件路径：`data/cds_era5-single-levels_total_precipitation_sfc_20190704.nc`"
        "（约 22 MB，NetCDF4 格式）"
    )

    assert _collect_artifact_paths(text) == [
        "figures/plot.png",
        "data/era5/sample.nc",
        "data/cds_era5-single-levels_total_precipitation_sfc_20190704.nc",
    ]
    assert (
        _subagent_title_from_text("下载 ERA5 后计算月均值并画图", max_len=8)
        == "下载 ERA5 …"
    )


def test_background_execution_detection_ignores_status_queries():
    assert (
        _requests_background_execution("帮我查文献并生成报告，在后台运行。")
        is True
    )
    assert _requests_background_execution("交给后台处理") is True
    assert _requests_background_execution("后台任务现在是什么状态？") is False
    assert _requests_background_execution("取消后台任务 3") is False
