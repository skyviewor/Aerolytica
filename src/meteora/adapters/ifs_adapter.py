"""IFS adapter using ECMWF Open Data .index files and HTTP Range requests."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import httpx

from meteora.adapters.cds_adapter import config_output_dir
from meteora.agent.progress import cancel_requested, emit_progress

CHUNK_SIZE = 5 * 1024 * 1024
HTTP_RETRY_ATTEMPTS = 5
HTTP_RETRY_BASE_DELAY = 2.0
HTTP_RETRY_MAX_DELAY = 30.0
DEFAULT_STREAM = "oper"
DEFAULT_TYPE = "fc"
DEFAULT_MODEL = "ifs"
DEFAULT_RESOL = "0p25"
ECMWF_IFS_BASE = "https://data.ecmwf.int/forecasts"
VALID_CYCLES = {"00", "06", "12", "18"}
VALID_MODELS = {"ifs", "aifs-single", "aifs-ens"}
VALID_STREAMS = {"oper", "wave", "enfo", "waef"}
VALID_LEVTYPES = {"sfc", "pl", "sol", "pt"}

MODEL_PATH = {
    "ifs": "ifs",
    "aifs-single": "aifs-single",
    "aifs-ens": "aifs-ens",
}

DEFAULT_TYPE_BY_MODEL_STREAM = {
    ("ifs", "oper"): "fc",
    ("ifs", "wave"): "fc",
    ("ifs", "enfo"): "ef",
    ("ifs", "waef"): "ef",
    ("aifs-single", "oper"): "fc",
    ("aifs-single", "wave"): "fc",
    ("aifs-ens", "enfo"): "pf",
    ("aifs-ens", "waef"): "pf",
}

IFS_FORECAST_SEGMENTS = (
    {"start": 0, "end": 144, "step": 3, "label": "0-144h by 3h"},
    {"start": 150, "end": 240, "step": 6, "label": "150-240h by 6h"},
)

IFS_FORECAST_SEGMENTS_SHORT = (
    {"start": 0, "end": 90, "step": 3, "label": "0-90h by 3h"},
)

ENFO_FORECAST_SEGMENTS = (
    {"start": 0, "end": 144, "step": 3, "label": "0-144h by 3h"},
    {"start": 150, "end": 360, "step": 6, "label": "150-360h by 6h"},
)

ENFO_FORECAST_SEGMENTS_SHORT = (
    {"start": 0, "end": 144, "step": 3, "label": "0-144h by 3h"},
)

AIFS_FORECAST_SEGMENTS = (
    {"start": 0, "end": 360, "step": 6, "label": "0-360h by 6h"},
)


def ifs_forecast_segments(cycle: str, stream: str = "oper", model: str = "ifs") -> tuple[dict, ...]:
    model = _normalize_model(model)
    stream = _normalize_stream(stream)
    if model in ("aifs-single", "aifs-ens"):
        return AIFS_FORECAST_SEGMENTS
    if stream in ("enfo", "waef"):
        if cycle in ("06", "18"):
            return ENFO_FORECAST_SEGMENTS_SHORT
        return ENFO_FORECAST_SEGMENTS
    if cycle in ("06", "18"):
        return IFS_FORECAST_SEGMENTS_SHORT
    return IFS_FORECAST_SEGMENTS


@dataclass(frozen=True)
class IFSIndexEntry:
    param: str
    levtype: str
    levelist: str | None
    step: str
    date: str
    time: str
    offset: int
    length: int
    raw: dict[str, Any]

    @property
    def range_header(self) -> str:
        return f"bytes={self.offset}-{self.offset + self.length - 1}"

    @property
    def byte_count(self) -> int:
        return self.length


@dataclass(frozen=True)
class IFSDownloadFile:
    step: int
    index_url: str
    grib_url: str
    file_path: Path
    selected_entries: list[IFSIndexEntry]
    missing: list[dict]
    downloaded_bytes: int
    source: str = "ecmwf"


class IFSAdapter:
    def __init__(self, base_url: str = ECMWF_IFS_BASE):
        self._base_url = base_url.rstrip("/")

    async def download(
        self,
        *,
        date: str,
        cycle: str,
        steps: list[int],
        variables: list[str],
        stream: str = DEFAULT_STREAM,
        typ: str = DEFAULT_TYPE,
        levtype: str | None = None,
        levels: list[str] | None = None,
        dest_dir: Path | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> list[IFSDownloadFile]:
        date = _normalize_date(date)
        cycle = _normalize_cycle(cycle)
        steps = _normalize_steps(steps)
        variables = _normalize_variables(variables)
        stream = _normalize_stream(stream)
        typ = _normalize_type(typ)
        levtype = _normalize_levtype(levtype) if levtype else None
        levels = _normalize_levels(levels)
        dest_root = dest_dir or config_output_dir()
        dest_root.mkdir(parents=True, exist_ok=True)

        results: list[IFSDownloadFile] = []
        for step in steps:
            if cancel_requested():
                raise RuntimeError("下载已取消")
            result = await self.download_one(
                date=date,
                cycle=cycle,
                step=step,
                variables=variables,
                stream=stream,
                typ=typ,
                levtype=levtype,
                levels=levels,
                dest_dir=dest_root,
                on_progress=on_progress,
            )
            results.append(result)
        return results

    async def download_one(
        self,
        *,
        date: str,
        cycle: str,
        step: int,
        variables: list[str],
        stream: str = DEFAULT_STREAM,
        typ: str = DEFAULT_TYPE,
        model: str = DEFAULT_MODEL,
        levtype: str | None = None,
        levels: list[str] | None = None,
        numbers: list[int] | None = None,
        dest_dir: Path | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> IFSDownloadFile:
        date = _normalize_date(date)
        cycle = _normalize_cycle(cycle)
        step = _normalize_step(step)
        variables = _normalize_variables(variables)
        stream = _normalize_stream(stream)
        typ = _normalize_type(typ)
        model = _normalize_model(model)
        levtype = _normalize_levtype(levtype) if levtype else None
        levels = _normalize_levels(levels)
        dest_root = dest_dir or config_output_dir()
        dest_root.mkdir(parents=True, exist_ok=True)
        stream_name = _get_stream_name(stream)

        grib_url = _build_grib_url(self._base_url, date, cycle, step, stream, typ, model)
        index_url = _build_index_url(grib_url)
        emit_progress(
            f"正在读取 {stream_name} 索引：{date} {cycle}z step={step}h，变量={', '.join(variables)}"
        )

        idx_text = await asyncio.to_thread(self._fetch_text, index_url)
        entries = _parse_ifs_index(idx_text)
        selected, missing = _select_ifs_entries(entries, variables, levtype, levels, numbers)
        if not selected:
            params = sorted(set(e.param for e in entries))
            levtypes = sorted(set(e.levtype for e in entries))
            level_samples = sorted(set(e.levelist for e in entries if e.levelist))
            raise RuntimeError(
                f"{stream_name} .index 中没有找到匹配字段。"
                f"请求: 变量={variables}, levtype={levtype or '不限'}, "
                f"levels={levels or '不限'}, numbers={numbers or '不限'}。\n"
                f"实际可用: 变量={', '.join(params[:20])}{'...(更多)' if len(params) > 20 else ''}, "
                f"levtype={', '.join(levtypes)}, "
                f"层级示例={', '.join(str(l) for l in level_samples[:10])}{'...(更多)' if len(level_samples) > 10 else ''}"
            )

        dest_path = _build_dest_path(
            date, cycle, step, variables, levtype, levels, dest_root, stream, typ, model
        )
        total_bytes = sum(e.byte_count for e in selected)
        emit_progress(
            f"命中 {len(selected)} 个 GRIB message，准备分块下载 "
            f"{_fmt_size(total_bytes) if total_bytes else '未知大小'}"
        )
        downloaded = await asyncio.to_thread(
            self._download_ranges,
            grib_url,
            selected,
            dest_path,
            total_bytes,
            on_progress,
        )
        emit_progress(f"{stream_name} 文件下载完成：{dest_path} ({_fmt_size(downloaded)})")
        return IFSDownloadFile(
            step=step,
            index_url=index_url,
            grib_url=grib_url,
            file_path=dest_path,
            selected_entries=selected,
            missing=missing,
            downloaded_bytes=downloaded,
        )

    @staticmethod
    def _fetch_text(url: str) -> str:
        with httpx.Client(timeout=30, follow_redirects=True) as client:
            resp = _request_with_retry(
                lambda: client.get(url),
                url=url,
                action="读取 IFS 索引",
            )
            return resp.text

    @staticmethod
    def _download_ranges(
        url: str,
        entries: list[IFSIndexEntry],
        dest: Path,
        total_bytes: int,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> int:
        tmp = dest.with_suffix(dest.suffix + ".part")
        if tmp.exists():
            tmp.unlink()
        if dest.exists():
            dest.unlink()

        done = 0
        with httpx.Client(timeout=None, follow_redirects=True) as client:
            with tmp.open("wb") as out:
                for entry in entries:
                    if cancel_requested():
                        raise RuntimeError("下载已取消")
                    headers = {"Range": entry.range_header}
                    with _stream_with_retry(
                        client,
                        url,
                        headers=headers,
                        action="下载 IFS GRIB 分块",
                    ) as resp:
                        for chunk in resp.iter_bytes(chunk_size=CHUNK_SIZE):
                            if cancel_requested():
                                raise RuntimeError("下载已取消")
                            out.write(chunk)
                            done += len(chunk)
                            if on_progress and total_bytes > 0:
                                on_progress(done, total_bytes)
        tmp.replace(dest)
        return done


def _request_with_retry(
    request_func: Callable[[], httpx.Response],
    *,
    url: str,
    action: str,
) -> httpx.Response:
    last_error: Exception | None = None
    for attempt in range(1, HTTP_RETRY_ATTEMPTS + 1):
        try:
            resp = request_func()
            if resp.status_code < 400:
                return resp
            if not _should_retry_status(resp.status_code) or attempt == HTTP_RETRY_ATTEMPTS:
                raise RuntimeError(_friendly_http_error(action, url, resp))
            delay = _retry_delay(resp, attempt)
            emit_progress(_retry_message(action, resp.status_code, attempt, delay))
            time.sleep(delay)
        except (httpx.TimeoutException, httpx.TransportError) as e:
            last_error = e
            if attempt == HTTP_RETRY_ATTEMPTS:
                raise RuntimeError(
                    f"{action}失败：网络连接异常（{e.__class__.__name__}: {e}），已重试 {attempt} 次。"
                ) from e
            delay = _retry_delay(None, attempt)
            emit_progress(f"{action}遇到网络异常，{delay:.0f}s 后重试（第 {attempt}/{HTTP_RETRY_ATTEMPTS} 次）")
            time.sleep(delay)
    raise RuntimeError(f"{action}失败：{last_error or '未知错误'}")


class _RetryingStream:
    def __init__(
        self,
        client: httpx.Client,
        url: str,
        *,
        headers: dict[str, str],
        action: str,
    ):
        self.client = client
        self.url = url
        self.headers = headers
        self.action = action
        self._ctx = None
        self._resp = None

    def __enter__(self):
        last_error: Exception | None = None
        for attempt in range(1, HTTP_RETRY_ATTEMPTS + 1):
            try:
                self._ctx = self.client.stream("GET", self.url, headers=self.headers)
                resp = self._ctx.__enter__()
                if resp.status_code in (200, 206):
                    self._resp = resp
                    return resp
                self._ctx.__exit__(None, None, None)
                self._ctx = None
                if not _should_retry_status(resp.status_code) or attempt == HTTP_RETRY_ATTEMPTS:
                    raise RuntimeError(_friendly_http_error(self.action, self.url, resp))
                delay = _retry_delay(resp, attempt)
                emit_progress(_retry_message(self.action, resp.status_code, attempt, delay))
                time.sleep(delay)
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_error = e
                if self._ctx is not None:
                    self._ctx.__exit__(None, None, None)
                    self._ctx = None
                if attempt == HTTP_RETRY_ATTEMPTS:
                    raise RuntimeError(
                        f"{self.action}失败：网络连接异常（{e.__class__.__name__}: {e}），已重试 {attempt} 次。"
                    ) from e
                delay = _retry_delay(None, attempt)
                emit_progress(f"{self.action}遇到网络异常，{delay:.0f}s 后重试（第 {attempt}/{HTTP_RETRY_ATTEMPTS} 次）")
                time.sleep(delay)
        raise RuntimeError(f"{self.action}失败：{last_error or '未知错误'}")

    def __exit__(self, exc_type, exc, tb):
        if self._ctx is not None:
            return self._ctx.__exit__(exc_type, exc, tb)
        return False


def _stream_with_retry(
    client: httpx.Client,
    url: str,
    *,
    headers: dict[str, str],
    action: str,
) -> _RetryingStream:
    return _RetryingStream(client, url, headers=headers, action=action)


def _should_retry_status(status_code: int) -> bool:
    return status_code == 429 or 500 <= status_code < 600


def _retry_delay(resp: httpx.Response | None, attempt: int) -> float:
    if resp is not None:
        retry_after = resp.headers.get("retry-after")
        if retry_after:
            try:
                return min(max(float(retry_after), 1.0), HTTP_RETRY_MAX_DELAY)
            except ValueError:
                pass
    return min(HTTP_RETRY_BASE_DELAY * (2 ** (attempt - 1)), HTTP_RETRY_MAX_DELAY)


def _retry_message(action: str, status_code: int, attempt: int, delay: float) -> str:
    if status_code == 429:
        reason = "请求过于频繁，ECMWF 正在限流"
    else:
        reason = f"服务暂时不可用（HTTP {status_code}）"
    return f"{action}遇到{reason}，{delay:.0f}s 后自动重试（第 {attempt}/{HTTP_RETRY_ATTEMPTS} 次）"


def _friendly_http_error(action: str, url: str, resp: httpx.Response) -> str:
    if resp.status_code == 429:
        return (
            f"{action}失败：ECMWF 返回 429 Too Many Requests，表示请求过于频繁/被限流。"
            "请稍后重试，或减少并发/分批下载。"
            f" URL: {url}"
        )
    if 500 <= resp.status_code < 600:
        return f"{action}失败：ECMWF 服务暂时不可用（HTTP {resp.status_code}）。URL: {url}"
    return f"{action}失败：HTTP {resp.status_code}。URL: {url}"


def _build_grib_url(
    base_url: str,
    date: str,
    cycle: str,
    step: int,
    stream: str = DEFAULT_STREAM,
    typ: str = DEFAULT_TYPE,
    model: str = DEFAULT_MODEL,
) -> str:
    model_path = MODEL_PATH.get(model, model)
    return (
        f"{base_url.rstrip('/')}/{date}/{cycle}z/{model_path}/{DEFAULT_RESOL}/{stream}/"
        f"{date}{cycle}0000-{step}h-{stream}-{typ}.grib2"
    )


def _build_index_url(grib_url: str) -> str:
    return grib_url.replace(".grib2", ".index")


def _parse_ifs_index(text: str) -> list[IFSIndexEntry]:
    entries: list[IFSIndexEntry] = []
    for raw_line in text.splitlines():
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            record = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        offset = record.get("_offset")
        length = record.get("_length")
        if offset is None or length is None:
            continue
        param = record.get("param", "")
        levtype = record.get("levtype", "")
        levelist = record.get("levelist") or None
        step = str(record.get("step", ""))
        date = str(record.get("date", ""))
        time = str(record.get("time", ""))
        entries.append(
            IFSIndexEntry(
                param=param,
                levtype=levtype,
                levelist=levelist,
                step=step,
                date=date,
                time=time,
                offset=int(offset),
                length=int(length),
                raw=record,
            )
        )
    return entries


def _select_ifs_entries(
    entries: list[IFSIndexEntry],
    variables: list[str],
    levtype: str | None = None,
    levels: list[str] | None = None,
    numbers: list[int] | None = None,
) -> tuple[list[IFSIndexEntry], list[dict]]:
    variables = _normalize_variables(variables)
    levels = _normalize_levels(levels) if levels is not None else None
    numbers_set = set(str(n) for n in numbers) if numbers else None
    selected: list[IFSIndexEntry] = []
    missing: list[dict] = []

    for variable in variables:
        candidates = [e for e in entries if e.param == variable]
        if levtype:
            candidates = [e for e in candidates if e.levtype == levtype]
        if levels:
            for level in levels:
                matched = [
                    e for e in candidates
                    if e.levelist is not None and _normalize_levelist(e.levelist) == level
                ]
                if matched:
                    selected.extend(matched)
                else:
                    missing.append(
                        {"variable": variable, "levtype": levtype, "level": level}
                    )
        elif candidates:
            selected.extend(candidates)
        else:
            missing.append(
                {"variable": variable, "levtype": levtype, "level": None}
            )

    if numbers_set:
        selected = [e for e in selected if e.raw.get("number", "") in numbers_set]

    return selected, missing


def _build_dest_path(
    date: str,
    cycle: str,
    step: int,
    variables: list[str],
    levtype: str | None,
    levels: list[str] | None,
    dest_dir: Path,
    stream: str = DEFAULT_STREAM,
    typ: str = DEFAULT_TYPE,
    model: str = DEFAULT_MODEL,
) -> Path:
    model = _normalize_model(model)
    stream = _normalize_stream(stream)
    typ = _normalize_type(typ)
    vars_part = "_".join(_normalize_variables(variables))
    if levtype:
        vars_part = f"{levtype}_{vars_part}"
    if levels:
        level_hash = hashlib.sha1("|".join(levels).encode()).hexdigest()[:8]
        vars_part = f"{vars_part}_{level_hash}"
    return (
        dest_dir
        / f"{model}_{date}_{cycle}z_{stream}_{typ}_step{step:03d}_{vars_part}.grib2"
    )


def _build_request_id(
    date: str,
    cycle: str,
    step: int,
    variables: list[str],
    levtype: str | None,
    levels: list[str] | None,
    stream: str = DEFAULT_STREAM,
    typ: str = DEFAULT_TYPE,
    model: str = DEFAULT_MODEL,
) -> str:
    normalized_date = _normalize_date(date)
    normalized_cycle = _normalize_cycle(cycle)
    normalized_step = _normalize_step(step)
    normalized_stream = _normalize_stream(stream)
    normalized_type = _normalize_type(typ)
    normalized_model = _normalize_model(model)
    key = "|".join(
        [
            normalized_date,
            normalized_cycle,
            str(normalized_step),
            normalized_stream,
            normalized_type,
            normalized_model,
            levtype or "",
            ",".join(_normalize_variables(variables)),
            ",".join(levels or []),
        ]
    )
    digest = hashlib.sha1(key.encode()).hexdigest()[:10]
    return (
        f"{normalized_model}-{normalized_date}-{normalized_cycle}-{normalized_stream}-{normalized_type}-"
        f"step{normalized_step:03d}-{digest}"
    )


def is_ifs_step_available(step: int, cycle: str, stream: str = "oper", model: str = "ifs") -> bool:
    step = _normalize_step(step)
    cycle = _normalize_cycle(cycle)
    segments = ifs_forecast_segments(cycle, stream=stream, model=model)
    for segment in segments:
        if segment["start"] <= step <= segment["end"]:
            return (step - segment["start"]) % segment["step"] == 0
    return False


def ifs_forecast_steps_for_range(
    *,
    start_step: int = 0,
    end_step: int | None = None,
    duration_hours: int | None = None,
    cycle: str = "00",
    stream: str = "oper",
    model: str = "ifs",
) -> list[int]:
    start_step = _normalize_step(start_step)
    cycle = _normalize_cycle(cycle)
    if duration_hours is not None:
        resolved_end = start_step + int(duration_hours)
    elif end_step is not None:
        resolved_end = _normalize_step(end_step)
    else:
        resolved_end = start_step
    if resolved_end < start_step:
        raise ValueError("end_step 不能小于 start_step")
    steps = [
        step
        for step in range(start_step, resolved_end + 1)
        if is_ifs_step_available(step, cycle, stream=stream, model=model)
    ]
    return steps


def _normalize_date(date: str) -> str:
    cleaned = re.sub(r"[^0-9]", "", str(date))
    if not re.fullmatch(r"\d{8}", cleaned):
        raise ValueError("date 必须是 YYYYMMDD 或 YYYY-MM-DD")
    return cleaned


def _normalize_cycle(cycle: str) -> str:
    cleaned = str(cycle).strip().lower().replace("z", "")
    if re.fullmatch(r"\d", cleaned):
        cleaned = f"0{cleaned}"
    if cleaned not in VALID_CYCLES:
        raise ValueError("cycle 只支持 00、06、12、18")
    return cleaned


def _normalize_step(step: int) -> int:
    value = int(step)
    if value < 0 or value > 360:
        raise ValueError("step 必须在 0 到 360 之间")
    return value


def _normalize_steps(steps: list[int]) -> list[int]:
    if not steps:
        raise ValueError("steps 不能为空")
    return [_normalize_step(s) for s in steps]


def _normalize_variables(variables: list[str]) -> list[str]:
    if not variables:
        raise ValueError("variables 不能为空")
    normalized = []
    for variable in variables:
        value = str(variable).strip().lower()
        if not value:
            continue
        normalized.append(value)
    if not normalized:
        raise ValueError("variables 不能为空")
    return normalized


def _normalize_stream(stream: str) -> str:
    value = str(stream or DEFAULT_STREAM).strip().lower()
    if not value:
        return DEFAULT_STREAM
    if value not in VALID_STREAMS:
        raise ValueError(f"stream 只支持 {', '.join(sorted(VALID_STREAMS))}")
    return value


def _normalize_type(typ: str) -> str:
    value = str(typ or DEFAULT_TYPE).strip().lower()
    if not value:
        return DEFAULT_TYPE
    return value


def _normalize_model(model: str) -> str:
    value = str(model or DEFAULT_MODEL).strip().lower()
    if not value:
        return DEFAULT_MODEL
    if value not in VALID_MODELS:
        raise ValueError(f"model 只支持 {', '.join(sorted(VALID_MODELS))}")
    return value


STREAM_DISPLAY_NAMES: dict[str, str] = {
    "oper": "IFS 大气预报",
    "wave": "IFS 海浪预报",
    "enfo": "IFS 集合预报",
    "waef": "IFS 集合海浪",
}


def _get_stream_name(stream: str, model: str = DEFAULT_MODEL) -> str:
    model_display = {"ifs": "IFS", "aifs-single": "AIFS", "aifs-ens": "AIFS 集合"}
    prefix = model_display.get(model, model.upper())
    stream_display = {
        "oper": f"{prefix} 大气预报",
        "wave": f"{prefix} 海浪预报",
        "enfo": f"{prefix} 集合预报",
        "waef": f"{prefix} 集合海浪",
    }
    return stream_display.get(stream, f"{prefix} {stream}")


def _normalize_levtype(levtype: str) -> str | None:
    value = str(levtype).strip().lower()
    if not value:
        return None
    return value


def _normalize_levels(levels: list[str] | None) -> list[str] | None:
    if levels is None:
        return None
    normalized = [_normalize_levelist(level) for level in levels if str(level).strip()]
    return normalized or None


def _normalize_levelist(levelist: str) -> str:
    return str(levelist).strip()


def _fmt_size(size: int) -> str:
    value = float(size)
    for unit in ["B", "KB", "MB", "GB"]:
        if value < 1024:
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"
