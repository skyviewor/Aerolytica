"""UCSB CHG CHIRPS monthly NetCDF provider."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

import httpx

from aero.agent.progress import cancel_requested
from aero.datasets.models import (
    DatasetDownloadRequest,
    DatasetDownloadResult,
    DatasetSpec,
    DatasetVariable,
)
from aero.datasets.provider import ProgressCallback

DATASET_ID = "chirps-v2-daily-p05"
PROVIDER_ID = "ucsb-chg"
BASE_URL = "https://data.chc.ucsb.edu/products/CHIRPS-2.0/global_daily/netcdf/p05/by_month"
CHUNK_SIZE = 1024 * 1024
MONTH_FILE_RE = re.compile(r"chirps-v2\.0\.(\d{4})\.(\d{2})\.days_p05\.nc")

CHIRPS_SPEC = DatasetSpec(
    dataset_id=DATASET_ID,
    name="CHIRPS v2.0 Global Daily Precipitation (0.05 degree)",
    provider_id=PROVIDER_ID,
    provider_name="UCSB Climate Hazards Center",
    domain="observations",
    description=(
        "全球陆地区域的日降水观测融合数据，结合卫星估计与站点观测，"
        "适用于干旱监测和降水气候分析。"
    ),
    variables=(
        DatasetVariable(
            name="precip",
            long_name="daily precipitation",
            units="mm/day",
            aliases=("precipitation", "rainfall", "降水", "降雨"),
        ),
    ),
    spatial_coverage="全球陆地，约 50°S–50°N",
    temporal_coverage="1981-01-01 至接近实时",
    spatial_resolution="0.05 degree",
    temporal_resolution="daily",
    file_formats=("NetCDF4",),
    download_granularity="monthly file",
    source_url=f"{BASE_URL}/",
    citation_url="https://www.chc.ucsb.edu/data/chirps",
    supports_resume=True,
    notes=(
        "远端按整月文件提供；指定日期范围需要先下载涉及月份，再在本地裁剪。",
        "远端文件不支持按区域裁剪；区域裁剪应在下载后本地执行。",
    ),
)


def parse_iso_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} 必须使用 YYYY-MM-DD 格式") from exc


def months_between(start: date, end: date) -> tuple[tuple[int, int], ...]:
    if end < start:
        raise ValueError("end_date 不能早于 start_date")
    months: list[tuple[int, int]] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append((year, month))
        month += 1
        if month == 13:
            year += 1
            month = 1
    return tuple(months)


class ChirpsProvider:
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
        return (CHIRPS_SPEC,)

    def build_month_url(self, year: int, month: int) -> str:
        return f"{self._base_url}/chirps-v2.0.{year}.{month:02d}.days_p05.nc"

    async def download(
        self,
        request: DatasetDownloadRequest,
        on_progress: ProgressCallback | None = None,
    ) -> DatasetDownloadResult:
        if request.dataset_id != DATASET_ID:
            raise ValueError(f"{self.provider_id} 不支持数据集 {request.dataset_id}")
        requested_variables = {value.casefold() for value in request.variables}
        valid_variables = {"precip", "precipitation", "rainfall", "降水", "降雨"}
        if requested_variables and not requested_variables <= valid_variables:
            unsupported = ", ".join(sorted(requested_variables - valid_variables))
            raise ValueError(f"CHIRPS 不支持变量: {unsupported}")

        start = parse_iso_date(request.start_date, "start_date")
        end = parse_iso_date(request.end_date, "end_date")
        months = months_between(start, end)
        request.output_dir.mkdir(parents=True, exist_ok=True)

        files: list[Path] = []
        urls: list[str] = []
        reused: list[Path] = []
        async with httpx.AsyncClient(
            timeout=None,
            follow_redirects=True,
            transport=self._transport,
        ) as client:
            remote_sizes = await self._check_month_availability(client, months)
            for index, (year, month) in enumerate(months, start=1):
                url = self.build_month_url(year, month)
                path = request.output_dir / Path(url).name
                if on_progress:
                    on_progress(
                        f"CHIRPS 正在获取第 {index}/{len(months)} 个月文件："
                        f"{year}-{month:02d}"
                    )
                was_reused = await self._download_with_resume(
                    client,
                    url,
                    path,
                    on_progress,
                    remote_sizes.get((year, month)),
                )
                files.append(path)
                urls.append(url)
                if was_reused:
                    reused.append(path)

        warnings = [
            "CHIRPS 远端按整月提供，返回文件尚未裁剪到精确日期范围。",
        ]
        if request.area is not None:
            warnings.append("CHIRPS 远端不支持区域裁剪，返回文件尚未裁剪到请求区域。")
        return DatasetDownloadResult(
            dataset_id=DATASET_ID,
            provider_id=self.provider_id,
            files=tuple(files),
            source_urls=tuple(urls),
            reused_files=tuple(reused),
            warnings=tuple(warnings),
            metadata={
                "requested_start_date": request.start_date,
                "requested_end_date": request.end_date,
                "download_granularity": "monthly file",
                "requires_local_subset": True,
            },
        )

    async def _download_with_resume(
        self,
        client: httpx.AsyncClient,
        url: str,
        destination: Path,
        on_progress: ProgressCallback | None,
        remote_size: int | None = None,
    ) -> bool:
        if remote_size is None:
            remote_size = await self._remote_size(client, url)
        complete_file_exists = (
            destination.exists()
            and remote_size is not None
            and destination.stat().st_size == remote_size
        )
        if complete_file_exists:
            if on_progress:
                on_progress(f"本地文件已完整，跳过下载：{destination.name}")
            return True

        part = destination.with_suffix(destination.suffix + ".part")
        offset = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        async with client.stream("GET", url, headers=headers) as response:
            if response.status_code == 416 and remote_size is not None and offset == remote_size:
                part.replace(destination)
                return True
            response.raise_for_status()
            if offset and response.status_code != 206:
                offset = 0
                part.unlink(missing_ok=True)
            mode = "ab" if offset else "wb"
            downloaded = offset
            total = remote_size or self._response_total(response, offset)
            with part.open(mode) as output:
                async for chunk in response.aiter_bytes(CHUNK_SIZE):
                    if cancel_requested():
                        raise RuntimeError("下载已取消")
                    output.write(chunk)
                    downloaded += len(chunk)
                    if on_progress and total:
                        on_progress(downloaded, total)
        part.replace(destination)
        return False

    @staticmethod
    async def _remote_size(client: httpx.AsyncClient, url: str) -> int | None:
        response = await client.head(url)
        if response.status_code >= 400:
            response.raise_for_status()
        value = response.headers.get("content-length")
        return int(value) if value and value.isdigit() else None

    async def _check_month_availability(
        self,
        client: httpx.AsyncClient,
        months: tuple[tuple[int, int], ...],
    ) -> dict[tuple[int, int], int | None]:
        available_months = await self._list_available_months(client)
        if available_months:
            missing = [month for month in months if month not in available_months]
            if missing:
                raise ValueError(self._availability_error(missing, available_months))

        sizes: dict[tuple[int, int], int | None] = {}
        missing: list[tuple[int, int]] = []
        for year, month in months:
            url = self.build_month_url(year, month)
            try:
                sizes[(year, month)] = await self._remote_size(client, url)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    missing.append((year, month))
                    continue
                raise RuntimeError(
                    f"CHIRPS 可用性检查失败：远端返回 HTTP {exc.response.status_code}"
                ) from exc
        if missing:
            raise ValueError(self._availability_error(missing, available_months))
        return sizes

    async def _list_available_months(
        self,
        client: httpx.AsyncClient,
    ) -> tuple[tuple[int, int], ...]:
        try:
            response = await client.get(f"{self._base_url}/")
            response.raise_for_status()
        except httpx.HTTPError:
            return ()

        months = {
            (int(match.group(1)), int(match.group(2)))
            for match in MONTH_FILE_RE.finditer(response.text)
        }
        return tuple(sorted(months))

    @staticmethod
    def _month_label(month: tuple[int, int]) -> str:
        return f"{month[0]}-{month[1]:02d}"

    def _availability_error(
        self,
        missing: list[tuple[int, int]],
        available_months: tuple[tuple[int, int], ...],
    ) -> str:
        missing_text = "、".join(self._month_label(month) for month in missing)
        if available_months:
            start = self._month_label(available_months[0])
            end = self._month_label(available_months[-1])
            return (
                f"CHIRPS 远端尚未发布请求月份：{missing_text}。"
                f"当前可用月份范围为 {start} 至 {end}。"
            )
        return f"CHIRPS 远端尚未发布请求月份：{missing_text}。请改用已发布月份或稍后再试。"

    @staticmethod
    def _response_total(response: httpx.Response, offset: int) -> int | None:
        value = response.headers.get("content-length")
        if not value or not value.isdigit():
            return None
        return offset + int(value)
