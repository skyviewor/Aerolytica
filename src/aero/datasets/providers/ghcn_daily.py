"""NOAA Global Historical Climatology Network Daily AWS Open Data provider."""

from __future__ import annotations

import csv
import gzip
import json
import re
from collections import defaultdict
from datetime import date
from pathlib import Path

import httpx

from aero.agent.progress import cancel_requested
from aero.datasets.models import (
    DatasetDownloadRequest,
    DatasetDownloadResult,
    DatasetSpec,
    DatasetStation,
    DatasetVariable,
)
from aero.datasets.provider import ProgressCallback

DATASET_ID = "noaa-ghcn-daily"
PROVIDER_ID = "noaa-nodd"
BASE_URL = "https://noaa-ghcn-pds.s3.amazonaws.com"
ACCESS_BASE = "https://www.ncei.noaa.gov/data/global-historical-climatology-network-daily/access"
CHUNK_SIZE = 1024 * 1024
MAX_AUTO_STATIONS = 50
STATION_ID_RE = re.compile(r"^[A-Z0-9]{11}$")
RAW_FIELDS = ("ID", "DATE", "ELEMENT", "DATA_VALUE", "M_FLAG", "Q_FLAG", "S_FLAG", "OBS_TIME")

VARIABLES = {
    "TMAX": ("daily maximum temperature", "°C", 0.1, ("maximum temperature", "最高气温")),
    "TMIN": ("daily minimum temperature", "°C", 0.1, ("minimum temperature", "最低气温")),
    "TAVG": ("daily average temperature", "°C", 0.1, ("average temperature", "平均气温")),
    "PRCP": ("daily precipitation", "mm", 0.1, ("precipitation", "降水")),
    "SNOW": ("daily snowfall", "mm", 1.0, ("snowfall", "降雪")),
    "SNWD": ("snow depth", "mm", 1.0, ("snow depth", "积雪深度")),
    "AWND": ("average daily wind speed", "m/s", 0.1, ("wind", "平均风速")),
    "WESD": ("water equivalent of snow on the ground", "mm", 0.1, ("snow water", "雪水当量")),
    "WESF": ("water equivalent of snowfall", "mm", 0.1, ("snowfall water",)),
    "EVAP": ("evaporation", "mm", 0.1, ("evaporation", "蒸发")),
    "WSFG": ("peak gust speed", "m/s", 0.1, ("gust", "阵风")),
    "WDFG": ("direction of peak gust", "degree", 1.0, ("gust direction", "阵风风向")),
    "PGTM": ("time of peak gust", "HHMM", 1.0, ("gust time",)),
}
VARIABLE_ALIASES = {
    alias.casefold(): code for code, (_, _, _, aliases) in VARIABLES.items() for alias in aliases
}

GHCN_DAILY_SPEC = DatasetSpec(
    dataset_id=DATASET_ID,
    name="NOAA Global Historical Climatology Network Daily",
    provider_id=PROVIDER_ID,
    provider_name="NOAA Open Data Dissemination",
    domain="observations",
    description="全球地面站逐日气温、降水、降雪、风等气候观测，来自 GHCN-Daily。",
    variables=tuple(
        DatasetVariable(code, description, unit, aliases)
        for code, (description, unit, _, aliases) in VARIABLES.items()
    ),
    spatial_coverage="全球陆地观测站",
    temporal_coverage="1750 至接近实时，具体范围按站点和要素确认",
    spatial_resolution="station observations",
    temporal_resolution="daily",
    file_formats=("CSV",),
    download_granularity="station file, locally subset by date and element",
    source_url="https://registry.opendata.aws/noaa-ghcn/",
    citation_url="https://www.ncei.noaa.gov/products/land-based-station/global-historical-climatology-network-daily",
    supports_server_time_subset=False,
    supports_server_area_subset=False,
    supports_resume=True,
    notes=(
        "下载前需指定站点或区域；区域自动选择最多 50 个站点。",
        "按站点下载 AWS 压缩归档，并在本地精确筛选日期和气象要素。",
        "输出将 NOAA 原始整数缩放为常用单位，同时保留质量和来源标记。",
    ),
)


def _parse_date(value: str, label: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} 必须使用 YYYY-MM-DD 格式") from exc


def _float(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


def parse_countries(text: str) -> dict[str, str]:
    return {
        line[:2]: line[3:].strip()
        for line in text.splitlines()
        if len(line) >= 4 and line[:2].strip()
    }


def parse_station_inventory(text: str) -> dict[str, dict[str, tuple[int, int]]]:
    inventory: dict[str, dict[str, tuple[int, int]]] = defaultdict(dict)
    for line in text.splitlines():
        if len(line) < 45:
            continue
        station_id = line[0:11].strip()
        element = line[31:35].strip()
        try:
            first_year = int(line[36:40])
            last_year = int(line[41:45])
        except ValueError:
            continue
        inventory[station_id][element] = (first_year, last_year)
    return dict(inventory)


def parse_stations(
    text: str,
    countries: dict[str, str],
    inventory: dict[str, dict[str, tuple[int, int]]],
) -> tuple[DatasetStation, ...]:
    stations: list[DatasetStation] = []
    for line in text.splitlines():
        if len(line) < 71:
            continue
        station_id = line[0:11].strip()
        if not STATION_ID_RE.fullmatch(station_id):
            continue
        histories = inventory.get(station_id, {}).values()
        first_year = min((years[0] for years in histories), default=None)
        last_year = max((years[1] for years in histories), default=None)
        stations.append(
            DatasetStation(
                station_id=station_id,
                name=line[41:71].strip(),
                country=countries.get(station_id[:2], station_id[:2]),
                state=line[38:40].strip(),
                latitude=_float(line[12:20].strip()),
                longitude=_float(line[21:30].strip()),
                elevation_m=_float(line[31:37].strip()),
                begin_date=f"{first_year:04d}-01-01" if first_year else "",
                end_date=f"{last_year:04d}-12-31" if last_year else "",
            )
        )
    return tuple(stations)


class GhcnDailyProvider:
    provider_id = PROVIDER_ID

    def __init__(
        self,
        *,
        base_url: str = BASE_URL,
        access_base: str = ACCESS_BASE,
        cache_dir: Path | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._access_base = access_base.rstrip("/")
        self._cache_dir = cache_dir or Path.home() / ".cache" / "aero" / "ghcn-daily"
        self._transport = transport
        self._stations: tuple[DatasetStation, ...] | None = None
        self._inventory: dict[str, dict[str, tuple[int, int]]] | None = None

    def list_datasets(self) -> tuple[DatasetSpec, ...]:
        return (GHCN_DAILY_SPEC,)

    async def search_variables(self, dataset_id: str, query: str = "") -> tuple[str, ...]:
        self._check_dataset(dataset_id)
        term = query.casefold().strip()
        values = [
            f"{code}: {description} ({unit})"
            for code, (description, unit, _, _) in VARIABLES.items()
        ]
        if not term:
            return tuple(values)
        return tuple(
            value
            for value in values
            if term in value.casefold()
            or any(term in alias.casefold() for alias in VARIABLES[value.split(":", 1)[0]][3])
        )

    async def search_stations(
        self,
        dataset_id: str,
        query: str = "",
        area: tuple[float, float, float, float] | None = None,
        start_date: str = "",
        end_date: str = "",
    ) -> tuple[DatasetStation, ...]:
        self._check_dataset(dataset_id)
        start = _parse_date(start_date, "start_date") if start_date else None
        end = _parse_date(end_date, "end_date") if end_date else start
        if start and end and end < start:
            raise ValueError("end_date 不能早于 start_date")
        async with self._client() as client:
            stations, _ = await self._load_catalogs(client)
        term = query.casefold().strip()
        return tuple(
            station
            for station in stations
            if (not term or term in self._station_text(station).casefold())
            and self._in_area(station, area)
            and (not start or not end or self._history_overlaps(station, start, end))
        )

    async def download(
        self,
        request: DatasetDownloadRequest,
        on_progress: ProgressCallback | None = None,
    ) -> DatasetDownloadResult:
        self._check_dataset(request.dataset_id)
        start = _parse_date(request.start_date, "start_date")
        end = _parse_date(request.end_date, "end_date")
        if end < start:
            raise ValueError("end_date 不能早于 start_date")
        if not request.stations and request.area is None:
            raise ValueError("NOAA GHCN-D 下载必须指定 stations 或 area")
        variables = self._normalize_variables(request.variables)
        request.output_dir.mkdir(parents=True, exist_ok=True)

        files: list[Path] = []
        raw_files: list[Path] = []
        urls: list[str] = []
        reused: list[Path] = []
        failures: list[dict[str, str]] = []
        actual_ranges: dict[str, dict[str, str]] = {}
        actual_variables: dict[str, list[str]] = {}
        async with self._client() as client:
            stations, inventory = await self._load_catalogs(client)
            selected = self._select_stations(stations, inventory, request, start, end, variables)
            for index, station in enumerate(selected, start=1):
                if on_progress:
                    on_progress(
                        f"NOAA GHCN-D 正在获取第 {index}/{len(selected)} 个站点："
                        f"{station.station_id} {station.name}"
                    )
                access_raw = (
                    request.output_dir / ".aero-cache" / "ghcn-daily" / f"{station.station_id}.csv"
                )
                sources = (
                    (f"{self._access_base}/{station.station_id}.csv", access_raw),
                    (
                        f"{self._base_url}/csv.gz/by_station/{station.station_id}.csv.gz",
                        access_raw.with_suffix(".csv.gz"),
                    ),
                )
                destination = request.output_dir / (
                    f"{DATASET_ID}_{station.station_id}_{request.start_date}_{request.end_date}.csv"
                )
                source_error: Exception | None = None
                for url, raw in sources:
                    try:
                        current_source = raw.suffix == ".csv"
                        raw_reused = not current_source and raw.exists() and raw.stat().st_size > 0
                        if not raw_reused:
                            await self._download_stream(client, url, raw, on_progress)
                        parsed_reused = (
                            destination.exists()
                            and destination.stat().st_mtime >= raw.stat().st_mtime
                        )
                        if not parsed_reused:
                            actual_range, found_variables = self._subset_csv(
                                raw, destination, start, end, variables
                            )
                        else:
                            actual_range, found_variables = self._inspect_subset(destination)
                        files.append(destination)
                        raw_files.append(raw)
                        urls.append(url)
                        actual_ranges[station.station_id] = actual_range
                        actual_variables[station.station_id] = list(found_variables)
                        if raw_reused and parsed_reused:
                            reused.append(destination)
                        break
                    except (OSError, RuntimeError, ValueError, httpx.HTTPError) as exc:
                        source_error = exc
                        destination.unlink(missing_ok=True)
                else:
                    failures.append({"station": station.station_id, "reason": str(source_error)})
        if not files:
            details = "; ".join(f"{item['station']}: {item['reason']}" for item in failures[:5])
            raise RuntimeError(f"NOAA GHCN-D 所有站点下载均失败：{details}")
        partial_ranges = {
            station_id: actual_range
            for station_id, actual_range in actual_ranges.items()
            if actual_range["start_date"] > request.start_date
            or actual_range["end_date"] < request.end_date
        }
        warnings: list[str] = []
        if failures:
            warnings.append(f"{len(failures)} 个站点下载失败，详情见 metadata.failed_stations")
        if partial_ranges:
            warnings.append(
                "部分站点仅返回请求区间内实际存在观测的日期，详情见 metadata.partial_ranges"
            )
        return DatasetDownloadResult(
            dataset_id=DATASET_ID,
            provider_id=self.provider_id,
            files=tuple(files),
            source_urls=tuple(urls),
            reused_files=tuple(reused),
            warnings=tuple(warnings),
            metadata={
                "stations": [station.to_dict() for station in selected],
                "raw_files": [str(path) for path in raw_files],
                "raw_files_preserved": True,
                "requested_variables": list(request.variables),
                "actual_variables": list(variables) if variables else ["all_available_elements"],
                "actual_ranges": actual_ranges,
                "actual_variables_by_station": actual_variables,
                "partial_ranges": partial_ranges,
                "output_format": "decoded GHCN-D long-form CSV",
                "server_subset": False,
                "requires_local_subset": False,
                "failed_stations": failures,
            },
        )

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=None, follow_redirects=True, transport=self._transport)

    async def _load_catalogs(
        self, client: httpx.AsyncClient
    ) -> tuple[tuple[DatasetStation, ...], dict[str, dict[str, tuple[int, int]]]]:
        if self._inventory is None:
            self._inventory = parse_station_inventory(
                await self._catalog_text(client, "ghcnd-inventory.txt")
            )
        if self._stations is None:
            countries = parse_countries(await self._catalog_text(client, "ghcnd-countries.txt"))
            self._stations = parse_stations(
                await self._catalog_text(client, "ghcnd-stations.txt"),
                countries,
                self._inventory,
            )
        return self._stations, self._inventory

    async def _catalog_text(self, client: httpx.AsyncClient, filename: str) -> str:
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        path = self._cache_dir / filename
        metadata_path = path.with_suffix(path.suffix + ".metadata.json")
        try:
            metadata = json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
        except (OSError, json.JSONDecodeError):
            metadata = {}
        headers = {}
        if metadata.get("etag"):
            headers["If-None-Match"] = metadata["etag"]
        try:
            response = await client.get(f"{self._base_url}/{filename}", headers=headers)
            if response.status_code == 304 and path.exists():
                return path.read_text()
            response.raise_for_status()
            path.write_text(response.text)
            metadata_path.write_text(json.dumps({"etag": response.headers.get("etag", "")}))
            return response.text
        except (httpx.HTTPError, OSError):
            if path.exists():
                return path.read_text()
            raise

    def _select_stations(
        self,
        stations: tuple[DatasetStation, ...],
        inventory: dict[str, dict[str, tuple[int, int]]],
        request: DatasetDownloadRequest,
        start: date,
        end: date,
        variables: tuple[str, ...],
    ) -> tuple[DatasetStation, ...]:
        viable = tuple(
            station
            for station in stations
            if self._history_overlaps(station, start, end)
            and self._elements_overlap(inventory.get(station.station_id, {}), variables, start, end)
        )
        if request.stations:
            selected = tuple(
                self._resolve_station(stations, selector) for selector in request.stations
            )
            unavailable = [station.station_id for station in selected if station not in viable]
            if unavailable:
                raise ValueError(
                    f"以下 NOAA GHCN-D 站点与请求日期或要素没有可用交集：{', '.join(unavailable)}"
                )
            if request.area:
                selected = tuple(
                    station for station in selected if self._in_area(station, request.area)
                )
        else:
            selected = tuple(station for station in viable if self._in_area(station, request.area))
            if len(selected) > MAX_AUTO_STATIONS:
                examples = ", ".join(station.station_id for station in selected[:10])
                raise ValueError(
                    f"区域内找到 {len(selected)} 个可用 NOAA GHCN-D 站点，超过自动下载上限 "
                    f"{MAX_AUTO_STATIONS}。请先查询并缩小站点范围。候选示例：{examples}"
                )
        if not selected:
            raise ValueError("请求区域或站点条件没有匹配到可用 NOAA GHCN-D 站点")
        return tuple(dict.fromkeys(selected))

    @staticmethod
    def _resolve_station(stations: tuple[DatasetStation, ...], selector: str) -> DatasetStation:
        key = selector.casefold().strip()
        exact = [
            station
            for station in stations
            if key in {station.station_id.casefold(), station.name.casefold()}
        ]
        suffix = [
            station
            for station in stations
            if key and len(key) >= 5 and station.station_id.casefold().endswith(key)
        ]
        candidates = exact or [
            station
            for station in stations
            if key and key in GhcnDailyProvider._station_text(station)
        ]
        candidates = exact or suffix or candidates
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            raise ValueError(f"未找到 NOAA GHCN-D 站点：{selector}")
        labels = ", ".join(f"{item.station_id} ({item.name})" for item in candidates[:10])
        raise ValueError(f"站点 {selector} 存在歧义，请指定其中一个：{labels}")

    @staticmethod
    def _station_text(station: DatasetStation) -> str:
        return " ".join(
            (station.station_id, station.name, station.country, station.state)
        ).casefold()

    @staticmethod
    def _history_overlaps(station: DatasetStation, start: date, end: date) -> bool:
        begin = date.fromisoformat(station.begin_date) if station.begin_date else date.min
        finish = date.fromisoformat(station.end_date) if station.end_date else date.max
        return begin <= end and finish >= start

    @staticmethod
    def _elements_overlap(
        inventory: dict[str, tuple[int, int]],
        variables: tuple[str, ...],
        start: date,
        end: date,
    ) -> bool:
        requested = variables or tuple(inventory)
        return bool(requested) and all(
            element in inventory
            and inventory[element][0] <= end.year
            and inventory[element][1] >= start.year
            for element in requested
        )

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
            if value not in VARIABLES and not re.fullmatch(r"[A-Z0-9]{4}", value):
                raise ValueError(f"NOAA GHCN-D 不支持要素：{variable}")
            normalized.append(value)
        return tuple(dict.fromkeys(normalized))

    @staticmethod
    def _subset_csv(
        source: Path,
        destination: Path,
        start: date,
        end: date,
        variables: tuple[str, ...],
    ) -> tuple[dict[str, str], tuple[str, ...]]:
        fields = (
            "station_id",
            "date",
            "element",
            "value",
            "unit",
            "raw_value",
            "m_flag",
            "q_flag",
            "s_flag",
            "obs_time",
        )
        destination.parent.mkdir(parents=True, exist_ok=True)
        part = destination.with_suffix(destination.suffix + ".part")
        count = 0
        first_date: date | None = None
        last_date: date | None = None
        available_first: date | None = None
        available_last: date | None = None
        found_variables: list[str] = []
        input_file = (
            gzip.open(source, "rt", newline="")
            if source.suffix == ".gz"
            else source.open("r", newline="")
        )
        with input_file, part.open("w", newline="") as output:
            first_line = input_file.readline()
            input_file.seek(0)
            ncei_wide = "STATION" in first_line and "LATITUDE" in first_line
            reader = csv.DictReader(input_file, fieldnames=None if ncei_wide else RAW_FIELDS)
            writer = csv.DictWriter(output, fieldnames=fields)
            writer.writeheader()
            for row in reader:
                source_rows = (
                    GhcnDailyProvider._decode_ncei_row(row, variables) if ncei_wide else (row,)
                )
                for source_row in source_rows:
                    try:
                        source_date = date.fromisoformat(source_row["DATE"])
                    except (KeyError, ValueError):
                        source_date = None
                    source_element = source_row.get("ELEMENT", "")
                    if source_date and (not variables or source_element in variables):
                        available_first = (
                            source_date
                            if available_first is None
                            else min(available_first, source_date)
                        )
                        available_last = (
                            source_date
                            if available_last is None
                            else max(available_last, source_date)
                        )
                    written = GhcnDailyProvider._write_subset_row(
                        writer, source_row, start, end, variables
                    )
                    if written is None:
                        continue
                    observation_date, element = written
                    count += 1
                    first_date = (
                        observation_date
                        if first_date is None
                        else min(first_date, observation_date)
                    )
                    last_date = (
                        observation_date if last_date is None else max(last_date, observation_date)
                    )
                    if element not in found_variables:
                        found_variables.append(element)
        if not count:
            part.unlink(missing_ok=True)
            if available_first and available_last:
                raise ValueError(
                    "站点归档中没有匹配请求日期和要素的 GHCN-D 记录；"
                    f"请求要素实际可用范围为 {available_first.isoformat()} 至 "
                    f"{available_last.isoformat()}"
                )
            raise ValueError("站点归档中没有匹配请求日期和要素的 GHCN-D 记录")
        part.replace(destination)
        return (
            {"start_date": first_date.isoformat(), "end_date": last_date.isoformat()},
            tuple(found_variables),
        )

    @staticmethod
    def _decode_ncei_row(
        row: dict[str, str], variables: tuple[str, ...]
    ) -> tuple[dict[str, str], ...]:
        decoded: list[dict[str, str]] = []
        elements = variables or tuple(
            field for field in row if field in VARIABLES and row.get(field, "").strip()
        )
        for element in elements:
            raw_value = row.get(element, "").strip()
            if not raw_value:
                continue
            attributes = (row.get(f"{element}_ATTRIBUTES", "") or "").split(",")
            decoded.append(
                {
                    "ID": row.get("STATION", "").strip(),
                    "DATE": row.get("DATE", "").strip(),
                    "ELEMENT": element,
                    "DATA_VALUE": raw_value,
                    "M_FLAG": attributes[0] if len(attributes) > 0 else "",
                    "Q_FLAG": attributes[1] if len(attributes) > 1 else "",
                    "S_FLAG": attributes[2] if len(attributes) > 2 else "",
                    "OBS_TIME": attributes[3] if len(attributes) > 3 else "",
                }
            )
        return tuple(decoded)

    @staticmethod
    def _write_subset_row(
        writer: csv.DictWriter,
        row: dict[str, str],
        start: date,
        end: date,
        variables: tuple[str, ...],
    ) -> tuple[date, str] | None:
        try:
            observation_date = date.fromisoformat(row["DATE"])
        except (KeyError, ValueError):
            return None
        element = row.get("ELEMENT", "")
        if not start <= observation_date <= end or variables and element not in variables:
            return None
        raw_value = row.get("DATA_VALUE", "")
        definition = VARIABLES.get(element)
        value = raw_value
        unit = "NOAA encoded value"
        if definition:
            try:
                value = f"{int(raw_value) * definition[2]:g}"
            except ValueError:
                pass
            unit = definition[1]
        writer.writerow(
            {
                "station_id": row.get("ID", ""),
                "date": observation_date.isoformat(),
                "element": element,
                "value": value,
                "unit": unit,
                "raw_value": raw_value,
                "m_flag": row.get("M_FLAG", ""),
                "q_flag": row.get("Q_FLAG", ""),
                "s_flag": row.get("S_FLAG", ""),
                "obs_time": row.get("OBS_TIME", ""),
            }
        )
        return observation_date, element

    @staticmethod
    def _inspect_subset(path: Path) -> tuple[dict[str, str], tuple[str, ...]]:
        dates: list[str] = []
        variables: list[str] = []
        with path.open(newline="") as input_file:
            for row in csv.DictReader(input_file):
                if row.get("date"):
                    dates.append(row["date"])
                if row.get("element") and row["element"] not in variables:
                    variables.append(row["element"])
        if not dates:
            raise ValueError("缓存的 GHCN-D 裁剪文件没有有效记录")
        return (
            {"start_date": min(dates), "end_date": max(dates)},
            tuple(variables),
        )

    @staticmethod
    async def _download_stream(
        client: httpx.AsyncClient,
        url: str,
        destination: Path,
        on_progress: ProgressCallback | None,
    ) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        part = destination.with_suffix(destination.suffix + ".part")
        offset = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        async with client.stream("GET", url, headers=headers) as response:
            if offset and response.status_code == 200:
                offset = 0
                part.unlink(missing_ok=True)
            response.raise_for_status()
            total = int(response.headers.get("content-length", "0")) + offset
            with part.open("ab" if offset else "wb") as output:
                downloaded = offset
                async for chunk in response.aiter_bytes(CHUNK_SIZE):
                    if cancel_requested():
                        raise RuntimeError("下载已取消")
                    output.write(chunk)
                    downloaded += len(chunk)
                    if on_progress and total:
                        on_progress(downloaded, total)
        part.replace(destination)

    @staticmethod
    def _check_dataset(dataset_id: str) -> None:
        if dataset_id != DATASET_ID:
            raise ValueError(f"{PROVIDER_ID} 不支持数据集 {dataset_id}")
