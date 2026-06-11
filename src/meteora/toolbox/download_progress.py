"""Reusable throttled download progress reporting."""

from __future__ import annotations

import itertools
import time

from meteora.agent.progress import emit_progress

_PROGRESS_IDS = itertools.count(1)


def format_size(size: int | float) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def format_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def download_progress_reporter():
    progress_id = next(_PROGRESS_IDS)
    start_time = time.monotonic()
    last_emit_time = 0.0
    last_percent = -1

    def report(done: int, total: int, *, force: bool = False) -> None:
        nonlocal last_emit_time, last_percent
        if total <= 0:
            return

        percent_value = min(100.0, done * 100 / total)
        percent = int(percent_value)
        now = time.monotonic()
        if not force and percent < 100 and percent == last_percent and now - last_emit_time < 2:
            return
        same_step = percent // 5 == last_percent // 5
        if not force and percent < 100 and now - last_emit_time < 2 and same_step:
            return

        last_percent = percent
        last_emit_time = now
        elapsed = max(now - start_time, 0.001)
        speed = done / elapsed
        eta = format_duration((total - done) / speed) if speed > 0 else "未知"
        filled = min(20, int(20 * percent / 100))
        bar = "█" * filled + "░" * (20 - filled)
        percent_label = f"{percent_value:5.1f}%" if 0 < percent_value < 10 else f"{percent:3d}%"
        emit_progress(
            f"下载进度#{progress_id} [{bar}] {percent_label} "
            f"({format_size(done)} / {format_size(total)}) "
            f"{format_size(speed)}/s ETA {eta}"
        )

    return report
