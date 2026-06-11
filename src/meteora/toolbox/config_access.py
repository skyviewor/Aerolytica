"""Configuration lookup helpers shared by built-in tool modules."""

from pathlib import Path

from meteora.core.config import MeteoraConfig


def find_config_path() -> Path:
    """Find the nearest project config path, or default to cwd."""
    cwd = Path.cwd()
    for parent in [cwd, *cwd.parents]:
        config_path = parent / "meteora.yaml"
        if config_path.exists():
            return config_path
    return cwd / "meteora.yaml"


def find_config() -> MeteoraConfig:
    """Load the nearest project config, or create an in-memory default."""
    config_path = find_config_path()
    if config_path.exists():
        return MeteoraConfig.load(config_path)
    return MeteoraConfig.create_default()


def mask_secret(value: str) -> str:
    """Mask a secret while leaving enough context to identify it."""
    if len(value) <= 8:
        return "*" * len(value)
    return value[:4] + "..." + value[-4:]
