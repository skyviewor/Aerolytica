"""NOAA HRRR AWS Open Data provider using GRIB2 index byte ranges."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path

import httpx

from aero.adapters.gfs_adapter import GFSIndexEntry, parse_gfs_idx, select_gfs_entries
from aero.agent.progress import cancel_requested
from aero.datasets.models import (
    DatasetDownloadRequest,
    DatasetDownloadResult,
    DatasetSpec,
    DatasetVariable,
)
from aero.datasets.provider import ProgressCallback

DATASET_ID = "hrrr-conus-forecast"
PROVIDER_ID = "noaa-nodd"
BASE_URL = "https://noaa-hrrr-bdp-pds.s3.amazonaws.com"
CHUNK_SIZE = 1024 * 1024
PRODUCTS = ("wrfsfcf", "wrfprsf", "wrfnatf")
COMMON_VARIABLES = (
    DatasetVariable("TMP", "temperature", "K", ("temperature", "气温", "温度")),
    DatasetVariable("DPT", "dew point temperature", "K", ("dewpoint", "露点")),
    DatasetVariable("UGRD", "u-component of wind", "m/s", ("u wind", "纬向风")),
    DatasetVariable("VGRD", "v-component of wind", "m/s", ("v wind", "经向风")),
    DatasetVariable("APCP", "total precipitation", "kg/m^2", ("precipitation", "降水")),
    DatasetVariable("REFC", "composite reflectivity", "dBZ", ("reflectivity", "组合反射率")),
    DatasetVariable("GUST", "wind speed gust", "m/s", ("gust", "阵风")),
    DatasetVariable("VIS", "visibility", "m", ("visibility", "能见度")),
)

HRRR_SPEC = DatasetSpec(
    dataset_id=DATASET_ID,
    name="NOAA HRRR CONUS Forecast",
    provider_id=PROVIDER_ID,
    provider_name="NOAA Open Data Dissemination",
    domain="forecast",
    description="美国本土 3 km、逐小时更新的 High-Resolution Rapid Refresh 预报。",
    variables=COMMON_VARIABLES,
    spatial_coverage="CONUS，美国本土及邻近区域",
    temporal_coverage="2014-07-30 至接近实时",
    spatial_resolution="3 km",
    temporal_resolution="hourly forecast cycles and steps",
    file_formats=("GRIB2",),
    download_granularity="selected GRIB2 messages via .idx byte ranges",
    source_url="https://registry.opendata.aws/noaa-hrrr-pds/",
    citation_url="https://rapidrefresh.noaa.gov/hrrr/",
    supports_server_time_subset=True,
    supports_resume=False,
    notes=(
        "必须指定变量、UTC 起报时次 times 和 forecast_hours。",
        "product 支持 wrfsfcf（地表，默认）、wrfprsf（等压面）和 wrfnatf（原生层）。",
        "使用官方 .idx 按变量和层级下载，不会下载完整数百 MB GRIB2 文件。",
        "AWS 公共对象存储不支持经纬度区域裁剪。",
    ),
)


@dataclass(frozen=True)
class HrrrFile:
    date: str
    cycle: str
    forecast_hour: int
    product: str
    grib_url: str
    idx_url: str
    destination: Path
    selected: tuple[GFSIndexEntry, ...]
    missing: tuple[dict, ...]
    downloaded_bytes: int


class HrrrProvider:
    provider_id = PROVIDER_ID

    def __init__(
        self,
        *,
        base_url: str = BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._transport = transport

    def list_datasets(self) -> tuple[DatasetSpec, ...]:
        return (HRRR_SPEC,)

    async def download(
        self,
        request: DatasetDownloadRequest,
        on_progress: ProgressCallback | None = None,
    ) -> DatasetDownloadResult:
        if request.dataset_id != DATASET_ID:
            raise ValueError(f"{self.provider_id} 不支持数据集 {request.dataset_id}")
        variables = self._normalize_variables(request.variables)
        cycles = self._normalize_cycles(request.times)
        forecast_hours = self._normalize_forecast_hours(request.forecast_hours)
        product = self._normalize_product(request.product)
        levels = self._level_texts(request.levels)
        days = self._dates_between(
            self._parse_date(request.start_date, "start_date"),
            self._parse_date(request.end_date, "end_date"),
        )
        request.output_dir.mkdir(parents=True, exist_ok=True)

        results: list[HrrrFile] = []
        async with httpx.AsyncClient(
            timeout=None,
            follow_redirects=True,
            transport=self._transport,
        ) as client:
            for day in days:
                for cycle in cycles:
                    for forecast_hour in forecast_hours:
                        results.append(
                            await self._download_one(
                                client,
                                day,
                                cycle,
                                forecast_hour,
                                product,
                                variables,
                                levels,
                                request.output_dir,
                                on_progress,
                            )
                        )

        warnings = []
        if request.area is not None:
            warnings.append("HRRR AWS 源不支持区域裁剪，返回文件仍覆盖完整 CONUS 网格。")
        return DatasetDownloadResult(
            dataset_id=DATASET_ID,
            provider_id=self.provider_id,
            files=tuple(item.destination for item in results),
            source_urls=tuple(item.grib_url for item in results),
            warnings=tuple(warnings),
            metadata={
                "variables": list(variables),
                "levels": list(levels),
                "cycles": list(cycles),
                "forecast_hours": list(forecast_hours),
                "product": product,
                "selected_messages": sum(len(item.selected) for item in results),
                "total_bytes": sum(item.downloaded_bytes for item in results),
                "missing": [missing for item in results for missing in item.missing],
                "requires_local_subset": request.area is not None,
            },
        )

    async def _download_one(
        self,
        client: httpx.AsyncClient,
        day: date,
        cycle: str,
        forecast_hour: int,
        product: str,
        variables: tuple[str, ...],
        levels: tuple[str, ...],
        output_dir: Path,
        on_progress: ProgressCallback | None,
    ) -> HrrrFile:
        stem = f"hrrr.t{cycle}z.{product}{forecast_hour:02d}.grib2"
        grib_url = f"{self._base_url}/hrrr.{day:%Y%m%d}/conus/{stem}"
        idx_url = f"{grib_url}.idx"
        if on_progress:
            on_progress(f"正在读取 HRRR 索引：{day:%Y-%m-%d} {cycle}Z f{forecast_hour:02d}")
        idx_response = await client.get(idx_url)
        if idx_response.status_code == 404:
            raise ValueError(f"HRRR 远端文件不存在：{day:%Y-%m-%d} {cycle}Z f{forecast_hour:02d}")
        idx_response.raise_for_status()
        entries = parse_gfs_idx(idx_response.text)
        entries = await self._resolve_last_range(client, grib_url, entries)
        selected, missing = select_gfs_entries(list(entries), list(variables), list(levels) or None)
        if not selected:
            raise ValueError("HRRR .idx 中没有找到请求变量和层级")

        destination = (
            output_dir
            / "noaa-hrrr-bdp-pds"
            / f"hrrr.{day:%Y%m%d}"
            / "conus"
            / self._destination_name(stem, variables, levels)
        )
        total = sum(entry.byte_count or 0 for entry in selected)
        if on_progress:
            on_progress(f"HRRR 命中 {len(selected)} 个 GRIB message，准备下载")
        downloaded = await self._download_ranges(
            client, grib_url, tuple(selected), destination, total, on_progress
        )
        return HrrrFile(
            date=day.isoformat(),
            cycle=cycle,
            forecast_hour=forecast_hour,
            product=product,
            grib_url=grib_url,
            idx_url=idx_url,
            destination=destination,
            selected=tuple(selected),
            missing=tuple(missing),
            downloaded_bytes=downloaded,
        )

    @staticmethod
    async def _resolve_last_range(
        client: httpx.AsyncClient,
        grib_url: str,
        entries: list[GFSIndexEntry],
    ) -> tuple[GFSIndexEntry, ...]:
        if not entries or entries[-1].end_byte is not None:
            return tuple(entries)
        response = await client.head(grib_url)
        response.raise_for_status()
        size = int(response.headers.get("content-length", "0"))
        if size <= entries[-1].start_byte:
            raise RuntimeError("HRRR 远端 GRIB2 文件大小无效")
        return tuple(entries[:-1]) + (replace(entries[-1], end_byte=size - 1),)

    @staticmethod
    async def _download_ranges(
        client: httpx.AsyncClient,
        grib_url: str,
        entries: tuple[GFSIndexEntry, ...],
        destination: Path,
        total: int,
        on_progress: ProgressCallback | None,
    ) -> int:
        destination.parent.mkdir(parents=True, exist_ok=True)
        part = destination.with_suffix(destination.suffix + ".part")
        part.unlink(missing_ok=True)
        downloaded = 0
        with part.open("wb") as output:
            for entry in entries:
                async with client.stream(
                    "GET", grib_url, headers={"Range": entry.range_header}
                ) as response:
                    if response.status_code != 206:
                        if response.status_code == 200:
                            raise RuntimeError(
                                "HRRR 源站忽略了 Range 请求，已停止以避免下载完整文件"
                            )
                        response.raise_for_status()
                    async for chunk in response.aiter_bytes(CHUNK_SIZE):
                        if cancel_requested():
                            raise RuntimeError("下载已取消")
                        output.write(chunk)
                        downloaded += len(chunk)
                        if on_progress and total:
                            on_progress(downloaded, total)
        part.replace(destination)
        return downloaded

    async def search_variables(self, dataset_id: str, query: str = "") -> tuple[str, ...]:
        if dataset_id != DATASET_ID:
            raise ValueError(f"{self.provider_id} 不支持数据集 {dataset_id}")
        terms = [term.casefold() for term in query.split() if term.strip()]
        values = []
        for variable in COMMON_VARIABLES:
            text = " ".join((variable.name, variable.long_name, *variable.aliases)).casefold()
            if not terms or all(term in text for term in terms):
                values.append(variable.name)
        return tuple(values)

    @staticmethod
    def _normalize_variables(requested: tuple[str, ...]) -> tuple[str, ...]:
        if not requested:
            raise ValueError("HRRR 下载必须指定变量，例如 TMP、UGRD、VGRD、APCP 或 REFC")
        return tuple(dict.fromkeys(value.strip().upper() for value in requested if value.strip()))

    @staticmethod
    def _normalize_cycles(requested: tuple[str, ...]) -> tuple[str, ...]:
        if not requested:
            raise ValueError('HRRR 下载必须指定 UTC 起报时次，例如 times=["03:00"]')
        cycles = []
        invalid = []
        for value in requested:
            compact = value.strip().lower().removesuffix("z").replace(":", "")
            if len(compact) == 2:
                compact += "00"
            if len(compact) != 4 or not compact.isdigit() or compact[2:] != "00":
                invalid.append(value)
                continue
            hour = int(compact[:2])
            if hour > 23:
                invalid.append(value)
            else:
                cycles.append(f"{hour:02d}")
        if invalid:
            raise ValueError(f"HRRR 起报时次必须是整点 UTC：{'、'.join(invalid)}")
        return tuple(dict.fromkeys(cycles))

    @staticmethod
    def _normalize_forecast_hours(requested: tuple[int, ...]) -> tuple[int, ...]:
        if not requested:
            raise ValueError("HRRR 下载必须指定 forecast_hours，例如 [0, 1, 6]")
        invalid = [value for value in requested if isinstance(value, bool) or not 0 <= value <= 48]
        if invalid:
            raise ValueError(f"HRRR forecast_hours 必须在 0 至 48 之间：{invalid}")
        return tuple(dict.fromkeys(requested))

    @staticmethod
    def _normalize_product(requested: str) -> str:
        product = requested.strip().lower() or "wrfsfcf"
        if product not in PRODUCTS:
            raise ValueError(f"HRRR 不支持产品 {requested}，可选值：{', '.join(PRODUCTS)}")
        return product

    @staticmethod
    def _level_texts(levels: tuple[float, ...]) -> tuple[str, ...]:
        return tuple(f"{level:g} mb" for level in levels)

    @staticmethod
    def _destination_name(stem: str, variables: tuple[str, ...], levels: tuple[str, ...]) -> str:
        variable_text = "-".join(variables).lower()
        level_text = "-".join(level.replace(" ", "") for level in levels).lower()
        suffix = f".{variable_text}"
        if level_text:
            suffix += f".{level_text}"
        return stem.replace(".grib2", f"{suffix}.grib2")

    @staticmethod
    def _parse_date(value: str, label: str) -> date:
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{label} 必须使用 YYYY-MM-DD 格式") from exc

    @staticmethod
    def _dates_between(start: date, end: date) -> tuple[date, ...]:
        if end < start:
            raise ValueError("end_date 不能早于 start_date")
        return tuple(start + timedelta(days=offset) for offset in range((end - start).days + 1))
