"""Aero shared types and config."""

from aero.core.config import CDSCredentials, Credentials, LLMConfig, AeroConfig
from aero.core.types import (
    DownloadResult,
    Message,
    ToolCall,
    ToolSpec,
)

__all__ = [
    "AeroConfig",
    "CDSCredentials",
    "Credentials",
    "LLMConfig",
    "DownloadResult",
    "Message",
    "ToolCall",
    "ToolSpec",
]
