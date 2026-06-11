"""NOAA PSL NCEP/NCAR and NCEP-DOE reanalysis provider."""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from datetime import date
from difflib import get_close_matches
from pathlib import Path
from urllib.parse import urlencode, urljoin
from xml.etree import ElementTree

import httpx

from meteora.agent.progress import cancel_requested
from meteora.datasets.models import (
    DatasetDownloadRequest,
    DatasetDownloadResult,
    DatasetSpec,
    DatasetVariable,
)
from meteora.datasets.provider import ProgressCallback
from meteora.toolbox.tools.netcdf import _subset_netcdf_file

PROVIDER_ID = "noaa-psl"
CATALOG_BASE = "https://psl.noaa.gov/thredds/catalog/Datasets"
FILE_BASE = "https://psl.noaa.gov/thredds/fileServer/"
NCSS_BASE = "https://psl.noaa.gov/thredds/ncss/grid/"
CHUNK_SIZE = 1024 * 1024
DOWNLOAD_ATTEMPTS = 4
YEAR_RE = re.compile(r"\.(\d{4})\.nc$")


@dataclass(frozen=True)
class Product:
    dataset_id: str
    name: str
    root: str
    start_year: int
    temporal_resolution: str
    monthly: bool
    version: str


@dataclass(frozen=True)
class CatalogEntry:
    category: str
    variable: str
    url_path: str
    year: int | None


PRODUCTS = {
    product.dataset_id: product
    for product in (
        Product(
            "ncep-reanalysis-1-6-hourly",
            "NCEP/NCAR Reanalysis 1 6-hourly",
            "ncep.reanalysis",
            1948,
            "6-hourly",
            False,
            "NCEP/NCAR Reanalysis 1",
        ),
        Product(
            "ncep-reanalysis-1-monthly",
            "NCEP/NCAR Reanalysis 1 monthly means",
            "ncep.reanalysis.derived",
            1948,
            "monthly",
            True,
            "NCEP/NCAR Reanalysis 1",
        ),
        Product(
            "ncep-reanalysis-2-6-hourly",
            "NCEP-DOE Reanalysis 2 6-hourly",
            "ncep.reanalysis2",
            1979,
            "6-hourly",
            False,
            "NCEP-DOE Reanalysis 2",
        ),
        Product(
            "ncep-reanalysis-2-monthly",
            "NCEP-DOE Reanalysis 2 monthly means",
            "ncep.reanalysis2.derived",
            1979,
            "monthly",
            True,
            "NCEP-DOE Reanalysis 2",
        ),
    )
}

NCEP_VARIABLE = DatasetVariable(
    name="dynamic_catalog",
    long_name="NOAA PSL dynamically discovered reanalysis variables",
    units="various",
    aliases=(
        "NCEP",
        "NCEP/NCAR",
        "NCEP-DOE",
        "再分析",
        "气压层",
        "地表",
        "air",
        "uwnd",
        "vwnd",
        "hgt",
    ),
)


def _spec(product: Product) -> DatasetSpec:
    return DatasetSpec(
        dataset_id=product.dataset_id,
        name=product.name,
        provider_id=PROVIDER_ID,
        provider_name="NOAA Physical Sciences Laboratory",
        domain="reanalysis",
        description=f"{product.version} 全球{product.temporal_resolution}再分析数据。",
        variables=(NCEP_VARIABLE,),
        spatial_coverage="全球",
        temporal_coverage=f"{product.start_year}-01-01 至当前档案末期",
        spatial_resolution="2.5 degree，部分地表高斯网格产品除外",
        temporal_resolution=product.temporal_resolution,
        file_formats=("NetCDF",),
        download_granularity="server-side subset or source file",
        source_url=f"{CATALOG_BASE}/{product.root}/catalog.html",
        citation_url="https://psl.noaa.gov/data/gridded/reanalysis/",
        supports_server_time_subset=True,
        supports_server_area_subset=True,
        supports_resume=True,
        notes=(
            "变量与类别从 NOAA PSL THREDDS 目录动态发现。",
            "同名变量存在于多个类别时，使用 category/variable 形式消歧。",
        ),
    )


NCEP_REANALYSIS_SPECS = tuple(_spec(product) for product in PRODUCTS.values())


def _parse_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} 必须使用 YYYY-MM-DD 格式") from exc


def _years_between(start: date, end: date) -> tuple[int, ...]:
    return tuple(range(start.year, end.year + 1))


def _entry_variable(filename: str) -> str:
    name = filename.removesuffix(".nc")
    name = re.sub(r"\.\d{4}$", "", name)
    for suffix in (".mon.mean", ".mon.ltm", ".clim", ".mean"):
        name = name.removesuffix(suffix)
    return name


def parse_catalog_xml(xml: str, root: str) -> tuple[tuple[CatalogEntry, ...], tuple[str, ...]]:
    """Parse datasets and child catalogue references from a THREDDS catalogue."""
    tree = ElementTree.fromstring(xml)
    entries: list[CatalogEntry] = []
    refs: list[str] = []
    for element in tree.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "catalogRef":
            href = next(
                (value for key, value in element.attrib.items() if key.rsplit("}", 1)[-1] == "href"),
                "",
            )
            if href:
                refs.append(href)
        elif tag == "dataset":
            url_path = element.attrib.get("urlPath", "")
            if not url_path or not url_path.endswith(".nc"):
                continue
            relative = url_path.split(f"{root}/", 1)[-1]
            parts = relative.split("/")
            category = "/".join(parts[:-1]) or "root"
            filename = parts[-1]
            match = YEAR_RE.search(filename)
            entries.append(
                CatalogEntry(
                    category=category,
                    variable=_entry_variable(filename),
                    url_path=url_path,
                    year=int(match.group(1)) if match else None,
                )
            )
    return tuple(entries), tuple(refs)


class NcepReanalysisProvider:
    provider_id = PROVIDER_ID

    def __init__(
        self,
        *,
        catalog_base: str = CATALOG_BASE,
        file_base: str = FILE_BASE,
        ncss_base: str = NCSS_BASE,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._catalog_base = catalog_base.rstrip("/")
        self._file_base = file_base.rstrip("/") + "/"
        self._ncss_base = ncss_base.rstrip("/") + "/"
        self._transport = transport
        self._catalog_cache: dict[str, tuple[CatalogEntry, ...]] = {}

    def list_datasets(self) -> tuple[DatasetSpec, ...]:
        return NCEP_REANALYSIS_SPECS

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
        if start.year < product.start_year:
            raise ValueError(f"{product.name} 可用时间始于 {product.start_year}-01-01")
        if not request.variables:
            raise ValueError("NCEP Reanalysis 下载必须指定变量，例如 pressure/air 或 surface/air")

        request.output_dir.mkdir(parents=True, exist_ok=True)
        async with httpx.AsyncClient(
            timeout=None,
            follow_redirects=True,
            transport=self._transport,
        ) as client:
            entries = await self._catalog_entries(client, product)
            selected = self._select_entries(entries, request.variables, product, start, end)
            return await self._download_selected(client, product, selected, request, on_progress)

    async def search_variables(self, dataset_id: str, query: str = "") -> tuple[str, ...]:
        product = PRODUCTS.get(dataset_id)
        if product is None:
            raise ValueError(f"{self.provider_id} 不支持数据集 {dataset_id}")
        async with httpx.AsyncClient(
            timeout=None,
            follow_redirects=True,
            transport=self._transport,
        ) as client:
            entries = await self._catalog_entries(client, product)
        eligible = self._eligible_entries(entries, product, years=None)
        values = sorted({f"{entry.category}/{entry.variable}" for entry in eligible})
        terms = [term.casefold() for term in query.split() if term.strip()]
        if terms:
            values = [value for value in values if all(term in value.casefold() for term in terms)]
        return tuple(values)

    async def _catalog_entries(
        self,
        client: httpx.AsyncClient,
        product: Product,
    ) -> tuple[CatalogEntry, ...]:
        if product.dataset_id in self._catalog_cache:
            return self._catalog_cache[product.dataset_id]
        root_url = f"{self._catalog_base}/{product.root}/catalog.xml"
        pending = [(root_url, 0)]
        visited: set[str] = set()
        entries: list[CatalogEntry] = []
        while pending:
            url, depth = pending.pop(0)
            if url in visited or depth > 4:
                continue
            visited.add(url)
            response = await client.get(url)
            response.raise_for_status()
            found, refs = parse_catalog_xml(response.text, product.root)
            entries.extend(found)
            pending.extend((urljoin(url, ref), depth + 1) for ref in refs)
        if not entries:
            raise RuntimeError(f"NOAA PSL 目录未返回 {product.name} 的可下载文件")
        result = tuple(entries)
        self._catalog_cache[product.dataset_id] = result
        return result

    @staticmethod
    def _select_entries(
        entries: tuple[CatalogEntry, ...],
        variables: tuple[str, ...],
        product: Product,
        start: date,
        end: date,
    ) -> tuple[CatalogEntry, ...]:
        selected: list[CatalogEntry] = []
        years = set(_years_between(start, end))
        for requested in variables:
            requested = requested.strip().casefold()
            eligible = NcepReanalysisProvider._eligible_entries(entries, product, years)
            category, variable = requested.rsplit("/", 1) if "/" in requested else ("", requested)
            exact = [entry for entry in eligible if entry.variable.casefold() == variable]
            candidates = exact or [
                entry
                for entry in eligible
                if entry.variable.casefold().startswith(f"{variable}.")
            ]
            if category:
                exact_category_matches = [
                    entry
                    for entry in candidates
                    if entry.category.casefold() == category
                ]
                suffix_category_matches = [
                    entry
                    for entry in candidates
                    if entry.category.casefold().endswith(f"/{category}")
                ]
                if exact_category_matches:
                    candidates = exact_category_matches
                elif suffix_category_matches:
                    candidates = suffix_category_matches
            identities = sorted({f"{entry.category}/{entry.variable}" for entry in candidates})
            if not candidates:
                available = sorted({f"{entry.category}/{entry.variable}" for entry in eligible})
                suggestions = get_close_matches(requested, available, n=6, cutoff=0.25)
                if not suggestions:
                    suggestions = get_close_matches(variable, available, n=6, cutoff=0.25)
                suggestion_text = f"；接近的可用变量: {', '.join(suggestions)}" if suggestions else ""
                raise ValueError(
                    f"未找到变量 {requested}{suggestion_text}。"
                    "请先查询数据集变量，或使用 category/variable 形式。"
                )
            if len(identities) > 1:
                raise ValueError(
                    f"变量 {requested} 存在歧义，请指定其中一个: {', '.join(identities[:8])}"
                )
            selected.extend(candidates)
        return tuple(dict.fromkeys(selected))

    @staticmethod
    def _eligible_entries(
        entries: tuple[CatalogEntry, ...],
        product: Product,
        years: set[int] | None,
    ) -> tuple[CatalogEntry, ...]:
        if product.monthly:
            return tuple(entry for entry in entries if entry.url_path.endswith(".mon.mean.nc"))
        return tuple(
            entry
            for entry in entries
            if entry.category.split("/", 1)[0].casefold() != "dailies"
            and (years is None or entry.year in years)
        )

    async def _download_selected(
        self,
        client: httpx.AsyncClient,
        product: Product,
        entries: tuple[CatalogEntry, ...],
        request: DatasetDownloadRequest,
        on_progress: ProgressCallback | None,
    ) -> DatasetDownloadResult:
        files: list[Path] = []
        urls: list[str] = []
        reused: list[Path] = []
        fallbacks: list[dict[str, str]] = []
        areas = self._split_area(request.area)
        levels: tuple[float | None, ...] = request.levels or (None,)
        for entry in entries:
            for area_index, area in enumerate(areas, start=1):
                for level in levels:
                    suffixes = [entry.category.replace("/", "-"), entry.variable]
                    if entry.year:
                        suffixes.append(str(entry.year))
                    if level is not None:
                        suffixes.append(f"{level:g}hPa")
                    if len(areas) > 1:
                        suffixes.append(f"area{area_index}")
                    destination = request.output_dir / (
                        f"{product.dataset_id}_{'_'.join(suffixes)}_subset.nc"
                    )
                    ncss_url = self._ncss_url(entry, request, area, level)
                    if destination.exists():
                        files.append(destination)
                        urls.append(ncss_url)
                        reused.append(destination)
                        continue
                    if on_progress:
                        on_progress(f"正在获取 NCEP Reanalysis 子集：{entry.category}/{entry.variable}")
                    try:
                        await self._download_stream(client, ncss_url, destination, on_progress)
                    except (httpx.HTTPError, OSError, RuntimeError) as exc:
                        destination.with_suffix(destination.suffix + ".part").unlink(missing_ok=True)
                        fallback_url = urljoin(self._file_base, entry.url_path)
                        cache = (
                            request.output_dir
                            / ".meteora-cache"
                            / "ncep"
                            / product.root
                            / entry.category
                            / Path(entry.url_path).name
                        )
                        await self._download_stream(client, fallback_url, cache, on_progress, resume=True)
                        _subset_netcdf_file(
                            input_path=cache,
                            output_path=destination,
                            start_time=request.start_date,
                            end_time=request.end_date,
                            area=list(area) if area else None,
                            variables=None,
                            levels=[level] if level is not None else list(request.levels) or None,
                            overwrite=True,
                        )
                        fallbacks.append({"source": fallback_url, "reason": str(exc)})
                        urls.append(fallback_url)
                    else:
                        urls.append(ncss_url)
                    files.append(destination)
        return DatasetDownloadResult(
            dataset_id=product.dataset_id,
            provider_id=self.provider_id,
            files=tuple(files),
            source_urls=tuple(urls),
            reused_files=tuple(reused),
            metadata={
                "version": product.version,
                "categories": sorted({entry.category for entry in entries}),
                "variables": list(request.variables),
                "levels": list(request.levels),
                "server_subset": not fallbacks,
                "fallbacks": fallbacks,
            },
        )

    def _ncss_url(
        self,
        entry: CatalogEntry,
        request: DatasetDownloadRequest,
        area: tuple[float, float, float, float] | None,
        level: float | None,
    ) -> str:
        params: list[tuple[str, str]] = [
            ("var", entry.variable.split(".", 1)[0]),
            ("time_start", f"{request.start_date}T00:00:00Z"),
            ("time_end", f"{request.end_date}T23:59:59Z"),
            ("accept", "netcdf4"),
        ]
        if area:
            north, west, south, east = area
            params.extend(
                [
                    ("north", str(north)),
                    ("west", str(west)),
                    ("south", str(south)),
                    ("east", str(east)),
                ]
            )
        else:
            params.append(("addLatLon", "true"))
        if level is not None:
            params.append(("vertCoord", f"{level:g}"))
        return f"{urljoin(self._ncss_base, entry.url_path)}?{urlencode(params)}"

    @staticmethod
    def _split_area(
        area: tuple[float, float, float, float] | None,
    ) -> tuple[tuple[float, float, float, float] | None, ...]:
        if area is None:
            return (None,)
        north, west, south, east = area
        if west <= east:
            return (area,)
        return ((north, west, south, 180.0), (north, -180.0, south, east))

    @staticmethod
    async def _download_stream(
        client: httpx.AsyncClient,
        url: str,
        destination: Path,
        on_progress: ProgressCallback | None,
        *,
        resume: bool = False,
    ) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if resume and destination.exists():
            return
        part = destination.with_suffix(destination.suffix + ".part")
        last_error: httpx.TransportError | None = None
        for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
            if not resume and part.exists():
                part.unlink()
            offset = part.stat().st_size if resume and part.exists() else 0
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            try:
                async with client.stream("GET", url, headers=headers) as response:
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
            except httpx.TransportError as exc:
                last_error = exc
                if attempt == DOWNLOAD_ATTEMPTS:
                    break
                if on_progress:
                    mode = "从断点继续" if resume and part.exists() else "重新下载"
                    on_progress(
                        f"下载连接中断，正在自动重试（{attempt + 1}/{DOWNLOAD_ATTEMPTS}，{mode}）"
                    )
                await asyncio.sleep(min(2 ** (attempt - 1), 4))
        assert last_error is not None
        raise last_error
