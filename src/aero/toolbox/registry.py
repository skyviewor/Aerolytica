"""Tool registry and decorator.

Phase 1 minimal: register tools by name, generate LLM function schemas.
"""

from aero.core.types import JSONSchema, ToolSpec


def register_tool(
    name: str,
    description: str,
    parameters: JSONSchema,
    requires_confirmation: bool = False,
):
    """Decorator to register a function as a Aero tool.

    Usage:
        @register_tool("download_era5", "Download ERA5 data", {...})
        def download_era5(variables, year, month): ...
    """

    def decorator(fn):
        spec = ToolSpec(
            name=name,
            description=description,
            parameters=parameters,
            function=fn,
            requires_confirmation=requires_confirmation,
        )
        _REGISTRY.register(spec)
        return fn

    return decorator


class ToolRegistry:
    """Central registry of all registered tools."""

    def __init__(self):
        self._tools: dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        self._tools[spec.name] = spec

    def get(self, name: str) -> ToolSpec | None:
        return self._tools.get(name)

    def list_all(self) -> list[ToolSpec]:
        return list(self._tools.values())

    def list_functions(self) -> list[dict]:
        """Return all tools as OpenAI-compatible function definitions."""
        return [t.to_llm_function() for t in self._tools.values()]


_REGISTRY = ToolRegistry()


def get_registry() -> ToolRegistry:
    return _REGISTRY
