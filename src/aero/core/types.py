"""Aero shared types."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

type JSONSchema = dict[str, Any]
type ToolArgs = dict[str, Any]
type ToolResult = dict[str, Any]


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: JSONSchema
    function: Any = field(repr=False, compare=False)
    requires_confirmation: bool = False

    def to_llm_function(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: ToolArgs


@dataclass
class Message:
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] | None = None


@dataclass
class DownloadResult:
    source: str
    file_path: Path
    variables: list[str]
    time_range: dict
    region: dict | None = None
    params: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)
    checksum: str | None = None
