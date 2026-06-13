"""NASA GES DISC MERRA-2 provider."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from aero.agent.progress import cancel_requested
from aero.core.config import AeroConfig
from aero.datasets.models import (
    DatasetDownloadRequest,
    DatasetDownloadResult,
    DatasetSpec,
    DatasetVariable,
)
from aero.datasets.provider import ProgressCallback

PROVIDER_ID = "nasa-gesdisc"
CMR_GRANULES_URL = "https://cmr.earthdata.nasa.gov/search/granules.json"
CHUNK_SIZE = 1024 * 1024
DOWNLOAD_ATTEMPTS = 4


@dataclass(frozen=True)
class Merra2Product:
    dataset_id: str
    short_name: str
    concept_id: str
    name: str
    description: str
    temporal_coverage: str
    temporal_resolution: str
    download_granularity: str
    common_variables: tuple[DatasetVariable, ...]
    pressure_levels: bool = False


SINGLE_LEVEL_VARIABLES = (
    DatasetVariable("T2M", "2-meter air temperature", "K", ("temperature", "气温", "2米气温")),
    DatasetVariable("U10M", "10-meter eastward wind", "m/s", ("u wind", "纬向风", "10米风")),
    DatasetVariable("V10M", "10-meter northward wind", "m/s", ("v wind", "经向风", "10米风")),
    DatasetVariable("PS", "surface pressure", "Pa", ("pressure", "地面气压")),
    DatasetVariable("QV2M", "2-meter specific humidity", "kg/kg", ("humidity", "比湿")),
    DatasetVariable("TQI", "total precipitable water vapor", "kg/m2", ("water vapor", "可降水量")),
)

PRESSURE_LEVEL_VARIABLES = (
    DatasetVariable("T", "air temperature", "K", ("temperature", "气温", "温度")),
    DatasetVariable("U", "eastward wind", "m/s", ("u wind", "纬向风")),
    DatasetVariable("V", "northward wind", "m/s", ("v wind", "经向风")),
    DatasetVariable("H", "geopotential height", "m", ("height", "位势高度")),
    DatasetVariable("QV", "specific humidity", "kg/kg", ("humidity", "比湿")),
    DatasetVariable(
        "OMEGA",
        "vertical pressure velocity",
        "Pa/s",
        ("vertical velocity", "垂直速度"),
    ),
)

PRODUCTS = {
    product.dataset_id: product
    for product in (
        Merra2Product(
            dataset_id="merra2-single-level-hourly",
            short_name="M2T1NXSLV",
            concept_id="C1276812863-GES_DISC",
            name="MERRA-2 2D Hourly Single-Level Diagnostics",
            description="NASA MERRA-2 全球单层逐小时同化诊断场（M2T1NXSLV）。",
            temporal_coverage="1980-01-01 至当前档案末期",
            temporal_resolution="hourly",
            download_granularity="daily NetCDF4 granule",
            common_variables=SINGLE_LEVEL_VARIABLES,
        ),
        Merra2Product(
            dataset_id="merra2-single-level-instant-hourly",
            short_name="M2I1NXASM",
            concept_id="C1276812820-GES_DISC",
            name="MERRA-2 2D Hourly Instantaneous Single-Level Assimilation",
            description="NASA MERRA-2 全球单层逐小时瞬时同化诊断场（M2I1NXASM）。",
            temporal_coverage="1980-01-01 至当前档案末期",
            temporal_resolution="hourly",
            download_granularity="daily NetCDF4 granule",
            common_variables=SINGLE_LEVEL_VARIABLES,
        ),
        Merra2Product(
            dataset_id="merra2-pressure-3hourly",
            short_name="M2I3NPASM",
            concept_id="C1276812879-GES_DISC",
            name="MERRA-2 3D 3-Hourly Pressure-Level Assimilation",
            description="NASA MERRA-2 全球气压层 3 小时瞬时同化场（M2I3NPASM）。",
            temporal_coverage="1980-01-01 至当前档案末期",
            temporal_resolution="3-hourly",
            download_granularity="daily NetCDF4 granule",
            common_variables=PRESSURE_LEVEL_VARIABLES,
            pressure_levels=True,
        ),
        Merra2Product(
            dataset_id="merra2-single-level-monthly",
            short_name="M2TMNXSLV",
            concept_id="C1276812859-GES_DISC",
            name="MERRA-2 2D Monthly Single-Level Diagnostics",
            description="NASA MERRA-2 全球单层月平均同化诊断场（M2TMNXSLV）。",
            temporal_coverage="1980-01 至当前档案末期",
            temporal_resolution="monthly",
            download_granularity="monthly NetCDF4 granule",
            common_variables=SINGLE_LEVEL_VARIABLES,
        ),
        Merra2Product(
            dataset_id="merra2-pressure-monthly",
            short_name="M2IMNPASM",
            concept_id="C1276812904-GES_DISC",
            name="MERRA-2 3D Monthly Pressure-Level Assimilation",
            description="NASA MERRA-2 全球气压层月平均同化场（M2IMNPASM）。",
            temporal_coverage="1980-01 至当前档案末期",
            temporal_resolution="monthly",
            download_granularity="monthly NetCDF4 granule",
            common_variables=PRESSURE_LEVEL_VARIABLES,
            pressure_levels=True,
        ),
    )
}


def _spec(product: Merra2Product) -> DatasetSpec:
    notes = [
        "通过 NASA CMR 发现 granule，并从 GES DISC HTTPS 链接下载 NetCDF4 文件。",
        "GES DISC 下载通常需要 Earthdata Login；可配置 EARTHDATA_TOKEN，或使用本机 .netrc。",
        "当前统一目录下载保留原始 granule；变量、区域、时次和层级裁剪"
        "请下载后再用 NetCDF 工具处理。",
    ]
    if product.pressure_levels:
        notes.append("气压层产品可在下载后按 lev 层级裁剪。")
    return DatasetSpec(
        dataset_id=product.dataset_id,
        name=product.name,
        provider_id=PROVIDER_ID,
        provider_name="NASA GES DISC",
        domain="reanalysis",
        description=product.description,
        variables=product.common_variables,
        spatial_coverage="全球",
        temporal_coverage=product.temporal_coverage,
        spatial_resolution="0.625 degree longitude x 0.5 degree latitude",
        temporal_resolution=product.temporal_resolution,
        file_formats=("NetCDF4",),
        download_granularity=product.download_granularity,
        source_url=f"https://disc.gsfc.nasa.gov/datasets/{product.short_name}_5.12.4/summary",
        citation_url="https://gmao.gsfc.nasa.gov/reanalysis/MERRA-2/",
        requires_auth=True,
        supports_server_time_subset=False,
        supports_server_area_subset=False,
        supports_resume=True,
        notes=tuple(notes),
    )


MERRA2_SPECS = tuple(_spec(product) for product in PRODUCTS.values())


def _parse_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} 必须使用 YYYY-MM-DD 格式") from exc


def _download_link(entry: dict[str, Any]) -> str:
    for link in entry.get("links", []):
        href = link.get("href", "")
        rel = link.get("rel", "")
        if href.startswith("https://") and rel.endswith("/data#") and href.endswith(".nc4"):
            return href
    for link in entry.get("links", []):
        href = link.get("href", "")
        title = str(link.get("title", "")).casefold()
        if href.startswith("https://") and href.endswith(".nc4") and "download" in title:
            return href
    return ""


def _destination(output_dir: Path, product: Merra2Product, entry: dict[str, Any], url: str) -> Path:
    filename = Path(httpx.URL(url).path).name
    if not filename:
        filename = f"{entry.get('producer_granule_id', entry.get('id', product.short_name))}.nc4"
    return output_dir / product.dataset_id / filename


class Merra2Provider:
    provider_id = PROVIDER_ID

    def __init__(
        self,
        *,
        cmr_url: str = CMR_GRANULES_URL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._cmr_url = cmr_url
        self._transport = transport

    def list_datasets(self) -> tuple[DatasetSpec, ...]:
        return MERRA2_SPECS

    async def search_variables(self, dataset_id: str, query: str = "") -> tuple[str, ...]:
        product = PRODUCTS.get(dataset_id)
        if product is None:
            raise ValueError(f"{self.provider_id} 不支持数据集 {dataset_id}")
        terms = [term.casefold() for term in query.split() if term.strip()]
        values = [
            f"{variable.name}: {variable.long_name} ({variable.units})"
            for variable in product.common_variables
        ]
        if terms:
            values = [
                value
                for value in values
                if all(term in value.casefold() for term in terms)
            ]
        return tuple(values)

    async def download(
        self,
        request: DatasetDownloadRequest,
        on_progress: ProgressCallback | None = None,
    ) -> DatasetDownloadResult:
        product = PRODUCTS.get(request.dataset_id)
        if product is None:
            raise ValueError(f"{self.provider_id} 不支持数据集 {request.dataset_id}")
        start = _parse_date(request.start_date, "start_date")
        end = _parse_date(request.end_date, "end_date")
        if end < start:
            raise ValueError("end_date 不能早于 start_date")
        if start < date(1980, 1, 1):
            raise ValueError("MERRA-2 可用时间始于 1980-01-01")
        if request.levels and not product.pressure_levels:
            raise ValueError("MERRA-2 单层产品不支持 levels，请改用气压层产品或去掉层级参数")

        request.output_dir.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(
            timeout=None,
            follow_redirects=True,
            transport=self._transport,
        ) as client:
            entries = await self._find_granules(client, product, start, end)
            files: list[Path] = []
            urls: list[str] = []
            reused: list[Path] = []
            token = _earthdata_token()
            for entry in entries:
                url = _download_link(entry)
                if not url:
                    raise RuntimeError(f"CMR granule 缺少可下载 HTTPS 链接: {entry.get('id')}")
                destination = _destination(request.output_dir, product, entry, url)
                if destination.exists():
                    files.append(destination)
                    urls.append(url)
                    reused.append(destination)
                    continue
                if on_progress:
                    on_progress(f"正在下载 MERRA-2 granule：{destination.name}")
                await self._download_stream(client, url, destination, on_progress, token=token)
                files.append(destination)
                urls.append(url)

        warnings: list[str] = []
        if request.variables or request.area or request.times or request.levels:
            warnings.append(
                "MERRA-2 统一下载当前保留原始 granule；variables/area/times/levels "
                "未做服务端裁剪，请下载后使用 NetCDF 子集工具处理。"
            )
        return DatasetDownloadResult(
            dataset_id=product.dataset_id,
            provider_id=self.provider_id,
            files=tuple(files),
            source_urls=tuple(urls),
            reused_files=tuple(reused),
            warnings=tuple(warnings),
            metadata={
                "short_name": product.short_name,
                "concept_id": product.concept_id,
                "granules": len(files),
                "requires_auth": True,
                "auth": "configure_earthdata_token or EARTHDATA_TOKEN",
                "server_subset": False,
                "variables": list(request.variables),
                "levels": list(request.levels),
                "time_zone": "UTC",
            },
        )

    async def _find_granules(
        self,
        client: httpx.AsyncClient,
        product: Merra2Product,
        start: date,
        end: date,
    ) -> tuple[dict[str, Any], ...]:
        params = {
            "collection_concept_id": product.concept_id,
            "temporal": f"{start.isoformat()}T00:00:00Z,{end.isoformat()}T23:59:59Z",
            "page_size": "2000",
            "sort_key": "start_date",
        }
        response = await client.get(self._cmr_url, params=params)
        response.raise_for_status()
        entries = response.json().get("feed", {}).get("entry", [])
        if not entries:
            raise ValueError(
                f"未找到 {product.name} 在 {start.isoformat()} 至 {end.isoformat()} 的 granule"
            )
        return tuple(entries)

    @staticmethod
    async def _download_stream(
        client: httpx.AsyncClient,
        url: str,
        destination: Path,
        on_progress: ProgressCallback | None,
        *,
        token: str = "",
    ) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        part = destination.with_suffix(destination.suffix + ".part")
        last_error: Exception | None = None
        for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
            offset = part.stat().st_size if part.exists() else 0
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            if token:
                headers["Authorization"] = f"Bearer {token}"
            try:
                async with client.stream("GET", url, headers=headers) as response:
                    if response.status_code in {401, 403}:
                        raise RuntimeError(
                            "GES DISC 下载需要 Earthdata Login 授权。请先配置 NASA "
                            "Earthdata token，或设置 EARTHDATA_TOKEN 环境变量后重试。"
                        )
                    response.raise_for_status()
                    if offset and response.status_code != 206:
                        offset = 0
                        part.unlink(missing_ok=True)
                    total_header = response.headers.get("content-length", "")
                    total = offset + int(total_header) if total_header.isdigit() else 0
                    with part.open("ab" if offset else "wb") as output:
                        downloaded = offset
                        async for chunk in response.aiter_bytes(CHUNK_SIZE):
                            if cancel_requested():
                                raise RuntimeError("下载已取消")
                            output.write(chunk)
                            downloaded += len(chunk)
                            if on_progress and total:
                                on_progress(downloaded, total)
                    if total and downloaded != total:
                        raise httpx.RemoteProtocolError(
                            f"下载连接提前结束：收到 {downloaded} 字节，预期 {total} 字节"
                        )
                part.replace(destination)
                return
            except RuntimeError:
                part.unlink(missing_ok=True)
                raise
            except (httpx.TransportError, OSError) as exc:
                last_error = exc
                if attempt == DOWNLOAD_ATTEMPTS:
                    break
                if on_progress:
                    on_progress(f"下载连接中断，正在自动重试（{attempt + 1}/{DOWNLOAD_ATTEMPTS}）")
                await asyncio.sleep(min(2 ** (attempt - 1), 4))
        assert last_error is not None
        raise last_error


def _earthdata_token() -> str:
    try:
        token = AeroConfig.create_default().credentials.earthdata.token
    except Exception:
        token = ""
    return token or os.environ.get("EARTHDATA_TOKEN", "").strip()
