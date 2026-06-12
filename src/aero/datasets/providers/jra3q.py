"""NCAR GDEX JRA-3Q THREDDS provider."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from difflib import get_close_matches
from pathlib import Path
from urllib.parse import urlencode
from xml.etree import ElementTree

import httpx

from aero.agent.progress import cancel_requested
from aero.datasets.models import (
    DatasetDownloadRequest,
    DatasetDownloadResult,
    DatasetSpec,
    DatasetVariable,
)
from aero.datasets.provider import ProgressCallback

PROVIDER_ID = "ncar-gdex"
PRESSURE_DATASET_ID = "jra3q-pressure-6-hourly-125"
SURFACE_DATASET_ID = "jra3q-surface-6-hourly-125"
BASE_URL = "https://tds.gdex.ucar.edu"
CATALOG_BASE = f"{BASE_URL}/thredds/catalog/files/g/d640000"
NCSS_BASE = f"{BASE_URL}/thredds/ncss/grid/"
CHUNK_SIZE = 1024 * 1024
AVAILABLE_START = date(1947, 9, 1)
SEARCH_SAMPLE_MONTH = "202501"
ENTRY_RE = re.compile(
    r"jra3q\.(?P<collection>[a-z0-9_]+)\.(?P<code>[0-9_]+)\."
    r"(?P<variable>[a-z0-9]+)-(?P<grid>[a-z0-9-]+)\."
    r"(?P<start>\d{10})_(?P<end>\d{10})\.nc$"
)


@dataclass(frozen=True)
class Jra3qProduct:
    dataset_id: str
    name: str
    collection: str
    description: str
    pressure_levels: bool
    variables: tuple[DatasetVariable, ...]


@dataclass(frozen=True)
class Jra3qEntry:
    collection: str
    variable: str
    code: str
    grid_name: str
    url_path: str
    start: datetime
    end: datetime


PRESSURE_VARIABLES = (
    DatasetVariable("hgt", "geopotential height", "gpm", ("height", "位势高度")),
    DatasetVariable("tmp", "temperature", "K", ("temperature", "气温", "温度")),
    DatasetVariable("ugrd", "u-component of wind", "m/s", ("u wind", "纬向风")),
    DatasetVariable("vgrd", "v-component of wind", "m/s", ("v wind", "经向风")),
    DatasetVariable("rh", "relative humidity", "%", ("relative humidity", "相对湿度")),
    DatasetVariable("spfh", "specific humidity", "kg/kg", ("specific humidity", "比湿")),
    DatasetVariable("vvel", "vertical velocity", "Pa/s", ("omega", "垂直速度")),
)
SURFACE_VARIABLES = (
    DatasetVariable("tmp2m", "2 m temperature", "K", ("temperature", "2m temperature", "2米气温")),
    DatasetVariable("rh2m", "2 m relative humidity", "%", ("relative humidity", "2米相对湿度")),
    DatasetVariable("ugrd10m", "10 m u-component of wind", "m/s", ("u wind", "10米纬向风")),
    DatasetVariable("vgrd10m", "10 m v-component of wind", "m/s", ("v wind", "10米经向风")),
    DatasetVariable("pres", "surface pressure", "Pa", ("pressure", "地面气压")),
    DatasetVariable("prmsl", "mean sea-level pressure", "Pa", ("mslp", "海平面气压")),
)

PRODUCTS = {
    product.dataset_id: product
    for product in (
        Jra3qProduct(
            dataset_id=PRESSURE_DATASET_ID,
            name="JRA-3Q 6-Hourly Pressure-Level Analysis 1.25 Degree",
            collection="anl_p125",
            description="JMA JRA-3Q 全球 1.25° 气压层 6 小时分析场。",
            pressure_levels=True,
            variables=PRESSURE_VARIABLES,
        ),
        Jra3qProduct(
            dataset_id=SURFACE_DATASET_ID,
            name="JRA-3Q 6-Hourly Surface Analysis 1.25 Degree",
            collection="anl_surf125",
            description="JMA JRA-3Q 全球 1.25° 地表 6 小时分析场。",
            pressure_levels=False,
            variables=SURFACE_VARIABLES,
        ),
    )
}


def _spec(product: Jra3qProduct) -> DatasetSpec:
    notes = [
        "通过 NCAR GDEX THREDDS NetCDF Subset Service 下载 NetCDF 子集。",
        "变量可用短名，例如 tmp、hgt、ugrd、vgrd 或 tmp2m、pres、prmsl。",
        "时间按月目录组织，指定日期范围会下载涉及月份的子集。",
        "GDEX 归档通常滞后实时数月；若请求月份尚未发布，会返回明确错误。",
    ]
    if product.pressure_levels:
        notes.append("气压层产品可用 levels 指定 hPa 层级，例如 [500, 850]。")
    else:
        notes.append("地表产品不需要指定 levels。")
    return DatasetSpec(
        dataset_id=product.dataset_id,
        name=product.name,
        provider_id=PROVIDER_ID,
        provider_name="NCAR Geoscience Data Exchange",
        domain="reanalysis",
        description=product.description,
        variables=product.variables,
        spatial_coverage="全球",
        temporal_coverage="1947-09-01 至当前 GDEX 归档末期（通常滞后数月）",
        spatial_resolution="1.25 degree",
        temporal_resolution="6-hourly",
        file_formats=("NetCDF3",),
        download_granularity="monthly variable subset",
        source_url=f"{CATALOG_BASE}/{product.collection}/catalog.html",
        citation_url="https://rda.ucar.edu/datasets/d640000/",
        supports_server_time_subset=True,
        supports_server_area_subset=True,
        supports_resume=False,
        notes=tuple(notes),
    )


JRA3Q_SPECS = tuple(_spec(product) for product in PRODUCTS.values())


def parse_catalog_xml(xml: str) -> tuple[Jra3qEntry, ...]:
    root = ElementTree.fromstring(xml)
    entries: list[Jra3qEntry] = []
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag != "dataset":
            continue
        url_path = element.attrib.get("urlPath", "")
        if not url_path:
            continue
        name = element.attrib.get("name") or Path(url_path).name
        match = ENTRY_RE.fullmatch(name)
        if match is None:
            continue
        entries.append(
            Jra3qEntry(
                collection=match.group("collection"),
                variable=match.group("variable"),
                code=match.group("code"),
                grid_name=f"{match.group('variable')}-{match.group('grid')}",
                url_path=url_path,
                start=_parse_catalog_datetime(match.group("start")),
                end=_parse_catalog_datetime(match.group("end")),
            )
        )
    return tuple(entries)


class Jra3qProvider:
    provider_id = PROVIDER_ID

    def __init__(
        self,
        *,
        catalog_base: str = CATALOG_BASE,
        ncss_base: str = NCSS_BASE,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._catalog_base = catalog_base.rstrip("/")
        self._ncss_base = ncss_base.rstrip("/") + "/"
        self._transport = transport
        self._entry_cache: dict[tuple[str, str], tuple[Jra3qEntry, ...]] = {}

    def list_datasets(self) -> tuple[DatasetSpec, ...]:
        return JRA3Q_SPECS

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
        if start < AVAILABLE_START:
            raise ValueError("JRA-3Q 可用时间始于 1947-09-01")
        variables = self._normalize_variables(product, request.variables)
        if not product.pressure_levels and request.levels:
            raise ValueError("JRA-3Q 地表产品不支持 levels，请去掉层级参数")
        levels: tuple[float | None, ...] = request.levels or (None,)
        request.output_dir.mkdir(parents=True, exist_ok=True)

        async with httpx.AsyncClient(
            timeout=None,
            follow_redirects=True,
            transport=self._transport,
        ) as client:
            entries = await self._select_entries(client, product, variables, start, end)
            files: list[Path] = []
            urls: list[str] = []
            for entry in entries:
                for level in levels:
                    if level is not None and not product.pressure_levels:
                        continue
                    destination = self._destination(request.output_dir, product, entry, level)
                    url = self._ncss_url(entry, request, level)
                    if on_progress:
                        on_progress(
                            f"正在获取 JRA-3Q 子集：{entry.variable} "
                            f"{entry.start:%Y-%m} "
                            f"{'' if level is None else f'{level:g} hPa'}"
                        )
                    await self._download_stream(client, url, destination, on_progress)
                    files.append(destination)
                    urls.append(url)

        return DatasetDownloadResult(
            dataset_id=product.dataset_id,
            provider_id=self.provider_id,
            files=tuple(files),
            source_urls=tuple(urls),
            metadata={
                "collection": product.collection,
                "variables": list(variables),
                "levels": list(request.levels),
                "server_subset": True,
                "time_zone": "UTC",
            },
        )

    async def search_variables(self, dataset_id: str, query: str = "") -> tuple[str, ...]:
        product = PRODUCTS.get(dataset_id)
        if product is None:
            raise ValueError(f"{self.provider_id} 不支持数据集 {dataset_id}")
        terms = [term.casefold() for term in query.split() if term.strip()]
        async with httpx.AsyncClient(
            timeout=None,
            follow_redirects=True,
            transport=self._transport,
        ) as client:
            entries = await self._month_entries(client, product, SEARCH_SAMPLE_MONTH)
        values = sorted({entry.variable for entry in entries})
        common = {
            variable.name: f"{variable.name}: {variable.long_name} ({variable.units})"
            for variable in product.variables
        }
        rendered = [common.get(value, value) for value in values]
        if terms:
            rendered = [
                value for value in rendered if all(term in value.casefold() for term in terms)
            ]
        return tuple(rendered)

    async def _select_entries(
        self,
        client: httpx.AsyncClient,
        product: Jra3qProduct,
        variables: tuple[str, ...],
        start: date,
        end: date,
    ) -> tuple[Jra3qEntry, ...]:
        selected: list[Jra3qEntry] = []
        months = _months_between(start, end)
        start_dt = datetime(start.year, start.month, start.day)
        end_dt = datetime(end.year, end.month, end.day, 23, 59, 59)
        for month in months:
            entries = await self._month_entries(client, product, month)
            for variable in variables:
                selected.extend(
                    entry
                    for entry in entries
                    if entry.variable == variable
                    and entry.end >= start_dt
                    and entry.start <= end_dt
                )
        if not selected:
            available_set: set[str] = set()
            for month in months:
                available_set.update(
                    entry.variable for entry in await self._month_entries(client, product, month)
                )
            available = sorted(available_set)
            suggestions = get_close_matches(" ".join(variables), available, n=6, cutoff=0.25)
            suggestion_text = f"；接近的可用变量: {', '.join(suggestions)}" if suggestions else ""
            raise ValueError(f"未找到变量 {', '.join(variables)}{suggestion_text}")
        return tuple(dict.fromkeys(selected))

    async def _month_entries(
        self,
        client: httpx.AsyncClient,
        product: Jra3qProduct,
        month: str,
    ) -> tuple[Jra3qEntry, ...]:
        key = (product.collection, month)
        if key in self._entry_cache:
            return self._entry_cache[key]
        url = f"{self._catalog_base}/{product.collection}/{month}/catalog.xml"
        response = await client.get(url)
        if response.status_code == 404:
            raise ValueError(f"JRA-3Q GDEX 尚未发布 {month} 的 {product.collection} 数据")
        response.raise_for_status()
        entries = parse_catalog_xml(response.text)
        if not entries:
            raise RuntimeError(f"NCAR GDEX 目录未返回 JRA-3Q {product.collection} {month} 文件")
        self._entry_cache[key] = entries
        return entries

    def _ncss_url(
        self,
        entry: Jra3qEntry,
        request: DatasetDownloadRequest,
        level: float | None,
    ) -> str:
        params: list[tuple[str, str]] = [
            ("var", entry.grid_name),
            ("time_start", f"{request.start_date}T00:00:00Z"),
            ("time_end", f"{request.end_date}T23:59:59Z"),
            ("accept", "netcdf3"),
        ]
        if request.area is None:
            params.append(("addLatLon", "true"))
        else:
            north, west, south, east = request.area
            params.extend(
                (
                    ("north", str(north)),
                    ("west", str(west)),
                    ("south", str(south)),
                    ("east", str(east)),
                )
            )
        if level is not None:
            params.append(("vertCoord", f"{level:g}"))
        return f"{self._ncss_base}{entry.url_path}?{urlencode(params)}"

    @staticmethod
    def _destination(
        output_dir: Path,
        product: Jra3qProduct,
        entry: Jra3qEntry,
        level: float | None,
    ) -> Path:
        parts = [
            product.dataset_id,
            entry.variable,
            entry.start.strftime("%Y%m%d%H"),
            entry.end.strftime("%Y%m%d%H"),
        ]
        if level is not None:
            parts.append(f"{level:g}hPa")
        return output_dir / f"{'_'.join(parts)}_subset.nc"

    @staticmethod
    async def _download_stream(
        client: httpx.AsyncClient,
        url: str,
        destination: Path,
        on_progress: ProgressCallback | None,
    ) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        part = destination.with_suffix(destination.suffix + ".part")
        part.unlink(missing_ok=True)
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length") or 0)
            downloaded = 0
            with part.open("wb") as output:
                async for chunk in response.aiter_bytes(CHUNK_SIZE):
                    if cancel_requested():
                        raise RuntimeError("下载已取消")
                    output.write(chunk)
                    downloaded += len(chunk)
                    if on_progress and total:
                        on_progress(downloaded, total)
        part.replace(destination)

    @staticmethod
    def _normalize_variables(
        product: Jra3qProduct,
        requested: tuple[str, ...],
    ) -> tuple[str, ...]:
        if not requested:
            examples = "、".join(variable.name for variable in product.variables[:4])
            raise ValueError(f"JRA-3Q 下载必须指定变量，例如 {examples}")
        lookup: dict[str, str] = {}
        for variable in product.variables:
            for alias in (variable.name, variable.long_name, *variable.aliases):
                lookup[alias.casefold()] = variable.name
        variables: list[str] = []
        invalid: list[str] = []
        for value in requested:
            key = value.strip().casefold()
            if not key:
                continue
            normalized = lookup.get(key, key)
            if not re.fullmatch(r"[a-z0-9]+", normalized):
                invalid.append(value)
            elif normalized not in variables:
                variables.append(normalized)
        if invalid:
            raise ValueError(f"JRA-3Q 不支持变量：{'、'.join(invalid)}")
        return tuple(variables)


def _parse_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} 必须使用 YYYY-MM-DD 格式") from exc


def _parse_catalog_datetime(value: str) -> datetime:
    return datetime.strptime(value, "%Y%m%d%H")


def _months_between(start: date, end: date) -> tuple[str, ...]:
    if end < start:
        raise ValueError("end_date 不能早于 start_date")
    months: list[str] = []
    year, month = start.year, start.month
    while (year, month) <= (end.year, end.month):
        months.append(f"{year}{month:02d}")
        month += 1
        if month == 13:
            year += 1
            month = 1
    return tuple(months)
