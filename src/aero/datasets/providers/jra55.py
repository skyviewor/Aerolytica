"""NCAR GDEX JRA-55 THREDDS provider."""

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
PRESSURE_DATASET_ID = "jra55-pressure-6-hourly-125"
SURFACE_DATASET_ID = "jra55-surface-6-hourly-125"
BASE_URL = "https://tds.gdex.ucar.edu"
CATALOG_BASE = f"{BASE_URL}/thredds/catalog/files/g/d628000"
NCSS_BASE = f"{BASE_URL}/thredds/ncss/grid/"
CHUNK_SIZE = 1024 * 1024
AVAILABLE_START = date(1958, 1, 1)
AVAILABLE_END = date(2024, 1, 31)
ENTRY_RE = re.compile(
    r"(?P<collection>[a-z0-9_]+)\.(?P<code>\d{3})_(?P<variable>[a-z0-9]+)\."
    r"(?P<start>\d{10})_(?P<end>\d{10})$"
)


@dataclass(frozen=True)
class Jra55Product:
    dataset_id: str
    name: str
    collection: str
    description: str
    spatial_resolution: str
    pressure_levels: bool
    common_variables: tuple[DatasetVariable, ...]


@dataclass(frozen=True)
class Jra55Entry:
    collection: str
    variable: str
    code: str
    url_path: str
    start: datetime
    end: datetime


PRESSURE_VARIABLES = (
    DatasetVariable("hgt", "geopotential height", "gpm", ("height", "位势高度")),
    DatasetVariable("tmp", "temperature", "K", ("temperature", "气温", "温度")),
    DatasetVariable("ugrd", "u-component of wind", "m/s", ("u wind", "纬向风")),
    DatasetVariable("vgrd", "v-component of wind", "m/s", ("v wind", "经向风")),
    DatasetVariable("rh", "relative humidity", "%", ("relative humidity", "相对湿度")),
    DatasetVariable("depr", "dew point depression", "K", ("dewpoint depression", "露点差")),
)
SURFACE_VARIABLES = (
    DatasetVariable("pres", "surface pressure", "Pa", ("pressure", "地面气压")),
    DatasetVariable("prmsl", "mean sea-level pressure", "Pa", ("mslp", "海平面气压")),
    DatasetVariable("tmp", "surface temperature", "K", ("temperature", "地表温度")),
    DatasetVariable("ugrd", "u-component of wind", "m/s", ("u wind", "纬向风")),
    DatasetVariable("vgrd", "v-component of wind", "m/s", ("v wind", "经向风")),
)

PRODUCTS = {
    product.dataset_id: product
    for product in (
        Jra55Product(
            dataset_id=PRESSURE_DATASET_ID,
            name="JRA-55 6-Hourly Pressure-Level Analysis 1.25 Degree",
            collection="anl_p125",
            description="JMA JRA-55 全球 1.25° 气压层 6 小时分析场。",
            spatial_resolution="1.25 degree",
            pressure_levels=True,
            common_variables=PRESSURE_VARIABLES,
        ),
        Jra55Product(
            dataset_id=SURFACE_DATASET_ID,
            name="JRA-55 6-Hourly Surface Analysis 1.25 Degree",
            collection="anl_surf125",
            description="JMA JRA-55 全球 1.25° 地表 6 小时分析场。",
            spatial_resolution="1.25 degree",
            pressure_levels=False,
            common_variables=SURFACE_VARIABLES,
        ),
    )
}


def _spec(product: Jra55Product) -> DatasetSpec:
    notes = [
        "通过 NCAR GDEX THREDDS NetCDF Subset Service 下载 NetCDF 子集。",
        "变量可用短名，例如 tmp、hgt、ugrd、vgrd；也可先查询变量。",
        "时间按月文件组织，指定日期范围会下载涉及月份的子集。",
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
        variables=product.common_variables,
        spatial_coverage="全球",
        temporal_coverage="1958-01-01 至 2024-01-31 18:00 UTC",
        spatial_resolution=product.spatial_resolution,
        temporal_resolution="6-hourly",
        file_formats=("NetCDF3",),
        download_granularity="monthly variable subset",
        source_url=f"{CATALOG_BASE}/{product.collection}/catalog.html",
        citation_url="https://rda.ucar.edu/datasets/d628000/",
        supports_server_time_subset=True,
        supports_server_area_subset=True,
        supports_resume=False,
        notes=tuple(notes),
    )


JRA55_SPECS = tuple(_spec(product) for product in PRODUCTS.values())


def parse_catalog_xml(xml: str) -> tuple[Jra55Entry, ...]:
    root = ElementTree.fromstring(xml)
    entries: list[Jra55Entry] = []
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
            Jra55Entry(
                collection=match.group("collection"),
                variable=match.group("variable"),
                code=match.group("code"),
                url_path=url_path,
                start=_parse_catalog_datetime(match.group("start")),
                end=_parse_catalog_datetime(match.group("end")),
            )
        )
    return tuple(entries)


class Jra55Provider:
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
        self._entry_cache: dict[tuple[str, int], tuple[Jra55Entry, ...]] = {}
        self._grid_cache: dict[str, str] = {}

    def list_datasets(self) -> tuple[DatasetSpec, ...]:
        return JRA55_SPECS

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
            raise ValueError("JRA-55 可用时间始于 1958-01-01")
        if end > AVAILABLE_END:
            raise ValueError("JRA-55 近实时更新已于 2024-01-31 终止；如需更新数据，请改用 JRA-3Q。")
        variables = self._normalize_variables(product, request.variables)
        if not product.pressure_levels and request.levels:
            raise ValueError("JRA-55 地表产品不支持 levels，请去掉层级参数")
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
                grid_name = await self._grid_name(client, entry)
                for level in levels:
                    if level is not None and not product.pressure_levels:
                        continue
                    destination = self._destination(request.output_dir, product, entry, level)
                    url = self._ncss_url(entry, grid_name, request, level)
                    if on_progress:
                        on_progress(
                            f"正在获取 JRA-55 子集：{entry.variable} "
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
            entries = await self._year_entries(client, product, 2020)
        values = sorted({entry.variable for entry in entries})
        common = {
            variable.name: f"{variable.name}: {variable.long_name} ({variable.units})"
            for variable in product.common_variables
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
        product: Jra55Product,
        variables: tuple[str, ...],
        start: date,
        end: date,
    ) -> tuple[Jra55Entry, ...]:
        selected: list[Jra55Entry] = []
        years = range(start.year, end.year + 1)
        start_dt = datetime(start.year, start.month, start.day)
        end_dt = datetime(end.year, end.month, end.day, 23, 59, 59)
        for year in years:
            entries = await self._year_entries(client, product, year)
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
            for year in years:
                available_set.update(
                    entry.variable for entry in await self._year_entries(client, product, year)
                )
            available = sorted(available_set)
            suggestions = get_close_matches(" ".join(variables), available, n=6, cutoff=0.25)
            suggestion_text = f"；接近的可用变量: {', '.join(suggestions)}" if suggestions else ""
            raise ValueError(f"未找到变量 {', '.join(variables)}{suggestion_text}")
        return tuple(dict.fromkeys(selected))

    async def _year_entries(
        self,
        client: httpx.AsyncClient,
        product: Jra55Product,
        year: int,
    ) -> tuple[Jra55Entry, ...]:
        key = (product.collection, year)
        if key in self._entry_cache:
            return self._entry_cache[key]
        url = f"{self._catalog_base}/{product.collection}/{year}/catalog.xml"
        response = await client.get(url)
        response.raise_for_status()
        entries = parse_catalog_xml(response.text)
        if not entries:
            raise RuntimeError(f"NCAR GDEX 目录未返回 JRA-55 {product.collection} {year} 文件")
        self._entry_cache[key] = entries
        return entries

    async def _grid_name(self, client: httpx.AsyncClient, entry: Jra55Entry) -> str:
        if entry.url_path in self._grid_cache:
            return self._grid_cache[entry.url_path]
        response = await client.get(f"{self._ncss_base}{entry.url_path}/dataset.xml")
        response.raise_for_status()
        root = ElementTree.fromstring(response.text)
        for element in root.iter():
            if element.tag.rsplit("}", 1)[-1] == "grid":
                name = element.attrib.get("name")
                if name:
                    self._grid_cache[entry.url_path] = name
                    return name
        raise RuntimeError(f"JRA-55 NCSS 元数据未返回变量网格名：{entry.url_path}")

    def _ncss_url(
        self,
        entry: Jra55Entry,
        grid_name: str,
        request: DatasetDownloadRequest,
        level: float | None,
    ) -> str:
        params: list[tuple[str, str]] = [
            ("var", grid_name),
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
        product: Jra55Product,
        entry: Jra55Entry,
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
        product: Jra55Product,
        requested: tuple[str, ...],
    ) -> tuple[str, ...]:
        if not requested:
            examples = "、".join(variable.name for variable in product.common_variables[:4])
            raise ValueError(f"JRA-55 下载必须指定变量，例如 {examples}")
        lookup: dict[str, str] = {}
        for variable in product.common_variables:
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
            raise ValueError(f"JRA-55 不支持变量：{'、'.join(invalid)}")
        return tuple(variables)


def _parse_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} 必须使用 YYYY-MM-DD 格式") from exc


def _parse_catalog_datetime(value: str) -> datetime:
    return datetime.strptime(value, "%Y%m%d%H")
