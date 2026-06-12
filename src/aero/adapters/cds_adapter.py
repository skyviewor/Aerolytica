"""CDS API adapter — generic downloader for any CDS dataset."""

import asyncio
import shutil
import zipfile
from collections.abc import Callable
from pathlib import Path

import httpx
import structlog

from aero.agent.progress import cancel_requested, emit_progress
from aero.core.types import DownloadResult

logger = structlog.get_logger()

CHUNK_SIZE = 5 * 1024 * 1024  # 5 MB
DOWNLOAD_TIMEOUT = httpx.Timeout(connect=30.0, read=30.0, write=30.0, pool=30.0)


class CDSAdapter:
    def __init__(self, cds_url: str, cds_key: str):
        self._url = cds_url
        self._key = cds_key

    # ── public two-step API ────────────────────────────────────────────

    async def submit(
        self,
        dataset_id: str,
        variables: list[str],
        year: int,
        month: int,
        day: int | None = None,
        pressure_level: int | None = None,
        area: list[float] | None = None,
        data_format: str = "netcdf",
        request_overrides: dict | None = None,
        *,
        on_submitted: Callable[[str, str, Path], None] | None = None,
        on_request_id: Callable[[str, Path], None] | None = None,
    ) -> dict:
        """Submit a CDS request and return metadata.

        on_submitted(request_id, download_url, dest_path) is called after
        the CDS task completes and the download URL is known.
        on_request_id(request_id, dest_path) is called immediately after
        CDS accepts the request, before remote polling.
        """
        if data_format not in ("netcdf", "grib"):
            raise ValueError(f"不支持的格式: {data_format}，仅支持 netcdf 和 grib")

        dest_path = self._build_dest_path(dataset_id, variables, year, month, day,
                                           pressure_level, data_format)
        request = self._build_request(dataset_id, variables, year, month, day,
                                      pressure_level, area, data_format, request_overrides)
        if cancel_requested():
            raise RuntimeError("下载已取消")

        emit_progress(
            f"正在提交数据请求：{dataset_id}，变量={', '.join(variables)}，"
            f"时间={year}-{month:02d}{f'-{day:02d}' if day else ''}, "
            f"区域={area or 'global'}"
        )

        result = await asyncio.to_thread(
            self._submit_to_cds,
            dataset_id,
            request,
        )
        request_id = _request_id_from_result(result)
        if not request_id:
            raise RuntimeError("CDS 未返回 request_id")
        if on_request_id is not None:
            on_request_id(request_id, dest_path)

        result = await self._poll_until_complete(result, request_id)
        download_info = await self._get_download_info(result)
        download_url = download_info["download_url"]

        # ── save immediately via callback before any cancellable I/O ──
        if on_submitted is not None:
            on_submitted(request_id, download_url, dest_path)

        url_head = _head_url(download_url)
        total_bytes = download_info.get("total_bytes") or url_head.get("content_length", 0)
        accept_ranges = url_head.get("accept_ranges", "")

        meta = {
            "request_id": request_id,
            "download_url": download_url,
            "total_bytes": total_bytes,
            "accept_ranges": accept_ranges,
            "dest_path": dest_path,
            "request_params": {
                "dataset_id": dataset_id,
                "variables": variables,
                "year": year,
                "month": month,
                "day": day,
                "pressure_level": pressure_level,
                "area": area,
                "data_format": data_format,
            },
        }

        # Emit progress AFTER building meta — even if user cancelled,
        # the caller can still use the metadata.
        try:
            emit_progress(
                f"远端数据准备完成。request_id={request_id}，"
                f"文件大小: {_fmt_size(total_bytes) if total_bytes else '未知'}"
                f"{'，支持断点续传' if accept_ranges == 'bytes' else ''}"
            )
        except RuntimeError:
            if cancel_requested():
                pass  # user cancelled but metadata is ready

        return meta

    async def fetch(
        self,
        download_url: str,
        dest_path: Path,
        resume_from: int = 0,
        on_progress: Callable[[int, int], None] | None = None,
        total_bytes: int = 0,
    ) -> int:
        """Download from CDS URL to dest_path with HTTP Range resume.

        Returns the final file size in bytes.
        """
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        existing_size = 0
        if dest_path.exists():
            existing_size = dest_path.stat().st_size

        if total_bytes > 0 and existing_size >= total_bytes:
            _normalize_downloaded_file(dest_path)
            file_size = dest_path.stat().st_size
            if file_size >= total_bytes:
                emit_progress(
                    f"本地文件已完整，无需重复下载：{dest_path} ({_fmt_size(file_size)})"
                )
                return file_size

        if resume_from == 0 and existing_size > 0:
            resume_from = existing_size

        if resume_from > 0:
            emit_progress(f"从上次中断的位置继续下载（已完成 {_fmt_size(resume_from)}）...")
        else:
            if dest_path.exists():
                dest_path.unlink()

        await asyncio.to_thread(
            self._download_file, download_url, dest_path, resume_from, on_progress, total_bytes,
        )

        _normalize_downloaded_file(dest_path)

        if cancel_requested():
            raise RuntimeError("下载已取消")

        file_size = dest_path.stat().st_size
        emit_progress(f"文件下载完成：{dest_path} ({_fmt_size(file_size)})")
        return file_size

    # ── convenience: submit + fetch in one call ─────────────────────────

    async def download(
        self,
        dataset_id: str,
        variables: list[str],
        year: int,
        month: int,
        dest_dir: Path,
        day: int | None = None,
        pressure_level: int | None = None,
        area: list[float] | None = None,
        data_format: str = "netcdf",
        request_overrides: dict | None = None,
        on_progress: Callable[[int, int], None] | None = None,
    ) -> DownloadResult:
        """Submit + fetch in one call (backward-compatible)."""
        dest_dir.mkdir(parents=True, exist_ok=True)

        meta = await self.submit(
            dataset_id=dataset_id,
            variables=variables,
            year=year,
            month=month,
            day=day,
            pressure_level=pressure_level,
            area=area,
            data_format=data_format,
            request_overrides=request_overrides,
        )

        file_size = await self.fetch(
            download_url=meta["download_url"],
            dest_path=meta["dest_path"],
            on_progress=on_progress,
        )

        return DownloadResult(
            source="cds",
            file_path=meta["dest_path"],
            variables=variables,
            time_range={"year": year, "month": f"{month:02d}", "day": day},
            region={"north": area[0], "west": area[1], "south": area[2], "east": area[3]}
            if area
            else None,
            params={
                "pressure_level": pressure_level,
                "dataset": dataset_id,
                "request_id": meta["request_id"],
                "download_url": meta["download_url"],
                "total_bytes": meta["total_bytes"],
                "requested_data_format": data_format,
                "requested_download_format": "unarchived",
                "actual_file_format": _detect_file_format(meta["dest_path"]),
            },
        )

    # ── internal helpers ───────────────────────────────────────────────

    @staticmethod
    def _build_dest_path(
        dataset_id: str, variables: list[str], year: int, month: int,
        day: int | None, pressure_level: int | None, data_format: str,
    ) -> Path:
        ext = ".grib" if data_format == "grib" else ".nc"
        pl_str = f"pl{pressure_level}" if pressure_level else "sfc"
        var_str = "_".join(variables)
        date_str = f"{year}{month:02d}{day:02d}" if day else f"{year}{month:02d}"
        ds_short = dataset_id.replace("reanalysis-", "").replace("derived-", "")
        filename = f"cds_{ds_short}_{var_str}_{pl_str}_{date_str}{ext}"
        return Path(config_output_dir()) / filename

    def _build_request(
        self, dataset_id: str, variables: list[str], year: int, month: int,
        day: int | None, pressure_level: int | None, area: list[float] | None,
        data_format: str, request_overrides: dict | None,
    ) -> dict:
        request: dict = {}
        if request_overrides:
            request.update(request_overrides)
        else:
            request["product_type"] = ["reanalysis"]
            request["data_format"] = data_format
            request["download_format"] = "unarchived"
            if "monthly" not in dataset_id:
                days = [f"{day:02d}"] if day else [f"{d:02d}" for d in range(1, 32)]
                request["day"] = days
                request["time"] = [f"{h:02d}:00" for h in range(0, 24)]

        request["variable"] = variables
        request["year"] = [str(year)]
        request["month"] = [f"{month:02d}"]
        if pressure_level:
            request["pressure_level"] = [str(pressure_level)]
        if area:
            request["area"] = area
        return request

    def _submit_to_cds(self, dataset: str, request: dict):
        import cdsapi

        client = cdsapi.Client(
            url=self._url,
            key=self._key,
            quiet=True,
            progress=False,
            wait_until_complete=False,
            delete=False,
        )
        if cancel_requested():
            raise RuntimeError("下载已取消")
        return client.retrieve(dataset, request)

    async def _poll_until_complete(self, result, request_id: str):
        sleep = 1.0
        last_state: str | None = None

        while True:
            state = await self._read_remote_state(result)
            if state != last_state:
                emit_progress(_remote_status_message(state, request_id))
                last_state = state

            if state == "completed":
                return result

            if state == "failed":
                raise RuntimeError(_remote_error_message(result) or "CDS 远端任务失败")

            if state not in ("queued", "running", "accepted", "submitted"):
                raise RuntimeError(f"未知 CDS 任务状态: {state}")

            if cancel_requested():
                raise RuntimeError("下载已取消")

            await asyncio.sleep(sleep)
            sleep = min(sleep * 1.5, 30)
            await self._refresh_remote_state(result, request_id)

    async def _read_remote_state(self, result) -> str:
        if hasattr(result, "reply"):
            return _normalize_remote_state(result.reply.get("state", "unknown"))
        if hasattr(result, "status"):
            return _normalize_remote_state(await asyncio.to_thread(lambda: result.status))
        return "unknown"

    async def _refresh_remote_state(self, result, request_id: str) -> None:
        if hasattr(result, "update"):
            await asyncio.to_thread(result.update, request_id)
            return
        if hasattr(result, "status"):
            await asyncio.to_thread(lambda: result.status)

    async def _get_download_info(self, result) -> dict:
        if hasattr(result, "location"):
            return {
                "download_url": result.location,
                "total_bytes": int(getattr(result, "content_length", 0) or 0),
            }

        if hasattr(result, "get_results"):
            results = await asyncio.to_thread(result.get_results)
            return {
                "download_url": results.location,
                "total_bytes": int(getattr(results, "content_length", 0) or 0),
            }

        raise RuntimeError("CDS 远端任务已完成，但未返回可下载文件地址")

    @staticmethod
    def _download_file(
        url: str,
        dest: Path,
        resume_from: int = 0,
        on_progress: Callable[[int, int], None] | None = None,
        known_total: int = 0,
    ) -> None:
        headers = {}
        mode = "ab" if resume_from > 0 else "wb"
        if resume_from > 0:
            headers["Range"] = f"bytes={resume_from}-"

        with dest.open(mode) as f:
            with httpx.stream(
                "GET",
                url,
                headers=headers,
                timeout=DOWNLOAD_TIMEOUT,
                follow_redirects=True,
            ) as resp:
                if resp.status_code not in (200, 206):
                    resp.raise_for_status()
                content_length = resp.headers.get("content-length")
                total = int(content_length) if content_length else 0
                if total == 0 and known_total > 0:
                    total = known_total - resume_from
                offset = resume_from
                for chunk in resp.iter_bytes(chunk_size=CHUNK_SIZE):
                    if cancel_requested():
                        raise RuntimeError("下载已取消")
                    f.write(chunk)
                    offset += len(chunk)
                    if on_progress and total > 0:
                        on_progress(offset, resume_from + total)


def config_output_dir() -> Path:
    from pathlib import Path as _Path
    cwd = _Path.cwd()
    for parent in [cwd, *cwd.parents]:
        config_path = parent / "aero.yaml"
        if config_path.exists():
            from aero.core.config import AeroConfig
            cfg = AeroConfig.load(config_path)
            return parent / cfg.output.data_dir
    from aero.core.config import AeroConfig
    cfg = AeroConfig.create_default()
    return cwd / cfg.output.data_dir


# ── static helpers ─────────────────────────────────────────────────────

def _head_url(url: str) -> dict:
    try:
        with httpx.Client(timeout=15, follow_redirects=True) as client:
            resp = client.head(url)
            return {
                "content_length": int(resp.headers.get("content-length", 0)),
                "accept_ranges": resp.headers.get("accept-ranges", ""),
            }
    except Exception:
        return {"content_length": 0, "accept_ranges": ""}


def _request_id_from_result(result) -> str:
    if hasattr(result, "reply"):
        return result.reply.get("request_id", "")
    if hasattr(result, "request_id"):
        return str(result.request_id)
    return ""


def _normalize_remote_state(state: str) -> str:
    state = str(state).lower()
    if state == "successful":
        return "completed"
    if state in ("rejected", "dismissed", "deleted"):
        return "failed"
    return state


def _remote_error_message(result) -> str:
    if hasattr(result, "reply"):
        error = result.reply.get("error", {})
        message = error.get("message") or "CDS 远端任务失败"
        reason = error.get("reason")
        return f"{message}{f'。{reason}' if reason else ''}"
    if hasattr(result, "json"):
        try:
            data = result.json
        except Exception as exc:
            return f"CDS 远端任务失败: {exc}"
        return str(data.get("error") or data.get("message") or data)
    return ""


def _remote_status_message(state: str, request_id: str) -> str:
    labels = {
        "accepted": "CDS 已接受请求",
        "submitted": "数据请求已提交",
        "queued": "远端正在排队准备数据",
        "running": "远端正在处理数据",
        "completed": "远端数据准备完成",
    }
    return f"{labels.get(state, f'CDS 远端状态: {state}')}。request_id={request_id}"


def _fmt_size(size: int) -> str:
    for unit in ["B", "KB", "MB", "GB"]:
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


def _is_netcdf(path: Path) -> bool:
    magic = path.read_bytes()[:4]
    return magic[:3] == b"CDF" or magic[:4] == b"\x89HDF"


def _detect_file_format(path: Path) -> str:
    magic = path.read_bytes()[:4]
    if magic[:3] == b"CDF":
        return "netcdf3"
    if magic[:4] == b"\x89HDF":
        return "netcdf4/hdf5"
    if magic[:2] == b"PK":
        return "zip"
    if magic[:4] == b"GRIB":
        return "grib"
    return "unknown"


def _is_grib(path: Path) -> bool:
    magic = path.read_bytes()[:4]
    return magic[:4] == b"GRIB"


def _normalize_downloaded_file(dest_path: Path) -> None:
    """Convert CDS ZIP responses into a real file (NetCDF or GRIB)."""
    if not zipfile.is_zipfile(dest_path):
        return

    extract_dir = dest_path.with_suffix(dest_path.suffix + ".parts")
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    extract_dir.mkdir(parents=True)

    with zipfile.ZipFile(dest_path) as archive:
        archive.extractall(extract_dir)

    data_files = sorted(
        p for p in extract_dir.rglob("*")
        if p.is_file() and _is_data_like_name(p)
    )
    if not data_files:
        raise RuntimeError("CDS 返回 ZIP 包，但其中没有数据文件")

    if len(data_files) == 1:
        shutil.move(str(data_files[0]), dest_path)
        shutil.rmtree(extract_dir)
        return

    if any(_is_grib(f) for f in data_files):
        shutil.move(str(data_files[0]), dest_path)
        shutil.rmtree(extract_dir)
        return

    try:
        import xarray as xr

        datasets = [xr.open_dataset(path) for path in data_files]
        merged = xr.merge(datasets, compat="override")
        merged.to_netcdf(dest_path)
        for ds in datasets:
            ds.close()
        merged.close()
        shutil.rmtree(extract_dir)
    except Exception as e:
        raise RuntimeError(f"CDS 返回多个数据文件，但自动合并失败: {e}") from e


def _is_data_like_name(path: Path) -> bool:
    return path.suffix.lower() in {".nc", ".nc4", ".cdf", ".netcdf", ".grb", ".grib"}
