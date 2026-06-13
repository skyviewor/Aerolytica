"""Tests for Aero agent modules."""

import pytest

from aero.agent.session import SessionManager
from aero.agent.system_prompt import build_system_prompt
from aero.core.config import AeroConfig
from aero.core.types import Message, ToolCall


def test_session_save_load(tmp_path):
    from aero.agent.session import SessionMeta

    sm = SessionManager(tmp_path)
    messages = [
        Message(role="system", content="You are a helpful assistant."),
        Message(role="user", content="Hello"),
        Message(
            role="assistant",
            content="Hi!",
            tool_calls=[ToolCall(id="c1", name="test", arguments={"x": 1})],
        ),
        Message(role="tool", content='{"result":"ok"}', tool_call_id="c1"),
    ]
    sm.save("test-session", messages, SessionMeta(name="测试会话", title_source="auto"))
    loaded = sm.load("test-session")
    assert loaded is not None
    loaded_messages, meta = loaded
    assert meta.id == "test-session"
    assert meta.name == "测试会话"
    assert meta.title_source == "auto"
    assert meta.message_count == len(messages)
    assert len(loaded_messages) == 4
    assert loaded_messages[0].role == "system"
    assert loaded_messages[2].tool_calls[0].name == "test"
    assert loaded_messages[3].tool_call_id == "c1"


def test_sanitize_tool_message_sequence_drops_orphan_tool_messages():
    from aero.agent.loop import _sanitize_tool_message_sequence

    messages = [
        Message(role="system", content="system"),
        Message(role="tool", content="orphan", tool_call_id="missing"),
        Message(role="assistant", content=""),
        Message(
            role="assistant", content="", tool_calls=[ToolCall(id="c1", name="test", arguments={})]
        ),
        Message(role="tool", content="ok", tool_call_id="c1"),
        Message(role="tool", content="extra", tool_call_id="extra"),
        Message(role="assistant", content="done"),
    ]

    sanitized = _sanitize_tool_message_sequence(messages)

    assert [(m.role, m.content) for m in sanitized] == [
        ("system", "system"),
        ("assistant", ""),
        ("assistant", ""),
        ("tool", "ok"),
        ("assistant", "done"),
    ]


def test_sanitize_tool_message_sequence_drops_incomplete_tool_call_blocks():
    from aero.agent.loop import _sanitize_tool_message_sequence

    messages = [
        Message(role="system", content="system"),
        Message(
            role="assistant",
            content="starting",
            tool_calls=[ToolCall(id="c1", name="launch_sub_agent", arguments={})],
        ),
        Message(role="assistant", content="后台任务完成摘要"),
        Message(role="tool", content="late", tool_call_id="c1"),
        Message(role="user", content="next"),
    ]

    sanitized = _sanitize_tool_message_sequence(messages)

    assert [(m.role, m.content, bool(m.tool_calls)) for m in sanitized] == [
        ("system", "system", False),
        ("assistant", "starting", False),
        ("assistant", "后台任务完成摘要", False),
        ("user", "next", False),
    ]


def test_session_list(tmp_path):
    sm = SessionManager(tmp_path)
    assert sm.list_sessions() == []
    sm.save("s1", [Message(role="user", content="a")])
    sm.save("s2", [Message(role="user", content="b")])
    sessions = sm.list_sessions()
    assert len(sessions) == 2
    assert {s.id for s in sessions} == {"s1", "s2"}


def test_session_delete_removes_from_index(tmp_path):
    sm = SessionManager(tmp_path)
    sm.save("s1", [Message(role="user", content="a")])
    sm.save("s2", [Message(role="user", content="b")])

    assert sm.delete("s1") is True

    sessions = sm.list_sessions()
    assert {s.id for s in sessions} == {"s2"}
    assert sm.load("s1") is None


def test_build_system_prompt():
    config = AeroConfig.create_default()
    prompt = build_system_prompt(config)
    assert "数据库" in prompt or "data" in prompt.lower()
    assert "必须优先调用工具箱里的工具" in prompt
    assert "检查这个数据的内容" in prompt
    assert "不要先写 Python/xarray 脚本" in prompt
    assert "必须先查询统一数据集目录" in prompt
    assert "download_tool 为唯一事实来源" in prompt
    assert "不要依赖系统提示中的静态名单" in prompt
    assert "NCEP Reanalysis 变量优先通过统一数据集变量查询能力确认" in prompt
    assert "允许用 run_shell、源站元数据或自定义分析兜底" in prompt
    assert "不要为 GFS/NOMADS/AWS/CDS/CAMS/ADS 下载编写 Python HTTP/Range/下载脚本" in prompt
    assert "不要用 `cdsapi.Client`、`urllib`、`requests`、`curl`" in prompt
    assert "curl、wget、aria2c" in prompt
    assert "必须调用 inspect_gfs_inventory" in prompt
    assert "不要用 run_shell 执行 curl/grep/head 去查看 NOMADS 或 AWS 的 `.idx` 文件" in prompt
    assert "不要跳过 CLI 直接写 Python/cfgrib/xarray 脚本" in prompt
    assert "GRIB/GRIB2/NetCDF 做合并、转换" in prompt
    assert "必须优先通过 run_shell 使用成熟命令行工具" in prompt
    assert "运行这些命令前，先为本次需要的具体命令调用 ensure_runtime_tools" in prompt
    assert "不要因为 `which` 在 base conda 或用户其他环境里找到了同名命令就直接使用" in prompt
    assert "先安装并尝试 CLI，只有 CLI 不适合或失败时才用 Python 兜底" in prompt
    assert "所有通过 run_shell 执行的 Python 程序" in prompt
    assert "都必须来自 Aero 统一的 `aero-agent` conda 环境" in prompt
    assert "`aero-agent` conda 环境" in prompt
    assert "参考资料" in prompt
    assert "source_url" in prompt
    assert "引用参考文献" in prompt
    assert "原样粘贴页面上的两行官方配置" in prompt
    assert "禁止猜测、查找、cat、read_file 或用 Python 读取 Aero 密钥文件" in prompt
    assert "SST 自动换成 TMP:surface" in prompt
    assert "analyze_image" in prompt
    assert "list_figures" in prompt
    assert "视觉模型" in prompt
    assert "check_vision_model_config" in prompt
    assert "当前轮没有成功调用" in prompt
    assert "禁止写任何图片/图表的视觉解读" in prompt
    assert "降水/云/要素集中在哪里" in prompt
    assert "生成图或改图后必须把图片嵌入对话框" in prompt
    assert "只要用户明确说“打开图片 / 打开这张图 / 帮我打开图”等自然表达" in prompt
    assert "但即使调用了 `preview_image`" in prompt
    assert "不要用 run_shell/curl/head/grep 查看 ADS 网页" in prompt
    assert "不要自己写 cdsapi/urllib/requests 下载脚本" in prompt
    assert "figures/precip_2023.png" in prompt
    assert "data/precip_2023.png" not in prompt
    assert "只有一个数据源——CDS" in prompt
    assert "没有源切换" in prompt
    assert "没有源切换" in prompt
    assert "CDS" in prompt
    assert "subset_netcdf" in prompt


def test_build_system_prompt_prefers_tools_in_english():
    config = AeroConfig.create_default()
    prompt = build_system_prompt(config, language="en")
    assert "use that tool first" in prompt
    assert "local NetCDF contents" in prompt
    assert "Do not write a Python/xarray script" in prompt
    assert "query the unified dataset catalogue first" in prompt
    assert "`download_tool` as the source of truth" in prompt
    assert "Do not rely on a memorized static list" in prompt
    assert "use the unified dataset-variable query first" in prompt
    assert "using run_shell, source metadata, or custom analysis as a fallback is allowed" in prompt
    assert "Do NOT write Python HTTP/Range/download scripts" in prompt
    assert "Do NOT use `cdsapi.Client`, `urllib`, `requests`, `curl`" in prompt
    assert "`curl`, `wget`, `aria2c`" in prompt
    assert "call inspect_gfs_inventory" in prompt
    assert "Do NOT use run_shell with" in prompt
    assert "Do not skip directly to a Python/cfgrib/xarray script" in prompt
    assert "GRIB/GRIB2/NetCDF merging, conversion" in prompt
    assert "prefer established command-line tools" in prompt
    assert "call ensure_runtime_tools" in prompt
    assert "Do NOT rely on `which` finding a command in base conda" in prompt
    assert "then use Python only as an explicit fallback when CLI is unsuitable or fails" in prompt
    assert "Any Python program run through run_shell" in prompt
    assert "MUST execute from Aero's unified `aero-agent` conda environment" in prompt
    assert "`aero-agent` conda environment" in prompt
    assert "References" in prompt
    assert "source_url" in prompt
    assert "Citing references" in prompt
    assert "official two-line configuration" in prompt
    assert "Never inspect, guess, find, cat, read_file, or run Python against Aero secret files" in prompt
    assert "surface temperature for SST" in prompt
    assert "analyze_image" in prompt
    assert "list_figures" in prompt
    assert "vision" in prompt
    assert "check_vision_model_config" in prompt
    assert "Without a successful `analyze_image` call in the current turn" in prompt
    assert "do not write any visual interpretation" in prompt
    assert "where precipitation/clouds/features are concentrated" in prompt
    assert "generated or revised figures must appear inline in the chat via Markdown image syntax" in prompt
    assert "Never omit the inline image" in prompt
    assert "Call `preview_image` when the user explicitly asks to open the image" in prompt
    assert "do not inspect ADS\n    pages with run_shell/curl/head/grep" in prompt
    assert "do not write your own cdsapi/urllib/requests\n    downloader" in prompt
    assert "figures/precip_2023.png" in prompt
    assert "data/precip_2023.png" not in prompt
    assert "only ONE data source" in prompt
    assert "No AWS, no GCS, no source switching" in prompt
    assert "CDS" in prompt
    assert "subset_netcdf" in prompt


def test_build_system_prompt_injects_selected_skill_context():
    config = AeroConfig.create_default()
    prompt = build_system_prompt(
        config,
        skill_context="### scientific-plotting\nUse publication-quality labels.",
    )

    assert "当前启用的 Skill 指导" in prompt
    assert "scientific-plotting" in prompt
    assert "Use publication-quality labels." in prompt


def test_user_facing_text_hides_internal_tool_names():
    from aero.agent.loop import _sanitize_user_facing_text

    text = (
        "你可以用 `inspect_nc` 查看具体内容，也可以调用 download_era5。"
        "需要裁剪时可以调用 subset_netcdf。"
        "你可以通过 `list_literature` 查看所有已保存的论文。"
    )
    sanitized = _sanitize_user_facing_text(text)

    assert "inspect_nc" not in sanitized
    assert "download_era5" not in sanitized
    assert "subset_netcdf" not in sanitized
    assert "list_literature" not in sanitized
    assert "你可以让我继续查看具体内容" in sanitized
    assert "你可以让我查看所有已保存的论文" in sanitized
    assert "调用 下载数据" not in sanitized
    assert "下载数据" in sanitized


def test_user_facing_text_hides_cams_tool_and_parameter_names():
    from aero.agent.loop import _sanitize_user_facing_text

    text = (
        "下载时通过 `download_cams` 工具，指定 `dataset_id` 为 "
        "`cams-global-reanalysis-eac4`，选择对应变量即可。"
    )

    sanitized = _sanitize_user_facing_text(text)

    assert sanitized == "下载时选择 CAMS EAC4 再分析数据集，选择对应变量即可。"
    assert "download_cams" not in sanitized
    assert "dataset_id" not in sanitized
    assert "cams-global-reanalysis-eac4" not in sanitized


def test_user_facing_text_hides_cams_tool_download_phrase():
    from aero.agent.loop import _sanitize_user_facing_text

    text = "CAMS 全球大气成分预报数据我是支持的，通过 `download_cams` 工具就能下载。"

    sanitized = _sanitize_user_facing_text(text)

    assert sanitized == "CAMS 全球大气成分预报数据我是支持的，我可以直接帮你下载。"
    assert "download_cams" not in sanitized
    assert "工具" not in sanitized


def test_user_facing_text_does_not_corrupt_paths_or_request_keys():
    from aero.agent.loop import _sanitize_user_facing_text

    text = (
        "正在下载 CAMS 文件："
        "data/cams_cams-global-atmospheric-composition-forecasts_"
        "particulate_matter_2.5um_20260612.nc\n"
        "{'data_format': ['netcdf_zip'], "
        "'dataset_id': 'cams-global-atmospheric-composition-forecasts'}"
    )

    sanitized = _sanitize_user_facing_text(text)

    assert "data/cams_cams-global-atmospheric-composition-forecasts_" in sanitized
    assert "data_format" in sanitized
    assert "dataset_id" in sanitized
    assert "CAMS 全球大气成分预报数据集_particulate" not in sanitized
    assert "数据格式" not in sanitized


def test_streaming_text_sanitizer_hides_split_tool_names():
    from aero.agent.loop import _StreamingTextSanitizer

    sanitizer = _StreamingTextSanitizer()
    chunks = [
        "你可以通过 `li",
        "st_litera",
        "ture` 查看所有已保存的论文。",
    ]

    streamed = "".join(sanitizer.push(chunk) for chunk in chunks)
    streamed += sanitizer.flush()

    assert "list_literature" not in streamed
    assert "你可以让我查看所有已保存的论文" in streamed


def test_streaming_text_sanitizer_hides_split_cams_tool_phrase():
    from aero.agent.loop import _StreamingTextSanitizer

    sanitizer = _StreamingTextSanitizer()
    chunks = [
        "CAMS 全球大气成分预报数据我是支持的，通过 `down",
        "load_",
        "cams` 工具就能下载。",
    ]

    streamed = "".join(sanitizer.push(chunk) for chunk in chunks)
    streamed += sanitizer.flush()

    assert "download_cams" not in streamed
    assert "工具就能下载" not in streamed
    assert "我可以直接帮你下载" in streamed


def test_progress_text_hides_internal_tool_names():
    from aero.agent.loop import _sanitize_progress_text

    cases = {
        "调用工具: retry_download": "开始重试下载",
        "调用工具: describe_dataset": "正在查看数据集信息",
        "调用工具: search_dataset_variables": "正在查询数据集变量",
        "调用工具: download_dataset": "准备下载数据集",
        "调用工具: download_era5": "准备下载数据",
        "调用工具: inspect_grib2": "正在查看 GRIB2 文件内容",
        "调用工具: search_gfs_variables": "正在查询 GFS 可用要素",
        "调用工具: check_gfs_availability": "正在检查 GFS 可用时次",
        "调用工具: inspect_gfs_inventory": "正在查看 GFS 文件库存",
        "调用工具: check_vision_model_config": "正在检查视觉模型配置",
        "调用工具: launch_sub_agent": "正在转交后台任务",
        "调用工具: query_sub_agents": "正在查询后台任务状态",
        "调用工具: cancel_sub_agent": "正在取消后台任务",
        "工具完成: lookup_gfs_parameter": "GFS 要素定义查询完成",
        "工具完成: check_vision_model_config": "视觉模型配置检查完成",
        "工具完成: launch_sub_agent": "后台任务已启动",
        "工具完成: query_sub_agents": "后台任务状态查询完成",
        "工具完成: cancel_sub_agent": "后台任务已取消",
        "工具完成: download_dataset": "数据集下载完成",
        "工具失败: download_gfs: bad": "GFS 预报数据下载失败: bad",
        "工具失败: describe_dataset: bad": "查看数据集信息失败: bad",
        "工具失败: search_dataset_variables: bad": "查询数据集变量失败: bad",
        "工具失败: check_vision_model_config: bad": "检查视觉模型配置失败: bad",
        "工具失败: launch_sub_agent: bad": "后台任务转交失败: bad",
        "工具失败: query_sub_agents: bad": "后台任务状态查询失败: bad",
        "工具失败: cancel_sub_agent: bad": "后台任务取消失败: bad",
        "工具完成: retry_download": "重试下载已完成",
        "工具失败: inspect_nc: bad": "查看文件内容失败: bad",
        "检测到后续工具调用，继续执行...": "继续处理后续步骤...",
        "URL 可能已过期，建议按原始参数重新提交 download_era5。": (
            "URL 可能已过期，建议按原始参数重新提交下载。"
        ),
    }

    for raw, expected in cases.items():
        sanitized = _sanitize_progress_text(raw)
        assert expected == sanitized
        assert "download_era5" not in sanitized
        assert "describe_dataset" not in sanitized
        assert "search_dataset_variables" not in sanitized
        assert "download_dataset" not in sanitized
        assert "inspect_grib2" not in sanitized
        assert "search_gfs_variables" not in sanitized
        assert "check_gfs_availability" not in sanitized
        assert "inspect_gfs_inventory" not in sanitized
        assert "check_vision_model_config" not in sanitized
        assert "lookup_gfs_parameter" not in sanitized
        assert "download_gfs" not in sanitized
        assert "retry_download" not in sanitized
        assert "inspect_nc" not in sanitized
        assert "launch_sub_agent" not in sanitized
        assert "query_sub_agents" not in sanitized
        assert "cancel_sub_agent" not in sanitized


def test_unknown_tool_progress_does_not_expose_internal_name():
    from aero.agent.loop import _tool_progress_message

    assert _tool_progress_message("new_internal_tool", "start") == "正在执行当前步骤"
    assert _tool_progress_message("new_internal_tool", "done") == "执行当前步骤完成"
    assert _tool_progress_message("new_internal_tool", "error") == "执行当前步骤失败"


def test_run_shell_progress_includes_command():
    from aero.agent.loop import _tool_progress_message

    args = {"command": "cdo -f nc copy data/in.grib2 data/out.nc"}

    assert (
        _tool_progress_message("run_shell", "start", args)
        == "正在执行命令：cdo -f nc copy data/in.grib2 data/out.nc"
    )
    assert _tool_progress_message("run_shell", "done", args) == "命令执行完成"


def test_run_shell_progress_truncates_long_command():
    from aero.agent.loop import _tool_progress_message

    command = "curl " + " ".join(f"https://example.com/file-{i}.grib2" for i in range(20))

    message = _tool_progress_message("run_shell", "start", {"command": command})

    assert message.startswith("正在执行命令：curl https://example.com/file-0.grib2")
    assert message.endswith("…")
    assert len(message) < len("正在执行命令：" + command)


def test_run_shell_progress_hides_missing_guessed_cd():
    from aero.agent.loop import _tool_progress_message

    message = _tool_progress_message(
        "run_shell",
        "start",
        {"command": "cd /home/user && python scripts/tmp/plot.py"},
    )

    assert message == "正在执行命令：python scripts/tmp/plot.py"


def test_run_shell_progress_redacts_inline_secrets():
    from aero.agent.loop import _tool_progress_message

    message = _tool_progress_message(
        "run_shell",
        "start",
        {
            "command": (
                "python3 -c \"import cdsapi; cdsapi.Client("
                "url='https://ads.atmosphere.copernicus.eu/api', "
                "key='ee3a913f-03e7-4c83-a3b9-ed422aa0e091')\""
            )
        },
    )

    assert "ee3a913f" not in message
    assert "key='***'" in message


def test_read_only_python_comparison_does_not_need_confirmation():
    from aero.agent import loop

    command = """python - <<'PY'
vals_pos = vals[vals > 0]
print(vals_pos.min(), vals_pos.max())
PY"""

    assert loop._tool_call_needs_confirmation("run_shell", {"command": command}) is False


def test_read_only_python_csv_summary_does_not_need_confirmation():
    from aero.agent import loop

    command = """cd /project && python - <<'PY'
import csv
with open("data.csv") as source:
    values = [float(row["temperature_c"]) for row in csv.DictReader(source)]
print(min(values), max(values))
PY"""

    assert loop._tool_call_needs_confirmation("run_shell", {"command": command}) is False


def test_python_writes_need_confirmation():
    from aero.agent import loop

    assert (
        loop._tool_call_needs_confirmation(
            "run_shell",
            {"command": """python - <<'PY'\nPath("out.txt").write_text("value")\nPY"""},
        )
        is True
    )
    assert (
        loop._tool_call_needs_confirmation(
            "run_shell",
            {"command": """python - <<'PY'\nopen("out.txt", "w").write("value")\nPY"""},
        )
        is True
    )


def test_nested_destructive_shell_command_needs_confirmation():
    from aero.agent import loop

    assert (
        loop._tool_call_needs_confirmation(
            "run_shell",
            {"command": "cd /project && rm data.csv"},
        )
        is True
    )


def test_real_shell_redirect_needs_confirmation():
    from aero.agent import loop

    assert (
        loop._tool_call_needs_confirmation(
            "run_shell",
            {"command": "python summary.py > output.txt"},
        )
        is True
    )


def test_existing_mkdir_parents_does_not_need_confirmation(tmp_path):
    from aero.agent import loop

    (tmp_path / "figures").mkdir()
    (tmp_path / "scripts" / "tmp").mkdir(parents=True)

    assert (
        loop._tool_call_needs_confirmation(
            "run_shell",
            {
                "command": "mkdir -p figures scripts/tmp",
                "workdir": str(tmp_path),
            },
        )
        is False
    )


def test_missing_mkdir_target_needs_confirmation(tmp_path):
    from aero.agent import loop

    (tmp_path / "figures").mkdir()

    assert (
        loop._tool_call_needs_confirmation(
            "run_shell",
            {
                "command": "mkdir -p figures scripts/tmp",
                "workdir": str(tmp_path),
            },
        )
        is True
    )


def test_existing_mkdir_with_another_write_still_needs_confirmation(tmp_path):
    from aero.agent import loop

    (tmp_path / "figures").mkdir()

    assert (
        loop._tool_call_needs_confirmation(
            "run_shell",
            {
                "command": "mkdir -p figures && touch figures/result.png",
                "workdir": str(tmp_path),
            },
        )
        is True
    )


def test_streamed_shell_output_reuses_status_lines():
    from aero.cli.main import _display_status_line, _is_same_status_slot, _status_progress_slot

    assert _status_progress_slot("stdout: collecting cartopy") is None
    assert _status_progress_slot("stderr: warning from pip") is None
    assert _display_status_line("stdout: collecting cartopy") == "命令输出：collecting cartopy"
    assert _display_status_line("stderr: warning from pip") == "命令日志：warning from pip"
    assert _is_same_status_slot("stdout: one", "stdout: two") is False
    assert _is_same_status_slot("stderr: one", "stderr: two") is False
    assert _is_same_status_slot("stdout: one", "stderr: two") is False


def test_stderr_display_distinguishes_logs_from_errors():
    from aero.cli.main import _display_status_line

    assert _display_status_line("stderr: 2026-06-12 23:39:07,972 INFO Request ID is abc") == (
        "命令日志：2026-06-12 23:39:07,972 INFO Request ID is abc"
    )
    assert _display_status_line("stderr: 2026-06-12 23:39:30,326 INFO status has been updated to running") == (
        "命令日志：2026-06-12 23:39:30,326 INFO status has been updated to running"
    )
    assert _display_status_line("stderr: Download completed!") == "命令日志：Download completed!"
    assert _display_status_line("stderr: Traceback (most recent call last):").startswith("错误输出：")
    assert _display_status_line("stderr: /bin/bash: mamba: No such file or directory").startswith(
        "错误输出："
    )


def test_reference_injection_uses_markdown_text_links():
    from aero.agent.loop import _inject_refs_if_missing

    text = _inject_refs_if_missing(
        "下载前提\n\n需要先配置 ADS 凭证。",
        [
            "https://ads.atmosphere.copernicus.eu/",
            "https://ads.atmosphere.copernicus.eu/datasets/cams-global-reanalysis-eac4?tab=download",
        ],
    )

    assert "参考资料" in text
    assert (
        "1. [CAMS EAC4 数据集下载页]"
        "(https://ads.atmosphere.copernicus.eu/datasets/"
        "cams-global-reanalysis-eac4?tab=download)"
    ) in text
    assert "Copernicus ADS" not in text
    assert "ads.atmosphere.copernicus.eu：" not in text
    assert "\n   <https://" not in text


def test_existing_markdown_reference_links_are_normalized():
    from aero.agent.loop import _inject_refs_if_missing

    text = _inject_refs_if_missing(
        "参考资料\n"
        "- [ECMWF Parameter Database](https://codes.ecmwf.int/grib/param-db/?filter=pm)\n"
        "- [ECMWF Parameter Database #2](https://codes.ecmwf.int/grib/param-db/?filter=o3)",
        [],
    )

    assert text == (
        "参考资料\n"
        "1. [ECMWF Parameter Database](https://codes.ecmwf.int/grib/param-db/?filter=pm)\n"
        "2. [ECMWF Parameter Database](https://codes.ecmwf.int/grib/param-db/?filter=o3)"
    )


def test_reference_links_encode_spaces_in_urls():
    from aero.agent.loop import _inject_refs_if_missing

    text = _inject_refs_if_missing(
        "参考资料\n"
        "- [ECMWF Parameter Database]"
        "(https://codes.ecmwf.int/grib/param-db/?filter=total column ozone)",
        [],
    )

    assert text == (
        "参考资料\n"
        "1. [ECMWF Parameter Database]"
        "(https://codes.ecmwf.int/grib/param-db/?filter=total%20column%20ozone)"
    )

    injected = _inject_refs_if_missing(
        "请看这个变量说明。",
        ["https://codes.ecmwf.int/grib/param-db/?filter=total%20column%20ozone"],
    )

    assert "total%20column%20ozone" in injected
    assert "total%2520column%2520ozone" not in injected


def test_reference_links_normalize_cams_dataset_title_urls():
    from aero.agent.loop import _inject_refs_if_missing

    text = _inject_refs_if_missing(
        "参考资料\n"
        "1. [CAMS 全球大气成分预报数据集]"
        "(https://ads.atmosphere.copernicus.eu/datasets/CAMS 全球大气成分预报数据集?tab=download)",
        [],
    )

    assert text == (
        "参考资料\n"
        "1. [CAMS 全球大气成分预报数据集]"
        "(https://ads.atmosphere.copernicus.eu/datasets/"
        "cams-global-atmospheric-composition-forecasts?tab=download)"
    )
    assert " " not in text.split("](", 1)[1].split(")", 1)[0]
    assert "全球大气成分预报" not in text.split("](", 1)[1].split(")", 1)[0]


def test_existing_plain_reference_urls_are_converted_to_markdown_links():
    from aero.agent.loop import _inject_refs_if_missing

    text = _inject_refs_if_missing(
        "参考资料\n"
        "1. CAMS 全球再分析 EAC4 数据页 https://ads.atmosphere.copernicus.eu/datasets/"
        "cams-global-reanalysis-eac4?tab=download",
        [],
    )

    assert text == (
        "参考资料\n"
        "1. [CAMS 全球再分析 EAC4 数据页]"
        "(https://ads.atmosphere.copernicus.eu/datasets/"
        "cams-global-reanalysis-eac4?tab=download)"
    )


def test_live_download_progress_lines_stay_below_regular_progress_messages():
    from types import SimpleNamespace

    from aero.cli.main import AeroApp

    panel = SimpleNamespace(lines=[])

    def upsert(text: str) -> bool:
        return AeroApp._upsert_status_line(None, panel, text)

    upsert("葵花卫星正在下载第 1/24 个文件")
    upsert("下载进度#1 [░░░░] 20%")
    upsert("葵花卫星正在下载第 2/24 个文件")
    upsert("葵花卫星正在下载第 3/24 个文件")

    assert panel.lines == [
        "葵花卫星正在下载第 1/24 个文件",
        "葵花卫星正在下载第 2/24 个文件",
        "葵花卫星正在下载第 3/24 个文件",
        "下载进度#1 [░░░░] 20%",
    ]

    upsert("下载进度#1 [██░░] 40%")

    assert panel.lines[-1] == "下载进度#1 [██░░] 40%"
    assert len(panel.lines) == 4


def test_shell_output_status_lines_keep_chronological_position():
    from types import SimpleNamespace

    from aero.cli.main import AeroApp

    panel = SimpleNamespace(lines=[])

    def upsert(text: str) -> bool:
        return AeroApp._upsert_status_line(None, panel, text)

    upsert("正在执行命令：mamba install cartopy")
    upsert("stdout: /bin/bash: mamba: No such file or directory")
    upsert("正在执行命令：pip install cartopy")

    assert panel.lines == [
        "正在执行命令：mamba install cartopy",
        "stdout: /bin/bash: mamba: No such file or directory",
        "正在执行命令：pip install cartopy",
    ]


def test_completed_download_progress_disappears_from_status_panel():
    from types import SimpleNamespace

    from aero.cli.main import AeroApp

    panel = SimpleNamespace(
        lines=[
            "正在下载文件",
            "下载进度#1 [██████████░░] 50%",
            "下载进度#2 [████░░░░░░] 20%",
        ]
    )

    AeroApp._upsert_status_line(None, panel, "下载进度#1 [████████████] 100%")

    assert panel.lines == [
        "正在下载文件",
        "下载进度#2 [████░░░░░░] 20%",
    ]

    AeroApp._upsert_status_line(None, panel, "下载进度#3 [████████████] 100.0%")

    assert all(not line.startswith("下载进度#3") for line in panel.lines)


def test_ensure_runtime_tools_confirmation_skipped_when_ready(monkeypatch, tmp_path):
    from aero.agent import loop
    from aero.agent.runtime import Runtime

    root = tmp_path / "miniconda3"
    env_bin = root / "envs" / "aero-agent" / "bin"
    env_bin.mkdir(parents=True)
    for tool in ("cdo", "grib_to_netcdf"):
        path = env_bin / tool
        path.write_text("#!/bin/sh\n")
        path.chmod(0o755)

    monkeypatch.setattr(
        Runtime,
        "_build_exec_env",
        staticmethod(lambda: {"PATH": str(env_bin), "CONDA_EXE": str(root / "bin" / "conda")}),
    )

    assert (
        loop._tool_call_needs_confirmation(
            "ensure_runtime_tools",
            {"tools": ["cdo", "grib_to_netcdf"]},
        )
        is False
    )


def test_ensure_runtime_tools_confirmation_required_when_missing(monkeypatch, tmp_path):
    from aero.agent import loop
    from aero.agent.runtime import Runtime

    root = tmp_path / "miniconda3"
    env_bin = root / "envs" / "aero-agent" / "bin"
    env_bin.mkdir(parents=True)
    cdo = env_bin / "cdo"
    cdo.write_text("#!/bin/sh\n")
    cdo.chmod(0o755)

    monkeypatch.setattr(
        Runtime,
        "_build_exec_env",
        staticmethod(lambda: {"PATH": str(env_bin), "CONDA_EXE": str(root / "bin" / "conda")}),
    )

    assert (
        loop._tool_call_needs_confirmation(
            "ensure_runtime_tools",
            {"tools": ["cdo", "grib_to_netcdf"]},
        )
        is True
    )


def test_write_file_progress_includes_file_name():
    from aero.agent.loop import _tool_progress_message

    args = {"file_path": "/Users/clarmylee/gitlab/products/aero/lab/scripts/tmp/merge_gfs.py"}

    assert (
        _tool_progress_message("write_file", "start", args)
        == "正在写入文件：scripts/tmp/merge_gfs.py"
    )
    assert _tool_progress_message("write_file", "done", args) == "文件写入完成"


def test_tool_result_error_status_is_not_success():
    from aero.agent.loop import _tool_result_has_error_status

    assert _tool_result_has_error_status({"status": "error", "message": "failed"}) is True
    assert _tool_result_has_error_status({"status": "success"}) is False
    assert _tool_result_has_error_status("error") is False


@pytest.mark.asyncio
async def test_download_progress_reports_fractional_start():
    import asyncio

    from aero.agent.progress import ProgressReporter, use_progress_reporter
    from aero.toolbox.builtin_tools import _download_progress_reporter

    queue: asyncio.Queue[str] = asyncio.Queue()
    reporter = ProgressReporter(asyncio.get_running_loop(), queue)

    with use_progress_reporter(reporter):
        progress = _download_progress_reporter()
        progress(5 * 1024 * 1024, 619 * 1024 * 1024, force=True)

    message = await asyncio.wait_for(queue.get(), timeout=1)

    assert "  0.8%" in message
    assert "(5.0 MB / 619.0 MB)" in message


def test_direct_tool_response_preserves_vision_setup_message():
    from aero.agent.loop import _direct_tool_response

    message = (
        "控制台入口：[阿里云百炼 API Key](https://example.com)\n"
        "如果你的终端不能点击链接，请复制这个地址打开：https://example.com"
    )

    assert (
        _direct_tool_response(
            "analyze_image",
            {"status": "not_configured", "message": message},
        )
        == message
    )
    assert (
        _direct_tool_response(
            "analyze_image",
            {"status": "success", "message": message},
        )
        is None
    )


def test_agent_applies_llm_config_update(tmp_path, monkeypatch):
    from aero.agent.loop import AgentLoop
    from aero.core.config import clear_llm_api_key, save_llm_api_key

    monkeypatch.setenv("AERO_SECRETS_PATH", str(tmp_path / "secrets.yaml"))
    config = AeroConfig.create_default()
    save_llm_api_key("deepseek", "sk-old")
    config_path = tmp_path / "aero.yaml"
    config.save(config_path)
    monkeypatch.chdir(tmp_path)

    fresh = AeroConfig.load(config_path)
    fresh.llm.provider = "deepseek"
    fresh.llm.model = "deepseek-v4-flash"
    fresh.llm.base_url = "https://api.deepseek.com"
    save_llm_api_key("deepseek", "sk-new")
    fresh.save(config_path)

    loop = AgentLoop(config)
    loop._apply_runtime_config_update(
        "configure_llm_provider",
        {
            "llm_config_updated": True,
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "base_url": "https://api.deepseek.com",
        },
    )

    assert loop.llm.config.provider == "deepseek"
    assert loop.llm.config.model == "deepseek-v4-flash"
    assert loop.llm.config.api_key == "sk-new"

    clear_llm_api_key("deepseek")
    loop._apply_runtime_config_update(
        "clear_llm_config",
        {
            "llm_config_updated": True,
            "provider": "deepseek",
            "model": "deepseek-v4-flash",
            "base_url": "https://api.deepseek.com",
            "api_key_cleared": True,
        },
    )

    assert loop.llm.config.api_key == ""


@pytest.mark.asyncio
async def test_list_downloads_returns_retry_parameters(tmp_path, monkeypatch):
    from aero.data.download_store import CDSDownloadStore
    from aero.toolbox.builtin_tools import list_downloads

    config = AeroConfig.create_default()
    config.save(tmp_path / "aero.yaml")
    monkeypatch.chdir(tmp_path)

    store = CDSDownloadStore(tmp_path / "aero_downloads.db")
    store.insert(
        source="era5-gcs",
        request_id="gcs-1",
        dataset_id="reanalysis-era5-single-levels",
        variables=["2m_temperature"],
        year=2026,
        month=2,
        day=16,
        pressure_level=None,
        area="global",
        data_format="netcdf",
        file_path=str(tmp_path / "data" / "era5.nc"),
        status="download_failed",
        error_msg="bad value(s) in fds_to_keep",
        notes="source=gcs",
    )

    result = await list_downloads(status="download_failed", limit=5)

    assert result["returned"] == 1
    record = result["downloads"][0]
    assert record["source"] == "era5-gcs"
    assert record["variables"] == ["2m_temperature"]
    assert record["year"] == 2026
    assert record["month"] == 2
    assert record["day"] == 16
    assert record["pressure_level"] is None
    assert record["data_format"] == "netcdf"
    assert record["notes"] == "source=gcs"


@pytest.mark.asyncio
async def test_runtime_execution():
    from aero.agent.runtime import Runtime

    rt = Runtime()

    def add(a, b):
        return {"sum": a + b}

    result = await rt.execute(add, {"a": 1, "b": 2})
    assert result.success
    assert result.result == {"sum": 3}


@pytest.mark.asyncio
async def test_runtime_execution_error():
    from aero.agent.runtime import Runtime

    rt = Runtime()

    async def fail():
        raise ValueError("test error")

    result = await rt.execute(fail, {})
    assert not result.success
    assert "test error" in result.error


@pytest.mark.asyncio
async def test_delete_file_success(tmp_path):
    from aero.toolbox.builtin_tools import delete_file

    test_file = tmp_path / "test.nc"
    test_file.write_text("data")
    assert test_file.exists()

    result = await delete_file(str(test_file))
    assert result["status"] == "success"
    assert not test_file.exists()


@pytest.mark.asyncio
async def test_read_file_blocks_aero_secret_files(tmp_path, monkeypatch):
    from aero.toolbox.builtin_tools import read_file

    secrets_dir = tmp_path / ".aero"
    secrets_dir.mkdir()
    secrets_path = secrets_dir / "secrets.yaml"
    secrets_path.write_text("credentials:\n  ads:\n    key: secret\n")
    monkeypatch.chdir(tmp_path)

    result = await read_file(str(secrets_path))

    assert result["status"] == "error"
    assert result["secret_access_blocked"] is True
    assert "check_ads_config" in result["suggested_tools"]


@pytest.mark.asyncio
async def test_delete_file_not_found():
    from aero.toolbox.builtin_tools import delete_file

    result = await delete_file("/nonexistent/path/file.nc")
    assert result["status"] == "error"
    assert "不存在" in result["message"]


@pytest.mark.asyncio
async def test_delete_file_not_a_file(tmp_path):
    from aero.toolbox.builtin_tools import delete_file

    result = await delete_file(str(tmp_path))
    assert result["status"] == "error"
    assert "不是文件" in result["message"]


@pytest.mark.asyncio
async def test_confirmation_deny():
    from aero.agent.loop import AgentLoop
    from aero.core.config import AeroConfig
    from aero.core.types import ToolCall

    config = AeroConfig.create_default()
    config.llm.api_key = "test-key"
    loop = AgentLoop(config)

    from aero.toolbox.builtin_tools import delete_file  # noqa: F401

    tc = ToolCall(id="tc1", name="delete_file", arguments={"file_path": "/tmp/test.nc"})

    events = []
    async for event in loop._execute_one_tool_stream(tc):
        events.append(event)
        if event.type == "confirm":
            loop.confirm_future.set_result("deny")

    confirm_events = [e for e in events if e.type == "confirm"]
    assert len(confirm_events) == 1

    tool_msgs = [m for m in loop.messages if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert "拒绝" in tool_msgs[0].content


@pytest.mark.asyncio
async def test_confirmation_allow(tmp_path):
    from aero.agent.loop import AgentLoop
    from aero.core.config import AeroConfig
    from aero.core.types import ToolCall

    config = AeroConfig.create_default()
    config.llm.api_key = "test-key"
    loop = AgentLoop(config)

    from aero.toolbox.builtin_tools import delete_file  # noqa: F401

    test_file = tmp_path / "to_delete.nc"
    test_file.write_text("data")

    tc = ToolCall(id="tc1", name="delete_file", arguments={"file_path": str(test_file)})

    events = []
    async for event in loop._execute_one_tool_stream(tc):
        events.append(event)
        if event.type == "confirm":
            loop.confirm_future.set_result("allow")

    confirm_events = [e for e in events if e.type == "confirm"]
    assert len(confirm_events) == 1
    assert not test_file.exists()

    tool_msgs = [m for m in loop.messages if m.role == "tool"]
    assert len(tool_msgs) == 1
    assert "success" in tool_msgs[0].content


@pytest.mark.asyncio
async def test_confirmation_always_skips_next(tmp_path):
    from aero.agent.loop import AgentLoop
    from aero.core.config import AeroConfig
    from aero.core.types import ToolCall

    config = AeroConfig.create_default()
    config.llm.api_key = "test-key"
    loop = AgentLoop(config)

    from aero.toolbox.builtin_tools import delete_file  # noqa: F401

    file1 = tmp_path / "file1.nc"
    file1.write_text("data1")
    file2 = tmp_path / "file2.nc"
    file2.write_text("data2")

    tc1 = ToolCall(id="tc1", name="delete_file", arguments={"file_path": str(file1)})
    events1 = []
    async for event in loop._execute_one_tool_stream(tc1):
        events1.append(event)
        if event.type == "confirm":
            loop.confirm_future.set_result("always")

    assert not file1.exists()
    assert "delete_file" in loop.always_allow

    tc2 = ToolCall(id="tc2", name="delete_file", arguments={"file_path": str(file2)})
    events2 = []
    async for event in loop._execute_one_tool_stream(tc2):
        events2.append(event)

    confirm_events2 = [e for e in events2 if e.type == "confirm"]
    assert len(confirm_events2) == 0
    assert not file2.exists()
