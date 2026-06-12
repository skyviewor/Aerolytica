"""Configuration lookup helpers shared by built-in tool modules."""

from pathlib import Path

from aero.core.config import AeroConfig


def find_config_path() -> Path:
    """Find the nearest project config path, or default to cwd."""
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        config_path = parent / "aero.yaml"
        if config_path.exists():
            return config_path
    return cwd / "aero.yaml"


def find_config() -> AeroConfig:
    """Load the nearest project config, or create an in-memory default."""
    config_path = find_config_path()
    if config_path.exists():
        return AeroConfig.load(config_path)
    return AeroConfig.create_default()


def mask_secret(value: str) -> str:
    """Mask a secret while leaving enough context to identify it."""
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + "..." + value[-4:]
