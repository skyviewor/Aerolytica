from pathlib import Path

from aero.agent.loop import _skill_selection_text
from aero.agent.skills import SkillLoader, SkillSelector, render_skill_context
from aero.core.types import Message


def _write_skill(
    root: Path,
    name: str,
    description: str,
    body: str = "Use this skill.",
) -> None:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n# {name}\n\n{body}\n",
        encoding="utf-8",
    )


def test_skill_loader_reads_standard_skill_frontmatter(tmp_path):
    builtin = tmp_path / "builtin"
    _write_skill(
        builtin,
        "scientific-plotting",
        "Use this skill for plot, figure, 画图, 绘图, 科研图.",
    )

    loader = SkillLoader(builtin_dir=builtin, project_dir=tmp_path / "project")
    skills = loader.load()

    assert [skill.name for skill in skills] == ["scientific-plotting"]
    assert "plot" in skills[0].description
    assert skills[0].source == "builtin"


def test_project_skill_overrides_builtin_skill(tmp_path):
    builtin = tmp_path / "builtin"
    project = tmp_path / "project"
    _write_skill(builtin, "scientific-plotting", "Built-in plotting skill.")
    _write_skill(project / "skills", "scientific-plotting", "Project plotting skill.")

    loader = SkillLoader(builtin_dir=builtin, project_dir=project)
    skills = loader.load()

    assert len(skills) == 1
    assert skills[0].description == "Project plotting skill."
    assert skills[0].source == "project"


def test_skill_selector_matches_plotting_requests(tmp_path):
    builtin = tmp_path / "builtin"
    _write_skill(
        builtin,
        "scientific-plotting",
        "Use this skill for plot, figure, visualization, 画图, 绘图, 科研图.",
    )
    _write_skill(builtin, "literature-review", "Use this skill for literature review.")

    selector = SkillSelector(SkillLoader(builtin_dir=builtin, project_dir=tmp_path / "project"))

    selected = selector.select("帮我把 ERA5 降水数据画图，并保存到 figures 目录")

    assert [item.skill.name for item in selected] == ["scientific-plotting"]


def test_builtin_era5_skill_is_selected_for_era5_downloads():
    selected = SkillSelector().select("下载 ERA5 位势涡度数据")

    assert "era5-cds" in [item.skill.name for item in selected]


def test_vertical_cross_section_loads_scientific_plotting_guidance():
    selected = SkillSelector().select("绘制青藏高原南北向风场垂直剖面图")
    context = render_skill_context(selected)

    assert "scientific-plotting" in [item.skill.name for item in selected]
    assert "references/vertical-cross-sections.md" in context
    assert "references/cross-section-wind-vectors.md" in context
    assert "references/wind-barbs.md" in context


def test_skill_selector_always_activates_plotting_skill_for_a_drawing_verb(tmp_path):
    builtin = tmp_path / "builtin"
    _write_skill(builtin, "scientific-plotting", "Scientific plotting workflow.")
    _write_skill(builtin, "literature-review", "Use this skill for literature review.")
    selector = SkillSelector(SkillLoader(builtin_dir=builtin, project_dir=tmp_path / "project"))

    assert [item.skill.name for item in selector.select("改画一张 3x3 的图")] == [
        "scientific-plotting"
    ]
    assert [item.skill.name for item in selector.select("画")] == ["scientific-plotting"]


def test_skill_selector_matches_chinese_plotting_requests_from_skill_body(tmp_path):
    builtin = tmp_path / "builtin"
    _write_skill(
        builtin,
        "scientific-plotting",
        "Use this skill for plot, figure, visualization, 画图, 绘图, 科研图.",
        "Use this skill for precipitation maps and 降水 spatial distribution plots.",
    )
    _write_skill(builtin, "literature-review", "Use this skill for literature review.")

    selector = SkillSelector(SkillLoader(builtin_dir=builtin, project_dir=tmp_path / "project"))

    selected = selector.select("画华南降水空间分布图")

    assert [item.skill.name for item in selected] == ["scientific-plotting"]


def test_skill_selector_adds_cnmaps_for_china_map_requests(tmp_path):
    builtin = tmp_path / "builtin"
    _write_skill(
        builtin,
        "scientific-plotting",
        "Use this skill for plot, figure, visualization, 画图, 绘图, 科研图.",
    )
    _write_skill(
        builtin,
        "cnmaps",
        "Use this skill for compliant boundary queries with cnmaps.",
    )

    selector = SkillSelector(SkillLoader(builtin_dir=builtin, project_dir=tmp_path / "project"))

    selected = selector.select("帮我画一张中国区域降水图")

    assert [item.skill.name for item in selected] == ["scientific-plotting", "cnmaps"]


def test_follow_up_plot_request_inherits_recent_user_context_for_skill_selection():
    history = [
        Message(role="system", content="system"),
        Message(role="user", content="中国地区气温，画一个 2x2 的图，共享同一个 colorbar"),
        Message(role="assistant", content="已完成。"),
    ]

    selection_text = _skill_selection_text("改画一张 3x3 的图", history)
    selected = SkillSelector().select(selection_text)

    assert [item.skill.name for item in selected] == ["scientific-plotting", "cnmaps"]


def test_non_referential_message_does_not_inherit_old_skill_context():
    history = [Message(role="user", content="中国地区气温，画一个 2x2 的图")]

    assert _skill_selection_text("你好", history) == "你好"


def test_skill_context_mentions_references_without_loading_them(tmp_path):
    builtin = tmp_path / "builtin"
    _write_skill(
        builtin,
        "scientific-plotting",
        "Use this skill for plot, figure, 画图.",
        "Read `references/maps.md` only for map details.",
    )
    references = builtin / "scientific-plotting" / "references"
    references.mkdir()
    (references / "maps.md").write_text("Detailed map rules should not be injected.")

    selector = SkillSelector(SkillLoader(builtin_dir=builtin, project_dir=tmp_path / "project"))
    context = render_skill_context(selector.select("画图"))

    assert "references/maps.md" in context
    assert "Detailed map rules should not be injected." not in context


def test_builtin_weather_verification_skill_is_selected_for_chinese_request():
    selected = SkillSelector().select(
        "用观测数据对气象预报做准确率测评，计算温度 RMSE 和降水 TS、ETS"
    )

    assert "weather-verification" in [item.skill.name for item in selected]


def test_builtin_weather_verification_skill_is_selected_for_english_request():
    selected = SkillSelector().select(
        "Verify a deterministic weather forecast against observations by lead time"
    )

    assert "weather-verification" in [item.skill.name for item in selected]


def test_weather_verification_context_points_to_cyeva_reference():
    selected = SkillSelector().select("对预报和观测做预报检验")
    context = render_skill_context(selected)

    assert "references/cyeva-api.md" in context


def test_builtin_weather_verification_requires_executed_cyeva():
    skill_text = (
        Path(__file__).resolve().parents[1]
        / "src/aero/skills/builtin/weather-verification/SKILL.md"
    ).read_text()

    assert "## Mandatory execution gate" in skill_text
    assert "Merely installing or importing the package does not satisfy" in skill_text
    assert "must never replace" in skill_text
    assert "the cyeva result" in skill_text
    assert "must name the cyeva version and comparison class used" in skill_text
