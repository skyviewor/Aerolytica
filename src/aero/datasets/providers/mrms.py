"""NOAA MRMS AWS Open Data provider."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from urllib.parse import quote
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

DATASET_ID = "noaa-mrms"
PROVIDER_ID = "noaa-nodd"
BASE_URL = "https://noaa-mrms-pds.s3.amazonaws.com"
CHUNK_SIZE = 1024 * 1024
TIME_TOLERANCE_MINUTES = 5
OBJECT_TIME_RE = re.compile(r"_(?P<stamp>\d{8}-\d{6})\.grib2\.gz$")


@dataclass(frozen=True)
class MrmsProduct:
    product_id: str
    long_name: str
    units: str
    aliases: tuple[str, ...] = ()


@dataclass(frozen=True)
class MrmsObject:
    key: str
    size: int
    product: str
    region: str
    requested_time: str
    actual_time: str


MRMS_PRODUCTS = (
    MrmsProduct(
        "PrecipRate_00.00",
        "surface precipitation rate",
        "mm/h",
        ("precip_rate", "precipitation rate", "rain rate", "降水率", "降水强度"),
    ),
    MrmsProduct(
        "MergedReflectivityQCComposite_00.50",
        "quality-controlled composite reflectivity",
        "dBZ",
        ("reflectivity", "composite_reflectivity", "组合反射率", "雷达反射率"),
    ),
    MrmsProduct(
        "MergedReflectivityQC_00.50",
        "quality-controlled reflectivity mosaic",
        "dBZ",
        ("reflectivity_qc", "base_reflectivity", "反射率", "雷达拼图"),
    ),
    MrmsProduct(
        "MultiSensor_QPE_01H_Pass2_00.00",
        "one-hour multi-sensor quantitative precipitation estimate",
        "mm",
        ("qpe_1h", "1h precipitation", "hourly precipitation", "一小时降水"),
    ),
    MrmsProduct(
        "MultiSensor_QPE_24H_Pass2_00.00",
        "24-hour multi-sensor quantitative precipitation estimate",
        "mm",
        ("qpe_24h", "24h precipitation", "daily precipitation", "二十四小时降水", "日降水"),
    ),
    MrmsProduct(
        "MESH_00.50",
        "maximum estimated size of hail",
        "mm",
        ("mesh", "hail", "冰雹", "最大估计冰雹尺寸"),
    ),
)
MRMS_REGIONS = ("CONUS", "ALASKA", "CARIB", "GUAM", "HAWAII", "CONUS_5KM", "ANC")
PRODUCT_LOOKUP = {
    key.casefold(): product.product_id
    for product in MRMS_PRODUCTS
    for key in (product.product_id, product.long_name, *product.aliases)
}

MRMS_SPEC = DatasetSpec(
    dataset_id=DATASET_ID,
    name="NOAA MRMS Radar and Precipitation Products",
    provider_id=PROVIDER_ID,
    provider_name="NOAA Open Data Dissemination",
    domain="radar observations",
    description="Multi-Radar/Multi-Sensor System 雷达拼图、反射率、降水估计等近实时产品。",
    variables=tuple(
        DatasetVariable(
            product.product_id,
            product.long_name,
            product.units,
            product.aliases,
            description="MRMS 产品目录名，可通过 variables 或 product 参数选择。",
        )
        for product in MRMS_PRODUCTS
    ),
    spatial_coverage="CONUS、Alaska、Caribbean、Guam、Hawaii 等 MRMS 区域",
    temporal_coverage="近实时及历史归档，因产品和区域而异",
    spatial_resolution="约 1 km 或 5 km，因产品和区域而异",
    temporal_resolution="2 minutes to hourly，因产品而异",
    file_formats=("GRIB2.GZ",),
    download_granularity="one MRMS product object per region/date/time",
    source_url="https://registry.opendata.aws/noaa-mrms-pds/",
    citation_url="https://www.nssl.noaa.gov/projects/mrms/",
    supports_server_time_subset=True,
    supports_resume=True,
    notes=(
        "必须通过 product 或 variables 指定 MRMS 产品，例如 PrecipRate_00.00 或 reflectivity。",
        "必须指定 UTC 时次 times；MRMS 会在请求时次前后 5 分钟内选择最近对象。",
        "platforms 可限定区域，默认 CONUS；常用 CONUS、ALASKA、CARIB、GUAM、HAWAII。",
        "也可使用 CONUS_5KM、ANC 等 MRMS 区域目录。",
        "AWS 公共对象存储不支持经纬度区域裁剪。",
    ),
)


def parse_s3_listing(xml: str) -> tuple[tuple[tuple[str, int], ...], str | None]:
    root = ElementTree.fromstring(xml)
    objects: list[tuple[str, int]] = []
    token: str | None = None
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "Contents":
            values = {child.tag.rsplit("}", 1)[-1]: child.text or "" for child in element}
            if values.get("Key"):
                objects.append((values["Key"], int(values.get("Size") or 0)))
        elif tag == "NextContinuationToken":
            token = element.text
    return tuple(objects), token


class MrmsProvider:
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
        return (MRMS_SPEC,)

    async def download(
        self,
        request: DatasetDownloadRequest,
        on_progress: ProgressCallback | None = None,
    ) -> DatasetDownloadResult:
        if request.dataset_id != DATASET_ID:
            raise ValueError(f"{self.provider_id} 不支持数据集 {request.dataset_id}")
        products = self._normalize_products(request.product, request.variables)
        regions = self._normalize_regions(request.platforms)
        times = self._normalize_times(request.times)
        days = self._dates_between(
            self._parse_date(request.start_date, "start_date"),
            self._parse_date(request.end_date, "end_date"),
        )
        request.output_dir.mkdir(parents=True, exist_ok=True)

        async with httpx.AsyncClient(
            timeout=None,
            follow_redirects=True,
            transport=self._transport,
        ) as client:
            objects = await self._discover_objects(
                client, products, regions, days, times, on_progress
            )
            files: list[Path] = []
            urls: list[str] = []
            reused: list[Path] = []
            for index, item in enumerate(objects, start=1):
                destination = request.output_dir / "noaa-mrms-pds" / item.key
                url = self._object_url(item.key)
                if on_progress:
                    on_progress(
                        f"MRMS 正在下载第 {index}/{len(objects)} 个文件：{Path(item.key).name}"
                    )
                was_reused = await self._download_with_resume(
                    client, url, destination, item.size, on_progress
                )
                files.append(destination)
                urls.append(url)
                if was_reused:
                    reused.append(destination)

        warnings = []
        if request.area is not None:
            warnings.append("MRMS AWS 源不支持区域裁剪，返回文件仍覆盖对应区域完整网格。")
        return DatasetDownloadResult(
            dataset_id=DATASET_ID,
            provider_id=self.provider_id,
            files=tuple(files),
            source_urls=tuple(urls),
            reused_files=tuple(reused),
            warnings=tuple(warnings),
            metadata={
                "products": list(products),
                "regions": list(regions),
                "requested_times": list(times),
                "actual_times": [item.actual_time for item in objects],
                "object_count": len(objects),
                "total_bytes": sum(item.size for item in objects),
                "time_zone": "UTC",
                "requires_local_subset": request.area is not None,
            },
        )

    async def search_variables(self, dataset_id: str, query: str = "") -> tuple[str, ...]:
        if dataset_id != DATASET_ID:
            raise ValueError(f"{self.provider_id} 不支持数据集 {dataset_id}")
        terms = [term.casefold() for term in query.split() if term.strip()]
        values = []
        for product in MRMS_PRODUCTS:
            text = " ".join(
                (product.product_id, product.long_name, product.units, *product.aliases)
            ).casefold()
            if not terms or all(term in text for term in terms):
                values.append(f"{product.product_id}: {product.long_name} ({product.units})")
        return tuple(values)

    async def _discover_objects(
        self,
        client: httpx.AsyncClient,
        products: tuple[str, ...],
        regions: tuple[str, ...],
        days: tuple[date, ...],
        times: tuple[str, ...],
        on_progress: ProgressCallback | None,
    ) -> tuple[MrmsObject, ...]:
        selected: list[MrmsObject] = []
        missing: list[str] = []
        for day in days:
            for region in regions:
                for product in products:
                    prefix = f"{region}/{product}/{day:%Y%m%d}/"
                    candidates = await self._list_prefix(client, prefix)
                    for requested_time in times:
                        nearest = self._nearest_object(
                            candidates, product, region, requested_time, day
                        )
                        if nearest is None:
                            missing.append(f"{day.isoformat()} {region} {product} {requested_time}")
                        else:
                            selected.append(nearest)
                    if on_progress:
                        count = len(candidates)
                        on_progress(
                            f"MRMS {day.isoformat()} {region} {product} 找到 {count} 个对象"
                        )
        if missing:
            raise ValueError(
                "MRMS 远端未找到请求产品/区域/UTC 时次附近的数据："
                + "、".join(missing[:8])
                + (" 等" if len(missing) > 8 else "")
            )
        return tuple(sorted(dict.fromkeys(selected), key=lambda item: item.key))

    async def _list_prefix(
        self,
        client: httpx.AsyncClient,
        prefix: str,
    ) -> tuple[tuple[str, int], ...]:
        objects: list[tuple[str, int]] = []
        token: str | None = None
        while True:
            params = {"list-type": "2", "prefix": prefix}
            if token:
                params["continuation-token"] = token
            response = await client.get(f"{self._base_url}/", params=params)
            response.raise_for_status()
            page, token = parse_s3_listing(response.text)
            objects.extend(page)
            if not token:
                return tuple(objects)

    async def _download_with_resume(
        self,
        client: httpx.AsyncClient,
        url: str,
        destination: Path,
        remote_size: int,
        on_progress: ProgressCallback | None,
    ) -> bool:
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and destination.stat().st_size == remote_size:
            return True
        part = destination.with_suffix(destination.suffix + ".part")
        offset = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        async with client.stream("GET", url, headers=headers) as response:
            if response.status_code == 416 and offset == remote_size:
                part.replace(destination)
                return True
            response.raise_for_status()
            if offset and response.status_code != 206:
                offset = 0
                part.unlink(missing_ok=True)
            downloaded = offset
            with part.open("ab" if offset else "wb") as output:
                async for chunk in response.aiter_bytes(CHUNK_SIZE):
                    if cancel_requested():
                        raise RuntimeError("下载已取消")
                    output.write(chunk)
                    downloaded += len(chunk)
                    if on_progress and remote_size:
                        on_progress(downloaded, remote_size)
        part.replace(destination)
        return False

    def _object_url(self, key: str) -> str:
        return f"{self._base_url}/{quote(key, safe='/')}"

    @staticmethod
    def _nearest_object(
        candidates: tuple[tuple[str, int], ...],
        product: str,
        region: str,
        requested_time: str,
        day: date,
    ) -> MrmsObject | None:
        requested_seconds = int(requested_time[:2]) * 3600 + int(requested_time[2:]) * 60
        best: tuple[int, str, int, str] | None = None
        for key, size in candidates:
            actual = _object_datetime(key)
            if actual is None or actual.date() != day:
                continue
            actual_seconds = actual.hour * 3600 + actual.minute * 60 + actual.second
            delta = abs(actual_seconds - requested_seconds)
            if delta <= TIME_TOLERANCE_MINUTES * 60:
                value = (delta, key, size, actual.strftime("%H%M%S"))
                if best is None or value < best:
                    best = value
        if best is None:
            return None
        _, key, size, actual_time = best
        return MrmsObject(
            key=key,
            size=size,
            product=product,
            region=region,
            requested_time=requested_time,
            actual_time=actual_time,
        )

    @staticmethod
    def _normalize_products(product: str, variables: tuple[str, ...]) -> tuple[str, ...]:
        requested = [product, *variables] if product.strip() else list(variables)
        if not requested:
            raise ValueError(
                'MRMS 下载必须指定变量或产品，例如 variables=["reflectivity"] '
                '或 product="PrecipRate_00.00"'
            )
        products: list[str] = []
        invalid: list[str] = []
        for value in requested:
            key = value.strip()
            if not key:
                continue
            canonical = PRODUCT_LOOKUP.get(key.casefold())
            if canonical is None:
                invalid.append(value)
            elif canonical not in products:
                products.append(canonical)
        if invalid:
            choices = "、".join(product.product_id for product in MRMS_PRODUCTS)
            raise ValueError(f"MRMS 不支持变量或产品：{'、'.join(invalid)}。可选值：{choices}")
        return tuple(products)

    @staticmethod
    def _normalize_regions(requested: tuple[str, ...]) -> tuple[str, ...]:
        if not requested:
            return ("CONUS",)
        lookup = {region.casefold(): region for region in MRMS_REGIONS}
        lookup.update(
            {
                "continental us": "CONUS",
                "us": "CONUS",
                "美国本土": "CONUS",
                "alaska": "ALASKA",
                "阿拉斯加": "ALASKA",
                "caribbean": "CARIB",
                "hawaii": "HAWAII",
                "夏威夷": "HAWAII",
            }
        )
        regions: list[str] = []
        invalid: list[str] = []
        for value in requested:
            region = lookup.get(value.strip().casefold())
            if region is None:
                invalid.append(value)
            elif region not in regions:
                regions.append(region)
        if invalid:
            raise ValueError(
                f"MRMS 不支持区域：{'、'.join(invalid)}。可选值：{', '.join(MRMS_REGIONS)}"
            )
        return tuple(regions)

    @staticmethod
    def _normalize_times(requested: tuple[str, ...]) -> tuple[str, ...]:
        if not requested:
            raise ValueError('MRMS 下载必须指定 UTC 时次，例如 times=["00:04"]')
        times: list[str] = []
        invalid: list[str] = []
        for value in requested:
            compact = value.strip().lower().removesuffix("z").replace(":", "")
            if len(compact) == 2:
                compact += "00"
            if len(compact) == 6:
                compact = compact[:4]
            if len(compact) != 4 or not compact.isdigit():
                invalid.append(value)
                continue
            hour, minute = int(compact[:2]), int(compact[2:])
            if hour > 23 or minute > 59:
                invalid.append(value)
            elif compact not in times:
                times.append(compact)
        if invalid:
            raise ValueError(f"MRMS UTC 时次必须使用 HH:MM、HHMM 或 HH 格式：{'、'.join(invalid)}")
        return tuple(times)

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


def _object_datetime(key: str) -> datetime | None:
    match = OBJECT_TIME_RE.search(Path(key).name)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group("stamp"), "%Y%m%d-%H%M%S")
    except ValueError:
        return None
