"""NOAA Open Data Dissemination Himawari-8/9 AHI provider."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
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

PROVIDER_ID = "noaa-nodd"
L1B_DATASET_ID = "himawari-ahi-l1b-full-disk"
L2_CLOUDS_DATASET_ID = "himawari-ahi-l2-full-disk-clouds"
DEFAULT_BUCKET_URLS = {
    "noaa-himawari8": "https://noaa-himawari8.s3.amazonaws.com",
    "noaa-himawari9": "https://noaa-himawari9.s3.amazonaws.com",
}
CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class HimawariProduct:
    dataset_id: str
    prefix: str
    variables: tuple[str, ...]
    aliases: dict[str, str]


@dataclass(frozen=True)
class S3Object:
    bucket: str
    key: str
    size: int


L1B_BANDS = tuple(f"B{band:02d}" for band in range(1, 17))
L2_CLOUD_PRODUCTS = ("CHGT", "CMSK", "CPHS")

PRODUCTS = {
    L1B_DATASET_ID: HimawariProduct(
        dataset_id=L1B_DATASET_ID,
        prefix="AHI-L1b-FLDK",
        variables=L1B_BANDS,
        aliases={f"band{band}": f"B{band:02d}" for band in range(1, 17)},
    ),
    L2_CLOUDS_DATASET_ID: HimawariProduct(
        dataset_id=L2_CLOUDS_DATASET_ID,
        prefix="AHI-L2-FLDK-Clouds",
        variables=L2_CLOUD_PRODUCTS,
        aliases={
            "cloud_height": "CHGT",
            "cloud_mask": "CMSK",
            "cloud_phase": "CPHS",
            "云高": "CHGT",
            "云掩膜": "CMSK",
            "云相态": "CPHS",
        },
    ),
}


def _band_variable(name: str) -> DatasetVariable:
    band = int(name[1:])
    return DatasetVariable(
        name=name,
        long_name=f"AHI spectral band {band:02d}",
        units="radiance / brightness temperature / reflectance",
        aliases=(f"band{band}", f"波段{band}", "AHI"),
    )


HIMAWARI_SPECS = (
    DatasetSpec(
        dataset_id=L1B_DATASET_ID,
        name="Himawari-8/9 AHI Level 1b Full Disk",
        provider_id=PROVIDER_ID,
        provider_name="NOAA Open Data Dissemination / JMA",
        domain="satellite",
        description="葵花 8/9 号 AHI 全圆盘 Level 1b 十分钟观测，按波段和圆盘分段提供。",
        variables=tuple(_band_variable(name) for name in L1B_BANDS),
        spatial_coverage="Himawari Full Disk，中心经度约 140.7°E",
        temporal_coverage="2015-07 至接近实时",
        spatial_resolution="0.5 / 1 / 2 km，取决于波段",
        temporal_resolution="10 minutes",
        file_formats=("Himawari Standard Data (.DAT.bz2)",),
        download_granularity="one band segment per file",
        source_url="https://registry.opendata.aws/noaa-himawari/",
        citation_url="https://www.data.jma.go.jp/mscweb/en/himawari89/",
        supports_server_time_subset=True,
        supports_resume=True,
        notes=(
            "必须指定 B01 至 B16 中至少一个波段，避免误下载整日全波段数据。",
            "每个全圆盘波段时次由 10 个分段文件组成。",
            "时间与远端目录均使用 UTC。",
        ),
    ),
    DatasetSpec(
        dataset_id=L2_CLOUDS_DATASET_ID,
        name="Himawari-8/9 AHI Level 2 Full Disk Clouds",
        provider_id=PROVIDER_ID,
        provider_name="NOAA Open Data Dissemination / JMA",
        domain="satellite",
        description="葵花 8/9 号 AHI 全圆盘 Level 2 云产品。",
        variables=(
            DatasetVariable("CHGT", "cloud height", "meters", ("cloud_height", "云高")),
            DatasetVariable("CMSK", "cloud mask", "category", ("cloud_mask", "云掩膜")),
            DatasetVariable("CPHS", "cloud phase", "category", ("cloud_phase", "云相态")),
        ),
        spatial_coverage="Himawari Full Disk，中心经度约 140.7°E",
        temporal_coverage="2023 至接近实时",
        spatial_resolution="product dependent",
        temporal_resolution="10 minutes",
        file_formats=("NetCDF4",),
        download_granularity="one full-disk product per file",
        source_url="https://registry.opendata.aws/noaa-himawari/",
        citation_url="https://www.data.jma.go.jp/mscweb/en/himawari89/",
        supports_server_time_subset=True,
        supports_resume=True,
        notes=(
            "必须指定 CHGT、CMSK 或 CPHS 中至少一个产品。",
            "单个全圆盘 NetCDF 文件可能达到数百 MB。",
            "时间与远端目录均使用 UTC。",
        ),
    ),
)


def dates_between(start: date, end: date) -> tuple[date, ...]:
    if end < start:
        raise ValueError("end_date 不能早于 start_date")
    return tuple(start + timedelta(days=offset) for offset in range((end - start).days + 1))


def parse_s3_listing(xml: str, bucket: str) -> tuple[tuple[S3Object, ...], str | None]:
    root = ElementTree.fromstring(xml)
    objects: list[S3Object] = []
    token: str | None = None
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag == "Contents":
            values = {child.tag.rsplit("}", 1)[-1]: child.text or "" for child in element}
            if values.get("Key"):
                objects.append(S3Object(bucket, values["Key"], int(values.get("Size") or 0)))
        elif tag == "NextContinuationToken":
            token = element.text
    return tuple(objects), token


class HimawariProvider:
    provider_id = PROVIDER_ID

    def __init__(
        self,
        *,
        bucket_urls: dict[str, str] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._bucket_urls = {
            bucket: url.rstrip("/") for bucket, url in (bucket_urls or DEFAULT_BUCKET_URLS).items()
        }
        self._transport = transport

    def list_datasets(self) -> tuple[DatasetSpec, ...]:
        return HIMAWARI_SPECS

    async def download(
        self,
        request: DatasetDownloadRequest,
        on_progress: ProgressCallback | None = None,
    ) -> DatasetDownloadResult:
        product = PRODUCTS.get(request.dataset_id)
        if product is None:
            raise ValueError(f"{self.provider_id} 不支持数据集 {request.dataset_id}")
        variables = self._normalize_variables(product, request.variables)
        days = dates_between(
            self._parse_date(request.start_date, "start_date"),
            self._parse_date(request.end_date, "end_date"),
        )
        request.output_dir.mkdir(parents=True, exist_ok=True)

        async with httpx.AsyncClient(
            timeout=None,
            follow_redirects=True,
            transport=self._transport,
        ) as client:
            objects = await self._discover_objects(client, product, variables, days, on_progress)
            files: list[Path] = []
            urls: list[str] = []
            reused: list[Path] = []
            for index, item in enumerate(objects, start=1):
                destination = request.output_dir / item.bucket / item.key
                url = self._object_url(item)
                if on_progress:
                    on_progress(
                        f"葵花卫星正在下载第 {index}/{len(objects)} 个文件：{Path(item.key).name}"
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
            warnings.append("Himawari 公共对象存储不支持区域裁剪，返回文件尚未裁剪到请求区域。")
        return DatasetDownloadResult(
            dataset_id=request.dataset_id,
            provider_id=self.provider_id,
            files=tuple(files),
            source_urls=tuple(urls),
            reused_files=tuple(reused),
            warnings=tuple(warnings),
            metadata={
                "requested_start_date": request.start_date,
                "requested_end_date": request.end_date,
                "variables": list(variables),
                "object_count": len(objects),
                "total_bytes": sum(item.size for item in objects),
                "satellites": sorted({item.bucket.removeprefix("noaa-") for item in objects}),
                "time_zone": "UTC",
                "requires_local_subset": request.area is not None,
            },
        )

    async def _discover_objects(
        self,
        client: httpx.AsyncClient,
        product: HimawariProduct,
        variables: tuple[str, ...],
        days: tuple[date, ...],
        on_progress: ProgressCallback | None,
    ) -> tuple[S3Object, ...]:
        selected: list[S3Object] = []
        missing_days: list[str] = []
        for day in days:
            day_objects: list[S3Object] = []
            prefix = f"{product.prefix}/{day:%Y/%m/%d}/"
            for bucket in self._bucket_urls:
                day_objects.extend(await self._list_prefix(client, bucket, prefix))
            matches = [item for item in day_objects if self._matches(item.key, product, variables)]
            if not matches:
                missing_days.append(day.isoformat())
            selected.extend(matches)
            if on_progress:
                on_progress(f"葵花卫星 {day.isoformat()} 找到 {len(matches)} 个匹配文件")
        if missing_days:
            missing = "、".join(missing_days)
            raise ValueError(f"Himawari 远端未找到请求变量在这些 UTC 日期的数据：{missing}")
        return tuple(sorted(selected, key=lambda item: (item.key, item.bucket)))

    async def _list_prefix(
        self,
        client: httpx.AsyncClient,
        bucket: str,
        prefix: str,
    ) -> tuple[S3Object, ...]:
        objects: list[S3Object] = []
        token: str | None = None
        while True:
            params = {"list-type": "2", "prefix": prefix}
            if token:
                params["continuation-token"] = token
            response = await client.get(f"{self._bucket_urls[bucket]}/", params=params)
            response.raise_for_status()
            page, token = parse_s3_listing(response.text, bucket)
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

    def _object_url(self, item: S3Object) -> str:
        return f"{self._bucket_urls[item.bucket]}/{quote(item.key, safe='/')}"

    @staticmethod
    def _matches(key: str, product: HimawariProduct, variables: tuple[str, ...]) -> bool:
        filename = Path(key).name
        if product.dataset_id == L1B_DATASET_ID:
            return any(f"_{variable}_FLDK_" in filename for variable in variables)
        return any(filename.startswith(f"AHI-{variable}_") for variable in variables)

    @staticmethod
    def _normalize_variables(
        product: HimawariProduct,
        requested: tuple[str, ...],
    ) -> tuple[str, ...]:
        if not requested:
            choices = "、".join(product.variables)
            raise ValueError(f"Himawari 下载必须指定变量，可选值：{choices}")
        lookup = {name.casefold(): name for name in product.variables}
        lookup.update({alias.casefold(): value for alias, value in product.aliases.items()})
        variables: list[str] = []
        invalid: list[str] = []
        for value in requested:
            normalized = lookup.get(value.strip().casefold())
            if normalized is None:
                invalid.append(value)
            elif normalized not in variables:
                variables.append(normalized)
        if invalid:
            raise ValueError(f"Himawari 不支持变量：{'、'.join(invalid)}")
        return tuple(variables)

    @staticmethod
    def _parse_date(value: str, label: str) -> date:
        try:
            return date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{label} 必须使用 YYYY-MM-DD 格式") from exc
