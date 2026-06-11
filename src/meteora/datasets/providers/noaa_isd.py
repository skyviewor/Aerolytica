"""NOAA NCEI Integrated Surface Database Global Hourly provider."""

from __future__ import annotations

import asyncio
import csv
import io
import json
import re
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode

import httpx

from meteora.agent.progress import cancel_requested
from meteora.data.isd_parser import parse_isd_csv
from meteora.datasets.models import (
    DatasetDownloadRequest,
    DatasetDownloadResult,
    DatasetSpec,
    DatasetStation,
    DatasetVariable,
)
from meteora.datasets.provider import ProgressCallback

DATASET_ID = "noaa-isd-global-hourly"
PROVIDER_ID = "noaa-ncei"
DATA_API = "https://www.ncei.noaa.gov/access/services/data/v1"
SEARCH_API = "https://www.ncei.noaa.gov/access/services/search/v1/data"
ARCHIVE_BASE = "https://www.ncei.noaa.gov/data/global-hourly/access"
HISTORY_URL = "https://www.ncei.noaa.gov/pub/data/noaa/isd-history.csv"
INVENTORY_URL = "https://www.ncei.noaa.gov/pub/data/noaa/isd-inventory.csv"
CHUNK_SIZE = 1024 * 1024
DOWNLOAD_ATTEMPTS = 4
MAX_AUTO_STATIONS = 50
STATION_ID_RE = re.compile(r"^[0-9A-Z]{6}[0-9A-Z]{5}$")
FIELD_RE = re.compile(r"^[A-Z][A-Z0-9]{1,15}$")
BASE_FIELDS = ("STATION", "DATE", "SOURCE", "REPORT_TYPE", "CALL_SIGN", "QUALITY_CONTROL")

VARIABLE_ALIASES = {
    "temperature": "TMP",
    "temperature_air": "TMP",
    "气温": "TMP",
    "dew_point": "DEW",
    "露点": "DEW",
    "wind": "WND",
    "风": "WND",
    "sea_level_pressure": "SLP",
    "海平面气压": "SLP",
    "visibility": "VIS",
    "能见度": "VIS",
    "ceiling": "CIG",
    "云底高度": "CIG",
}
COMMON_FIELDS = {
    "TMP": "air temperature",
    "DEW": "dew point temperature",
    "WND": "wind observation",
    "SLP": "sea level pressure",
    "VIS": "visibility",
    "CIG": "ceiling height",
    "AA1": "liquid precipitation period 1",
    "AA2": "liquid precipitation period 2",
    "AA3": "liquid precipitation period 3",
    "GA1": "sky cover layer 1",
    "GA2": "sky cover layer 2",
    "GA3": "sky cover layer 3",
    "GF1": "sky condition observation",
    "MA1": "atmospheric pressure observation",
    "REM": "remarks",
}
ADDITIONAL_FIELDS = (
    "AB1",
    "AD1",
    "AE1",
    "AH1",
    "AH2",
    "AH3",
    "AH4",
    "AH5",
    "AH6",
    "AI1",
    "AI2",
    "AI3",
    "AI4",
    "AI5",
    "AI6",
    "AJ1",
    "AK1",
    "AL1",
    "AM1",
    "AN1",
    "AT1",
    "AT2",
    "AT3",
    "AT4",
    "AT5",
    "AT6",
    "AT7",
    "AU1",
    "AU2",
    "AU3",
    "AU4",
    "AW1",
    "AW2",
    "AW3",
    "AW4",
    "AW5",
    "AX1",
    "AX2",
    "AX3",
    "AX4",
    "ED1",
    "GD1",
    "GD2",
    "GD3",
    "GD4",
    "GE1",
    "KA1",
    "KA2",
    "KB1",
    "KB2",
    "KB3",
    "KC1",
    "KC2",
    "KD1",
    "KD2",
    "KE1",
    "MD1",
    "MG1",
    "MH1",
    "MK1",
    "MW1",
    "MW2",
    "MW3",
    "OC1",
    "OD1",
    "OE1",
    "OE2",
    "OE3",
    "RH1",
    "RH2",
    "RH3",
    "EQD",
)
KNOWN_FIELDS = frozenset((*BASE_FIELDS, *COMMON_FIELDS, *ADDITIONAL_FIELDS))

ISD_SPEC = DatasetSpec(
    dataset_id=DATASET_ID,
    name="NOAA Integrated Surface Database Global Hourly",
    provider_id=PROVIDER_ID,
    provider_name="NOAA National Centers for Environmental Information",
    domain="observations",
    description="全球地面站逐小时与逐次天气观测，保留 NOAA ISD 原始字段和质量标记。",
    variables=tuple(
        DatasetVariable(
            name=code,
            long_name=description,
            units="encoded in ISD field",
            aliases=tuple(alias for alias, target in VARIABLE_ALIASES.items() if target == code),
        )
        for code, description in COMMON_FIELDS.items()
    ),
    spatial_coverage="全球地面观测站",
    temporal_coverage="1901 至接近实时，具体范围按站点确认",
    spatial_resolution="station observations",
    temporal_resolution="hourly and sub-hourly reports",
    file_formats=("CSV",),
    download_granularity="station and requested date range",
    source_url=f"{ARCHIVE_BASE}/",
    citation_url="https://www.ncei.noaa.gov/products/land-based-station/integrated-surface-database",
    supports_server_time_subset=True,
    supports_server_area_subset=False,
    supports_resume=True,
    notes=(
        "下载前需指定站点或区域；区域自动选择最多 50 个站点。",
        "字段保留 NOAA ISD 原始编码、单位缩放和质量标记。",
        "下载后自动保留原始 CSV，并生成常规气象要素可读版 CSV。",
    ),
)


def _parse_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} 必须使用 YYYY-MM-DD 格式") from exc


def _catalog_date(value: str) -> date | None:
    try:
        return date(int(value[:4]), int(value[4:6]), int(value[6:8]))
    except (TypeError, ValueError):
        return None


def _float(value: str) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_station_history(text: str) -> tuple[DatasetStation, ...]:
    stations: list[DatasetStation] = []
    for row in csv.DictReader(io.StringIO(text)):
        station_id = f"{row.get('USAF', '').strip()}{row.get('WBAN', '').strip()}".upper()
        if not STATION_ID_RE.fullmatch(station_id):
            continue
        begin = _catalog_date(row.get("BEGIN", ""))
        end = _catalog_date(row.get("END", ""))
        stations.append(
            DatasetStation(
                station_id=station_id,
                name=row.get("STATION NAME", "").strip(),
                country=row.get("CTRY", "").strip(),
                state=row.get("STATE", "").strip(),
                icao=row.get("ICAO", "").strip(),
                latitude=_float(row.get("LAT", "")),
                longitude=_float(row.get("LON", "")),
                elevation_m=_float(row.get("ELEV(M)", "")),
                begin_date=begin.isoformat() if begin else "",
                end_date=end.isoformat() if end else "",
            )
        )
    return tuple(stations)


def parse_station_inventory(text: str) -> dict[str, dict[tuple[int, int], int]]:
    inventory: dict[str, dict[tuple[int, int], int]] = defaultdict(dict)
    months = ("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC")
    for row in csv.DictReader(io.StringIO(text)):
        station_id = f"{row.get('USAF', '').strip()}{row.get('WBAN', '').strip()}".upper()
        try:
            year = int(row.get("YEAR", ""))
        except ValueError:
            continue
        for month, label in enumerate(months, start=1):
            try:
                inventory[station_id][(year, month)] = int(row.get(label, "0") or 0)
            except ValueError:
                inventory[station_id][(year, month)] = 0
    return dict(inventory)


class NoaaIsdProvider:
    provider_id = PROVIDER_ID

    def __init__(
        self,
        *,
        data_api: str = DATA_API,
        search_api: str = SEARCH_API,
        archive_base: str = ARCHIVE_BASE,
        history_url: str = HISTORY_URL,
        inventory_url: str = INVENTORY_URL,
        cache_dir: Path | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._data_api = data_api
        self._search_api = search_api
        self._archive_base = archive_base.rstrip("/")
        self._history_url = history_url
        self._inventory_url = inventory_url
        self._cache_dir = cache_dir or Path.home() / ".cache" / "meteora" / "isd"
        self._transport = transport
        self._stations: tuple[DatasetStation, ...] | None = None
        self._inventory: dict[str, dict[tuple[int, int], int]] | None = None

    def list_datasets(self) -> tuple[DatasetSpec, ...]:
        return (ISD_SPEC,)

    async def search_variables(self, dataset_id: str, query: str = "") -> tuple[str, ...]:
        if dataset_id != DATASET_ID:
            raise ValueError(f"{self.provider_id} 不支持数据集 {dataset_id}")
        term = query.casefold().strip()
        values = [
            f"{code}: {COMMON_FIELDS.get(code, 'ISD additional data group')}"
            for code in sorted(KNOWN_FIELDS - set(BASE_FIELDS))
        ]
        if not term:
            return tuple(values)
        matches = []
        for value in values:
            code = value.split(":", 1)[0]
            aliases = [alias for alias, target in VARIABLE_ALIASES.items() if target == code]
            if term in " ".join((value, *aliases)).casefold():
                matches.append(value)
        return tuple(matches)

    async def search_stations(
        self,
        dataset_id: str,
        query: str = "",
        area: tuple[float, float, float, float] | None = None,
        start_date: str = "",
        end_date: str = "",
    ) -> tuple[DatasetStation, ...]:
        if dataset_id != DATASET_ID:
            raise ValueError(f"{self.provider_id} 不支持数据集 {dataset_id}")
        start = _parse_date(start_date, "start_date") if start_date else None
        end = _parse_date(end_date, "end_date") if end_date else start
        if start and end and end < start:
            raise ValueError("end_date 不能早于 start_date")
        async with self._client() as client:
            stations, inventory = await self._load_catalogs(client)
        term = query.casefold().strip()
        results: list[DatasetStation] = []
        for station in stations:
            if term and term not in " ".join(
                (
                    station.station_id,
                    station.name,
                    station.country,
                    station.state,
                    station.icao,
                )
            ).casefold():
                continue
            if area and not self._in_area(station, area):
                continue
            if start and end and not self._history_covers(station, start, end):
                continue
            observations = self._observation_count(inventory.get(station.station_id), start, end)
            if observations == 0:
                continue
            results.append(
                DatasetStation(
                    **{
                        **station.to_dict(),
                        "monthly_observations": observations,
                    }
                )
            )
        return tuple(sorted(results, key=lambda item: (item.station_id, item.name)))

    async def download(
        self,
        request: DatasetDownloadRequest,
        on_progress: ProgressCallback | None = None,
    ) -> DatasetDownloadResult:
        if request.dataset_id != DATASET_ID:
            raise ValueError(f"{self.provider_id} 不支持数据集 {request.dataset_id}")
        start = _parse_date(request.start_date, "start_date")
        end = _parse_date(request.end_date, "end_date")
        if end < start:
            raise ValueError("end_date 不能早于 start_date")
        if not request.stations and request.area is None:
            raise ValueError("NOAA ISD 下载必须指定 stations 或 area")
        variables = self._normalize_variables(request.variables)
        request.output_dir.mkdir(parents=True, exist_ok=True)

        files: list[Path] = []
        raw_files: list[Path] = []
        urls: list[str] = []
        reused: list[Path] = []
        failures: list[dict[str, str]] = []
        fallbacks: list[dict[str, str]] = []
        async with self._client() as client:
            stations, inventory = await self._load_catalogs(client)
            selected = self._select_stations(stations, inventory, request, start, end)
            for index, station in enumerate(selected, start=1):
                if on_progress:
                    on_progress(
                        f"NOAA ISD 正在获取第 {index}/{len(selected)} 个站点："
                        f"{station.station_id} {station.name}"
                    )
                destination = request.output_dir / (
                    f"{DATASET_ID}_{station.station_id}_{request.start_date}_{request.end_date}.csv"
                )
                parsed_destination = destination.with_name(f"{destination.stem}_parsed.csv")
                raw_reused = False
                if destination.exists() and self._valid_csv(destination):
                    raw_reused = True
                    urls.append(self._data_url(station.station_id, request, variables))
                else:
                    destination.unlink(missing_ok=True)
                    try:
                        available_fields = await self._available_fields(client, station, request)
                        self._check_available_variables(variables, available_fields, station)
                        api_url = self._data_url(station.station_id, request, variables)
                        await self._download_stream(client, api_url, destination, on_progress)
                        if not self._valid_csv(destination):
                            raise RuntimeError("NOAA ISD 精确裁剪接口返回了无效 CSV")
                        urls.append(api_url)
                    except ValueError as exc:
                        failures.append({"station": station.station_id, "reason": str(exc)})
                        continue
                    except (httpx.HTTPError, OSError, RuntimeError) as exc:
                        destination.unlink(missing_ok=True)
                        try:
                            source_urls = await self._fallback_station(
                                client, station, request, variables, destination, on_progress
                            )
                        except (httpx.HTTPError, OSError, RuntimeError, ValueError) as fallback_exc:
                            failures.append(
                                {
                                    "station": station.station_id,
                                    "reason": str(fallback_exc),
                                }
                            )
                            continue
                        urls.extend(source_urls)
                        fallbacks.append({"station": station.station_id, "reason": str(exc)})
                parsed_reused = (
                    parsed_destination.exists()
                    and parsed_destination.stat().st_mtime >= destination.stat().st_mtime
                )
                if not parsed_reused:
                    if on_progress:
                        on_progress(f"正在整理 NOAA ISD 常规气象要素：{station.station_id}")
                    try:
                        parse_isd_csv(destination, parsed_destination)
                    except (OSError, ValueError) as exc:
                        parsed_destination.unlink(missing_ok=True)
                        failures.append(
                            {
                                "station": station.station_id,
                                "reason": f"原始数据已下载，但自动解析失败：{exc}",
                            }
                        )
                        continue
                files.append(parsed_destination)
                raw_files.append(destination)
                if raw_reused and parsed_reused:
                    reused.append(parsed_destination)
        if not files:
            detail = "; ".join(
                f"{item['station']}: {item['reason']}" for item in failures[:5]
            )
            raise RuntimeError(f"NOAA ISD 所有站点下载均失败：{detail}")
        warnings = (
            (f"{len(failures)} 个站点下载失败，详情见 metadata.failed_stations",)
            if failures
            else ()
        )
        return DatasetDownloadResult(
            dataset_id=DATASET_ID,
            provider_id=self.provider_id,
            files=tuple(files),
            source_urls=tuple(urls),
            reused_files=tuple(reused),
            warnings=warnings,
            metadata={
                "stations": [station.to_dict() for station in selected],
                "raw_files": [str(path) for path in raw_files],
                "output_format": "human-readable conventional weather CSV",
                "raw_files_preserved": True,
                "requested_variables": list(request.variables),
                "actual_variables": list(variables) if variables else ["all_available_fields"],
                "server_subset": not fallbacks,
                "fallbacks": fallbacks,
                "failed_stations": failures,
            },
        )

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=None, follow_redirects=True, transport=self._transport)

    async def _load_catalogs(
        self, client: httpx.AsyncClient
    ) -> tuple[tuple[DatasetStation, ...], dict[str, dict[tuple[int, int], int]]]:
        if self._stations is None:
            self._stations = parse_station_history(
                await self._catalog_text(client, "isd-history.csv", self._history_url)
            )
        if self._inventory is None:
            self._inventory = parse_station_inventory(
                await self._catalog_text(client, "isd-inventory.csv", self._inventory_url)
            )
        return self._stations, self._inventory

    async def _catalog_text(self, client: httpx.AsyncClient, filename: str, url: str) -> str:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        path = self._cache_dir / filename
        metadata_path = path.with_suffix(path.suffix + ".metadata.json")
        metadata: dict[str, str] = {}
        try:
            metadata = json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
        except (OSError, json.JSONDecodeError):
            metadata = {}
        headers = {}
        if metadata.get("etag"):
            headers["If-None-Match"] = metadata["etag"]
        if metadata.get("last_modified"):
            headers["If-Modified-Since"] = metadata["last_modified"]
        try:
            response = await client.get(url, headers=headers)
            if response.status_code == 304 and path.exists():
                return path.read_text()
            response.raise_for_status()
            path.write_text(response.text)
            metadata_path.write_text(
                json.dumps(
                    {
                        "etag": response.headers.get("etag", ""),
                        "last_modified": response.headers.get("last-modified", ""),
                    }
                )
            )
            return response.text
        except (httpx.HTTPError, OSError):
            if path.exists():
                return path.read_text()
            raise

    def _select_stations(
        self,
        stations: tuple[DatasetStation, ...],
        inventory: dict[str, dict[tuple[int, int], int]],
        request: DatasetDownloadRequest,
        start: date,
        end: date,
    ) -> tuple[DatasetStation, ...]:
        viable = tuple(
            station
            for station in stations
            if self._history_covers(station, start, end)
            and self._observation_count(inventory.get(station.station_id), start, end) != 0
        )
        if request.stations:
            selected = tuple(self._resolve_station(viable, selector) for selector in request.stations)
            if request.area:
                selected = tuple(station for station in selected if self._in_area(station, request.area))
        else:
            selected = tuple(station for station in viable if self._in_area(station, request.area))
            if len(selected) > MAX_AUTO_STATIONS:
                examples = ", ".join(station.station_id for station in selected[:10])
                raise ValueError(
                    f"区域内找到 {len(selected)} 个可用 NOAA ISD 站点，超过自动下载上限 "
                    f"{MAX_AUTO_STATIONS}。请先查询并缩小站点范围。候选示例：{examples}"
                )
        if not selected:
            raise ValueError("请求区域或站点条件没有匹配到可用 NOAA ISD 站点")
        unavailable = [
            station.station_id
            for station in selected
            if not self._history_covers(station, start, end)
            or self._observation_count(inventory.get(station.station_id), start, end) == 0
        ]
        if unavailable:
            raise ValueError(f"以下 NOAA ISD 站点不覆盖请求日期：{', '.join(unavailable)}")
        return tuple(dict.fromkeys(selected))

    @staticmethod
    def _resolve_station(
        stations: tuple[DatasetStation, ...], selector: str
    ) -> DatasetStation:
        key = selector.casefold().strip()
        exact = [
            station
            for station in stations
            if key in {station.station_id.casefold(), station.icao.casefold(), station.name.casefold()}
        ]
        if len(exact) == 1:
            return exact[0]
        candidates = exact or [
            station
            for station in stations
            if key
            and key
            in " ".join((station.station_id, station.name, station.icao)).casefold()
        ]
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            raise ValueError(f"未找到 NOAA ISD 站点：{selector}")
        labels = ", ".join(f"{station.station_id} ({station.name})" for station in candidates[:10])
        raise ValueError(f"站点 {selector} 存在歧义，请指定其中一个：{labels}")

    @staticmethod
    def _history_covers(station: DatasetStation, start: date, end: date) -> bool:
        begin = date.fromisoformat(station.begin_date) if station.begin_date else date.min
        finish = date.fromisoformat(station.end_date) if station.end_date else date.max
        if finish >= date.today() - timedelta(days=730):
            finish = date.max
        return begin <= start and finish >= end

    @staticmethod
    def _observation_count(
        inventory: dict[tuple[int, int], int] | None,
        start: date | None,
        end: date | None,
    ) -> int | None:
        if inventory is None or start is None or end is None:
            return None
        relevant = [
            count
            for (year, month), count in inventory.items()
            if (start.year, start.month) <= (year, month) <= (end.year, end.month)
        ]
        if not relevant:
            return None
        total = sum(relevant)
        if total:
            return total
        positive_months = [month for month, count in inventory.items() if count > 0]
        if positive_months and max(positive_months) < (start.year, start.month):
            return None
        return 0

    @staticmethod
    def _in_area(station: DatasetStation, area: tuple[float, float, float, float] | None) -> bool:
        if area is None:
            return True
        if station.latitude is None or station.longitude is None:
            return False
        north, west, south, east = area
        longitude_matches = west <= station.longitude <= east
        if west > east:
            longitude_matches = station.longitude >= west or station.longitude <= east
        return south <= station.latitude <= north and longitude_matches

    @staticmethod
    def _normalize_variables(variables: tuple[str, ...]) -> tuple[str, ...]:
        normalized: list[str] = []
        for variable in variables:
            value = VARIABLE_ALIASES.get(variable.casefold(), variable.upper())
            if value in BASE_FIELDS:
                continue
            if value not in KNOWN_FIELDS and not FIELD_RE.fullmatch(value):
                raise ValueError(f"NOAA ISD 不支持字段：{variable}")
            normalized.append(value)
        return tuple(dict.fromkeys(normalized))

    async def _available_fields(
        self, client: httpx.AsyncClient, station: DatasetStation, request: DatasetDownloadRequest
    ) -> set[str]:
        params = {
            "dataset": "global-hourly",
            "stations": station.station_id,
            "startDate": request.start_date,
            "endDate": request.end_date,
            "limit": "1",
        }
        response = await client.get(f"{self._search_api}?{urlencode(params)}")
        response.raise_for_status()
        payload = response.json()
        if not payload.get("results"):
            raise RuntimeError(f"站点 {station.station_id} 在请求日期没有可下载记录")
        return {
            str(item.get("key", "")).upper()
            for item in payload.get("dataTypes", {}).get("buckets", ())
            if item.get("key")
        }

    @staticmethod
    def _check_available_variables(
        variables: tuple[str, ...], available: set[str], station: DatasetStation
    ) -> None:
        missing = [variable for variable in variables if variable not in available]
        if missing:
            raise ValueError(
                f"站点 {station.station_id} 在请求日期不提供字段：{', '.join(missing)}。"
                f"可用字段示例：{', '.join(sorted(available)[:30])}"
            )

    def _data_url(
        self, station_id: str, request: DatasetDownloadRequest, variables: tuple[str, ...]
    ) -> str:
        params = {
            "dataset": "global-hourly",
            "stations": station_id,
            "startDate": request.start_date,
            "endDate": request.end_date,
            "format": "csv",
            "includeAttributes": "true",
        }
        if variables:
            params["dataTypes"] = ",".join(variables)
        return f"{self._data_api}?{urlencode(params)}"

    async def _fallback_station(
        self,
        client: httpx.AsyncClient,
        station: DatasetStation,
        request: DatasetDownloadRequest,
        variables: tuple[str, ...],
        destination: Path,
        on_progress: ProgressCallback | None,
    ) -> tuple[str, ...]:
        sources: list[Path] = []
        urls: list[str] = []
        for year in range(int(request.start_date[:4]), int(request.end_date[:4]) + 1):
            url = f"{self._archive_base}/{year}/{station.station_id}.csv"
            source = (
                request.output_dir
                / ".meteora-cache"
                / "isd"
                / str(year)
                / f"{station.station_id}.csv"
            )
            await self._download_stream(client, url, source, on_progress, resume=True)
            sources.append(source)
            urls.append(url)
        self._subset_csv(sources, destination, request.start_date, request.end_date, variables)
        return tuple(urls)

    @staticmethod
    def _subset_csv(
        sources: list[Path],
        destination: Path,
        start_date: str,
        end_date: str,
        variables: tuple[str, ...],
    ) -> None:
        rows: list[dict[str, str]] = []
        source_fields: list[str] = []
        for source in sources:
            with source.open(newline="") as input_file:
                reader = csv.DictReader(input_file)
                if not source_fields:
                    source_fields = list(reader.fieldnames or ())
                rows.extend(
                    row
                    for row in reader
                    if start_date <= row.get("DATE", "")[:10] <= end_date
                )
        if not rows:
            raise RuntimeError("NOAA ISD 归档文件中没有请求日期的记录")
        fields = source_fields
        if variables:
            fields = [field for field in (*BASE_FIELDS, *variables) if field in source_fields]
        destination.parent.mkdir(parents=True, exist_ok=True)
        with destination.open("w", newline="") as output:
            writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)

    @staticmethod
    def _valid_csv(path: Path) -> bool:
        try:
            with path.open(newline="") as input_file:
                reader = csv.reader(input_file)
                fields = next(reader)
                next(reader)
        except (OSError, StopIteration, csv.Error):
            return False
        return "STATION" in fields and "DATE" in fields

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
            if not resume:
                part.unlink(missing_ok=True)
            offset = part.stat().st_size if resume and part.exists() else 0
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            try:
                async with client.stream("GET", url, headers=headers) as response:
                    response.raise_for_status()
                    if offset and response.status_code != 206:
                        offset = 0
                        part.unlink(missing_ok=True)
                    value = response.headers.get("content-length", "")
                    total = offset + int(value) if value.isdigit() else 0
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
