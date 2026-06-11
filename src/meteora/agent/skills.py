"""Standard Skill loading and selection for Meteora agents."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - fallback for minimal runtimes
    yaml = None


@dataclass(frozen=True)
class Skill:
    name: str
    description: str
    path: Path
    body: str
    source: str


@dataclass(frozen=True)
class SelectedSkill:
    skill: Skill
    score: int


class SkillLoader:
    """Load standard SKILL.md folders from built-in and project locations."""

    def __init__(
        self,
        builtin_dir: Path | None = None,
        project_dir: Path | None = None,
    ) -> None:
        package_dir = Path(__file__).resolve().parents[1]
        self.builtin_dir = builtin_dir or package_dir / "skills" / "builtin"
        self.project_dir = project_dir or Path.cwd()

    def load(self) -> list[Skill]:
        skills: dict[str, Skill] = {}
        for source, root in (
            ("builtin", self.builtin_dir),
            ("project", self.project_dir / "skills"),
        ):
            for skill in self._load_from_root(root, source):
                skills[skill.name] = skill
        return sorted(skills.values(), key=lambda skill: skill.name)

    def _load_from_root(self, root: Path, source: str) -> list[Skill]:
        if not root.exists() or not root.is_dir():
            return []

        skills = []
        for entry in sorted(root.iterdir()):
            if not entry.is_dir():
                continue
            skill_md = entry / "SKILL.md"
            if not skill_md.exists():
                continue
            skill = _parse_skill(skill_md, source)
            if skill is not None:
                skills.append(skill)
        return skills


class SkillSelector:
    """Select a small set of relevant Skills from standard metadata."""

    def __init__(self, loader: SkillLoader | None = None, max_selected: int = 2) -> None:
        self.loader = loader or SkillLoader()
        self.max_selected = max_selected

    def select(self, text: str) -> list[SelectedSkill]:
        normalized = _normalize(text)
        if not normalized:
            return []

        skills = self.loader.load()
        selected = []
        for skill in skills:
            score = _score_skill(skill, normalized)
            if score > 0:
                selected.append(SelectedSkill(skill=skill, score=score))

        selected.sort(key=lambda item: (-item.score, item.skill.name))
        selected = selected[: self.max_selected]
        return _expand_related_skills(skills, selected, normalized, self.max_selected)


def render_skill_context(selected: list[SelectedSkill]) -> str:
    if not selected:
        return ""

    parts = []
    for item in selected:
        skill = item.skill
        root = skill.path.parent
        references = root / "references"
        scripts = root / "scripts"
        assets = root / "assets"
        resource_lines = [
            f"- Skill root: `{root}`",
        ]
        if references.exists():
            resource_lines.append(f"- References: `{references}`")
        if scripts.exists():
            resource_lines.append(f"- Scripts: `{scripts}`")
        if assets.exists():
            resource_lines.append(f"- Assets: `{assets}`")

        parts.append(
            "\n".join(
                [
                    f"### {skill.name}",
                    f"Description: {skill.description}",
                    f"Source: {skill.source}",
                    "",
                    "Resources:",
                    *resource_lines,
                    "",
                    skill.body.strip(),
                ]
            )
        )
    return "\n\n".join(parts)


def _parse_skill(path: Path, source: str) -> Skill | None:
    raw = path.read_text(encoding="utf-8")
    metadata, body = _split_frontmatter(raw)
    name = str(metadata.get("name") or "").strip()
    description = str(metadata.get("description") or "").strip()
    if not name or not description:
        return None
    return Skill(
        name=name,
        description=description,
        path=path,
        body=body.strip(),
        source=source,
    )


def _split_frontmatter(raw: str) -> tuple[dict, str]:
    if not raw.startswith("---\n"):
        return {}, raw
    marker = "\n---\n"
    end = raw.find(marker, 4)
    if end == -1:
        return {}, raw
    frontmatter = raw[4:end]
    body = raw[end + len(marker) :]
    data = yaml.safe_load(frontmatter) if yaml is not None else _parse_simple_yaml(frontmatter)
    data = data or {}
    return data if isinstance(data, dict) else {}, body


def _parse_simple_yaml(text: str) -> dict[str, str]:
    data = {}
    current_key = ""
    for raw_line in text.splitlines():
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if raw_line.startswith((" ", "\t")) and current_key:
            data[current_key] = f"{data[current_key]} {line.strip()}".strip()
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        current_key = key.strip()
        data[current_key] = value.strip().strip("'\"")
    return data


def _score_skill(skill: Skill, normalized_text: str) -> int:
    metadata = _normalize(f"{skill.name} {skill.description}")
    body = _normalize(skill.body)
    score = 0

    for token in _tokens(metadata):
        if len(token) >= 3 and token in normalized_text:
            score += 2

    for phrase in _important_phrases(metadata):
        if phrase in normalized_text:
            score += 4

    for token in _tokens(body):
        if len(token) >= 3 and token in normalized_text:
            score += 1

    return score


def _expand_related_skills(
    skills: list[Skill],
    selected: list[SelectedSkill],
    normalized_text: str,
    max_selected: int,
) -> list[SelectedSkill]:
    by_name = {skill.name: skill for skill in skills}
    selected_by_name = {item.skill.name: item for item in selected}

    if _is_china_map_request(normalized_text):
        sciplot = by_name.get("scientific-plotting")
        cnmaps = by_name.get("cnmaps")
        if sciplot is not None:
            if "scientific-plotting" in selected_by_name:
                selected = [
                    SelectedSkill(skill=item.skill, score=100)
                    if item.skill.name == "scientific-plotting"
                    else item
                    for item in selected
                ]
            else:
                selected.append(SelectedSkill(skill=sciplot, score=100))
                selected_by_name["scientific-plotting"] = selected[-1]
        if cnmaps is not None and "cnmaps" not in selected_by_name:
            selected.append(SelectedSkill(skill=cnmaps, score=99))

    selected.sort(key=lambda item: (-item.score, item.skill.name))
    return selected[:max_selected]


def _is_china_map_request(normalized_text: str) -> bool:
    china_terms = (
        "中国",
        "全国",
        "东亚",
        "华北",
        "华东",
        "华南",
        "华中",
        "东北",
        "西北",
        "西南",
        "北京",
        "天津",
        "河北",
        "山西",
        "内蒙古",
        "辽宁",
        "吉林",
        "黑龙江",
        "上海",
        "江苏",
        "浙江",
        "安徽",
        "福建",
        "江西",
        "山东",
        "河南",
        "湖北",
        "湖南",
        "广东",
        "广西",
        "海南",
        "重庆",
        "四川",
        "贵州",
        "云南",
        "西藏",
        "陕西",
        "甘肃",
        "青海",
        "宁夏",
        "新疆",
        "台湾",
        "香港",
        "澳门",
        "南海",
        "china",
        "mainland china",
    )
    map_terms = (
        "图",
        "画",
        "绘",
        "地图",
        "空间分布",
        "高度场",
        "温度场",
        "降水场",
        "风场",
        "气压场",
        "边界",
        "国界",
        "省界",
        "市界",
        "map",
        "plot",
        "figure",
        "boundary",
        "border",
    )
    return any(term in normalized_text for term in china_terms) and any(
        term in normalized_text for term in map_terms
    )


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def _tokens(text: str) -> set[str]:
    tokens = set(re.findall(r"[a-z0-9][a-z0-9_-]{2,}|[\u4e00-\u9fff]{2,}", text.lower()))
    for token in list(tokens):
        if re.fullmatch(r"[\u4e00-\u9fff]{3,}", token):
            tokens.update(token[index : index + 2] for index in range(len(token) - 1))
    return tokens


def _important_phrases(text: str) -> set[str]:
    phrases = set()
    for phrase in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        phrases.add(phrase)
    for phrase in re.findall(r"[a-z][a-z0-9 -]{2,}", text):
        phrase = " ".join(phrase.split())
        if len(phrase) >= 4:
            phrases.add(phrase)
    return phrases
