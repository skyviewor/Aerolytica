"""Shared dataset catalogue and download models."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DatasetVariable:
    name: str
    long_name: str
    units: str
    aliases: tuple[str, ...] = ()
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DatasetSpec:
    dataset_id: str
    name: str
    provider_id: str
    provider_name: str
    domain: str
    description: str
    variables: tuple[DatasetVariable, ...]
    spatial_coverage: str
    temporal_coverage: str
    spatial_resolution: str
    temporal_resolution: str
    file_formats: tuple[str, ...]
    download_granularity: str
    source_url: str
    citation_url: str
    download_tool: str = "download_dataset"
    requires_auth: bool = False
    supports_server_time_subset: bool = False
    supports_server_area_subset: bool = False
    supports_resume: bool = False
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DatasetStation:
    station_id: str
    name: str
    country: str = ""
    state: str = ""
    icao: str = ""
    latitude: float | None = None
    longitude: float | None = None
    elevation_m: float | None = None
    begin_date: str = ""
    end_date: str = ""
    monthly_observations: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DatasetDownloadRequest:
    dataset_id: str
    start_date: str
    end_date: str
    output_dir: Path
    variables: tuple[str, ...] = ()
    levels: tuple[float, ...] = ()
    stations: tuple[str, ...] = ()
    area: tuple[float, float, float, float] | None = None


@dataclass(frozen=True)
class DatasetDownloadResult:
    dataset_id: str
    provider_id: str
    files: tuple[Path, ...]
    source_urls: tuple[str, ...]
    reused_files: tuple[Path, ...] = ()
    warnings: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dataset_id": self.dataset_id,
            "provider_id": self.provider_id,
            "files": [str(path) for path in self.files],
            "source_urls": list(self.source_urls),
            "reused_files": [str(path) for path in self.reused_files],
            "warnings": list(self.warnings),
            "metadata": self.metadata,
        }
