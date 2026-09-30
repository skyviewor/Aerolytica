"""Non-secret input forms; permission approval remains a separate protocol."""

from __future__ import annotations

import math
import re
from contextlib import contextmanager
from contextvars import ContextVar

from aero.toolbox.registry import register_tool

_provider = ContextVar("aero_user_input_provider", default=None)
_TYPES = {"text", "multiline", "number", "single_choice", "multiple_choice", "boolean"}


def validate_description(description: dict) -> dict:
    if not isinstance(description, dict) or set(description) - {
        "title",
        "instructions",
        "fields",
        "message_id",
    }:
        raise ValueError("表单描述无效")
    if not isinstance(description.get("title"), str) or not 1 <= len(description["title"]) <= 200:
        raise ValueError("表单标题无效")
    if (
        not isinstance(description.get("instructions", ""), str)
        or len(description.get("instructions", "")) > 8000
    ):
        raise ValueError("表单说明无效")
    if re.search(
        r"secret|password|api.?key|密钥|密码|凭据",
        description["title"] + description.get("instructions", ""),
        re.I,
    ):
        raise ValueError("凭据必须使用安全输入工具")
    fields = description.get("fields")
    if not isinstance(fields, list) or not 1 <= len(fields) <= 20:
        raise ValueError("需要 1 至 20 个字段")
    names = set()
    for field in fields:
        if not isinstance(field, dict) or set(field) - {
            "name",
            "label",
            "description",
            "type",
            "required",
            "options",
            "min",
            "max",
            "max_length",
        }:
            raise ValueError("字段描述无效")
        name = field.get("name", "")
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,63}", name)
            or name in names
        ):
            raise ValueError("字段名称无效或重复")
        names.add(name)
        if not isinstance(field.get("label"), str) or not 1 <= len(field["label"]) <= 200:
            raise ValueError("字段标签无效")
        if (
            not isinstance(field.get("description", ""), str)
            or len(field.get("description", "")) > 2000
        ):
            raise ValueError("字段说明无效")
        if field.get("type") not in _TYPES or not isinstance(field.get("required", True), bool):
            raise ValueError("字段类型无效")
        if re.search(
            r"secret|password|api.?key|token|密钥|密码|凭据",
            name + field["label"] + field.get("description", ""),
            re.I,
        ):
            raise ValueError("凭据必须使用安全输入工具")
        for key in ("min", "max"):
            value = field.get(key)
            if value is not None and (
                not isinstance(value, (int, float))
                or isinstance(value, bool)
                or not math.isfinite(value)
            ):
                raise ValueError("数字范围无效")
        if field.get("min", -math.inf) > field.get("max", math.inf):
            raise ValueError("数字范围无效")
        length = field.get("max_length", 8000)
        if not isinstance(length, int) or isinstance(length, bool) or not 1 <= length <= 8000:
            raise ValueError("文本长度限制无效")
        if field["type"] in {"single_choice", "multiple_choice"}:
            options = field.get("options")
            if (
                not isinstance(options, list)
                or not 1 <= len(options) <= 100
                or not all(isinstance(v, str) and 1 <= len(v) <= 500 for v in options)
            ):
                raise ValueError("选择项缺少有效选项")
            if len(set(options)) != len(options):
                raise ValueError("选项不能重复")
    return description


def validate_values(description: dict, values: dict) -> dict:
    validate_description(description)
    if not isinstance(values, dict) or set(values) - {f["name"] for f in description["fields"]}:
        raise ValueError("输入字段无效")
    for field in description["fields"]:
        value = values.get(field["name"])
        label = field.get("label", field["name"])
        if value is None or value == "" or value == []:
            if field.get("required", True):
                raise ValueError(f"请填写{label}")
            continue
        kind = field["type"]
        valid = True
        if kind in {"text", "multiline"}:
            valid = isinstance(value, str) and len(value) <= field.get("max_length", 8000)
        elif kind == "number":
            valid = (
                isinstance(value, (int, float))
                and not isinstance(value, bool)
                and math.isfinite(value)
            )
            valid = valid and field.get("min", -math.inf) <= value <= field.get("max", math.inf)
        elif kind == "boolean":
            valid = isinstance(value, bool)
        elif kind == "single_choice":
            valid = value in field["options"]
        elif kind == "multiple_choice":
            valid = (
                isinstance(value, list)
                and all(v in field["options"] for v in value)
                and len(set(value)) == len(value)
            )
        if not valid:
            raise ValueError(f"{label}的格式或取值无效")
    return values


@contextmanager
def use_user_input_provider(provider):
    token = _provider.set(provider)
    try:
        yield
    finally:
        _provider.reset(token)


@register_tool(
    name="request_user_input",
    description="执行任务缺少必要普通参数时请求表单输入；支持选择和数字。禁止收集凭据、密钥，禁止替代操作审批。",
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "instructions": {"type": "string"},
            "fields": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "label": {"type": "string"},
                        "description": {"type": "string"},
                        "type": {"type": "string", "enum": sorted(_TYPES)},
                        "required": {"type": "boolean"},
                        "options": {"type": "array", "items": {"type": "string"}},
                        "min": {"type": "number"},
                        "max": {"type": "number"},
                        "max_length": {"type": "integer"},
                    },
                    "required": ["name", "label", "type"],
                },
            },
        },
        "required": ["title", "fields"],
    },
)
async def request_user_input(title: str, fields: list[dict], instructions: str = "") -> dict:
    try:
        description = validate_description(
            {"title": title, "instructions": instructions, "fields": fields}
        )
    except (ValueError, TypeError) as exc:
        return {"status": "invalid", "message": str(exc)}
    provider = _provider.get()
    if provider is None:
        return {"status": "unavailable", "message": "当前界面不支持输入表单"}
    return await provider(description)
