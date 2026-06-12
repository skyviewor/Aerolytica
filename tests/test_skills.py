from pathlib import Path

from aero.agent.skills import SkillLoader, SkillSelector, render_skill_context


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

    selector = SkillSelector(
        SkillLoader(builtin_dir=builtin, project_dir=tmp_path / "project")
    )

    selected = selector.select("帮我把 ERA5 降水数据画图，并保存到 figures 目录")

    assert [item.skill.name for item in selected] == ["scientific-plotting"]


def test_skill_selector_matches_chinese_plotting_requests_from_skill_body(tmp_path):
    builtin = tmp_path / "builtin"
    _write_skill(
        builtin,
        "scientific-plotting",
        "Use this skill for plot, figure, visualization, 画图, 绘图, 科研图.",
        "Use this skill for precipitation maps and 降水 spatial distribution plots.",
    )
    _write_skill(builtin, "literature-review", "Use this skill for literature review.")

    selector = SkillSelector(
        SkillLoader(builtin_dir=builtin, project_dir=tmp_path / "project")
    )

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

    selector = SkillSelector(
        SkillLoader(builtin_dir=builtin, project_dir=tmp_path / "project")
    )

    selected = selector.select("帮我画一张中国区域降水图")

    assert [item.skill.name for item in selected] == ["scientific-plotting", "cnmaps"]


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

    selector = SkillSelector(
        SkillLoader(builtin_dir=builtin, project_dir=tmp_path / "project")
    )
    context = render_skill_context(selector.select("画图"))

    assert "references/maps.md" in context
    assert "Detailed map rules should not be injected." not in context
