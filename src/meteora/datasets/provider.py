"""Provider interface for unified dataset access."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

from meteora.datasets.models import (
    DatasetDownloadRequest,
    DatasetDownloadResult,
    DatasetSpec,
)

ProgressCallback = Callable[..., None]


class DatasetProvider(Protocol):
    provider_id: str

    def list_datasets(self) -> tuple[DatasetSpec, ...]: ...

    async def download(
        self,
        request: DatasetDownloadRequest,
        on_progress: ProgressCallback | None = None,
    ) -> DatasetDownloadResult: ...
