"""Reusable byte-granular download progress reporting."""

from __future__ import annotations

import itertools
import time

from aero.agent.progress import emit_progress

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
    start_time: float | None = None
    start_done = 0
    last_report: float | None = None
    completed = False

    def report(
        done: int,
        total: int,
        *,
        force: bool = False,
        reset_measurement: bool = False,
    ) -> None:
        nonlocal start_time, start_done, last_report, completed
        if total <= 0:
            return

        percent_value = min(100.0, done * 100 / total)
        percent = int(percent_value)
        now = time.monotonic()
        if completed:
            return
        if (
            last_report is not None
            and now - last_report < 5
            and done < total
            and not force
            and not reset_measurement
        ):
            return
        last_report = now
        completed = done >= total
        if start_time is None or reset_measurement:
            # A resumed transfer begins with bytes that were written during a
            # previous connection.  They are progress, not bytes transferred
            # by this connection, so establish a fresh measurement baseline.
            start_time = now
            start_done = done
            speed_label = "速度测量中"
            eta_label = "ETA 估算中"
        else:
            elapsed = max(now - start_time, 0.001)
            transferred = max(0, done - start_done)
            speed = transferred / elapsed
            speed_label = f"{format_size(speed)}/s"
            eta_label = (
                f"ETA {format_duration((total - done) / speed)}" if speed > 0 else "ETA 估算中"
            )
        filled = min(20, int(20 * percent / 100))
        bar = "█" * filled + "░" * (20 - filled)
        percent_label = f"{percent_value:5.1f}%" if 0 < percent_value < 10 else f"{percent:3d}%"
        emit_progress(
            f"下载进度#{progress_id} [{bar}] {percent_label} "
            f"({format_size(done)} / {format_size(total)}) "
            f"{speed_label} {eta_label}"
        )

    return report
