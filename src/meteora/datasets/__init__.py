"""Unified dataset catalogue and provider adapters."""

from meteora.datasets.catalog import DatasetCatalog, get_dataset_catalog
from meteora.datasets.models import (
    DatasetDownloadRequest,
    DatasetDownloadResult,
    DatasetSpec,
    DatasetStation,
    DatasetVariable,
)

__all__ = [
    "DatasetCatalog",
    "DatasetDownloadRequest",
    "DatasetDownloadResult",
    "DatasetSpec",
    "DatasetStation",
    "DatasetVariable",
    "get_dataset_catalog",
]
