"""Image attachment helpers for chat messages."""

from __future__ import annotations

import hashlib
import re
import tempfile
from functools import lru_cache
from pathlib import Path

from PIL import Image as _PILImage
from rich.style import Style
from rich.text import Text


def extract_image_paths(text: str) -> list[str]:
    """Extract local image paths from markdown image syntax or plain path references."""
    paths = []

    # Markdown syntax: ![](path) or ![alt](path)
    for match in re.finditer(r"!\[(?:[^\]]*)\]\(([^)]+)\)", text):
        path = match.group(1).strip()
        if path and not path.startswith(("http://", "https://")):
            paths.append(path)

    # Fallback: common image extensions mentioned as standalone paths
    if not paths:
        for match in re.finditer(
            r"\b([\w./-]+\.(?:png|jpe?g|gif|svg|webp))\b", text, re.IGNORECASE
        ):
            path = match.group(1)
            if (
                path
                and "://" not in path
                and not re.match(r"^[\w.-]+\.[a-z]{2,}/", path)
            ):
                paths.append(path)

    return paths


def strip_image_markdown(text: str) -> str:
    """Remove inline image markdown, optionally replacing with path caption."""
    return re.sub(r"!\[(?:[^\]]*)\]\([^)]+\)\s*", "", text).rstrip()


def resolve_image_path(path: str, project_dir: str | Path) -> Path | None:
    """Resolve a relative image path against the project directory."""
    base = Path(project_dir)
    abs_path = base / path
    if abs_path.exists() and abs_path.is_file():
        return abs_path
    return None


def image_metadata(path: str | Path) -> tuple[int | None, int | None, str]:
    """Return image dimensions and a compact file size string."""
    image_path = Path(path)
    size = image_path.stat().st_size if image_path.exists() else 0
    try:
        with _PILImage.open(image_path) as image:
            width, height = image.size
    except Exception:
        width = None
        height = None

    if size >= 1024 * 1024:
        size_text = f"{size / (1024 * 1024):.1f} MB"
    elif size >= 1024:
        size_text = f"{size / 1024:.1f} KB"
    else:
        size_text = f"{size} B"

    return width, height, size_text


def terminal_image_preview(
    path: str | Path,
    *,
    max_width: int = 1200,
    max_height: int = 800,
) -> Path:
    """Return a cached, high-quality preview file for terminal protocols."""
    image_path = Path(path)
    stat = image_path.stat()
    return _cached_terminal_image_preview(
        str(image_path.resolve()),
        stat.st_mtime_ns,
        stat.st_size,
        max_width,
        max_height,
    )


@lru_cache(maxsize=16)
def _cached_terminal_image_preview(
    path: str,
    _mtime_ns: int,
    _file_size: int,
    max_width: int,
    max_height: int,
) -> Path:
    """Create a resized preview file once per source image version."""
    with _PILImage.open(path) as image:
        if image.width <= max_width and image.height <= max_height:
            return Path(path)
        image.load()
        preview = image.copy()
    preview.thumbnail((max_width, max_height), _PILImage.Resampling.LANCZOS)
    cache_key = hashlib.sha256(
        f"{path}:{_mtime_ns}:{_file_size}:{max_width}:{max_height}".encode()
    ).hexdigest()[:24]
    cache_dir = Path(tempfile.gettempdir()) / "aero-image-previews"
    cache_dir.mkdir(parents=True, exist_ok=True)
    preview_path = cache_dir / f"{cache_key}.png"
    if not preview_path.exists():
        preview.save(preview_path, format="PNG", optimize=True)
    return preview_path


def terminal_half_block_preview(
    path: str | Path,
    *,
    max_width: int = 96,
    max_height: int = 24,
) -> Text:
    """Render and cache a compact colored terminal preview."""
    image_path = Path(path)
    stat = image_path.stat()
    return _cached_terminal_half_block_preview(
        str(image_path.resolve()),
        stat.st_mtime_ns,
        stat.st_size,
        max_width,
        max_height,
    )


@lru_cache(maxsize=32)
def _cached_terminal_half_block_preview(
    path: str,
    _mtime_ns: int,
    _file_size: int,
    max_width: int,
    max_height: int,
) -> Text:
    """Build a preview once per file version instead of on every chat redraw."""
    image_path = Path(path)
    preview = Text()
    with _PILImage.open(image_path) as image:
        image = image.convert("RGB")
        width, height = image.size
        if width <= 0 or height <= 0:
            return preview

        target_width = min(max_width, width)
        target_height_pixels = max(2, min(max_height * 2, height))
        scale = min(target_width / width, target_height_pixels / height, 1.0)
        target_width = max(1, int(width * scale))
        target_height_pixels = max(2, int(height * scale))
        if target_height_pixels % 2:
            target_height_pixels += 1

        resized = image.resize((target_width, target_height_pixels), _PILImage.Resampling.LANCZOS)
        pixels = resized.load()
        for y in range(0, target_height_pixels, 2):
            for x in range(target_width):
                upper = pixels[x, y]
                lower = pixels[x, min(y + 1, target_height_pixels - 1)]
                style = Style(
                    color=f"rgb({upper[0]},{upper[1]},{upper[2]})",
                    bgcolor=f"rgb({lower[0]},{lower[1]},{lower[2]})",
                )
                preview.append("▀", style=style)
            if y + 2 < target_height_pixels:
                preview.append("\n")
    return preview
