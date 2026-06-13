"""Dataset catalogue, search, and provider dispatch."""

from __future__ import annotations

from aero.datasets.models import (
    DatasetDownloadRequest,
    DatasetDownloadResult,
    DatasetSpec,
    DatasetStation,
)
from aero.datasets.provider import DatasetProvider, ProgressCallback


class DatasetCatalog:
    def __init__(self, providers: tuple[DatasetProvider, ...] = ()) -> None:
        self._dataset_providers: dict[str, DatasetProvider] = {}
        self._datasets: dict[str, DatasetSpec] = {}
        for provider in providers:
            self.register(provider)

    def register(self, provider: DatasetProvider) -> None:
        for dataset in provider.list_datasets():
            self.register_spec(dataset)
            self._dataset_providers[dataset.dataset_id] = provider

    def register_spec(self, dataset: DatasetSpec) -> None:
        if dataset.dataset_id in self._datasets:
            raise ValueError(f"重复的数据集 ID: {dataset.dataset_id}")
        self._datasets[dataset.dataset_id] = dataset

    def list_datasets(self) -> tuple[DatasetSpec, ...]:
        return tuple(self._datasets.values())

    def describe(self, dataset_id: str) -> DatasetSpec:
        try:
            return self._datasets[dataset_id]
        except KeyError as exc:
            raise ValueError(f"未知数据集: {dataset_id}") from exc

    def search(
        self,
        query: str = "",
        *,
        domain: str = "",
        provider: str = "",
        requires_auth: bool | None = None,
    ) -> tuple[DatasetSpec, ...]:
        terms = [term.casefold() for term in query.split() if term.strip()]
        domain_key = domain.casefold().strip()
        provider_key = provider.casefold().strip()
        results: list[DatasetSpec] = []
        for dataset in self._datasets.values():
            if domain_key and domain_key not in dataset.domain.casefold():
                continue
            provider_text = f"{dataset.provider_id} {dataset.provider_name}".casefold()
            if provider_key and provider_key not in provider_text:
                continue
            if requires_auth is not None and dataset.requires_auth != requires_auth:
                continue
            haystack = " ".join(
                [
                    dataset.dataset_id,
                    dataset.name,
                    dataset.domain,
                    dataset.description,
                    dataset.provider_name,
                    *(variable.name for variable in dataset.variables),
                    *(variable.long_name for variable in dataset.variables),
                    *(alias for variable in dataset.variables for alias in variable.aliases),
                ]
            ).casefold()
            if terms and not all(term in haystack for term in terms):
                continue
            results.append(dataset)
        return tuple(results)

    async def download(
        self,
        request: DatasetDownloadRequest,
        on_progress: ProgressCallback | None = None,
    ) -> DatasetDownloadResult:
        dataset = self.describe(request.dataset_id)
        provider = self._dataset_providers.get(dataset.dataset_id)
        if provider is None:
            raise ValueError(
                f"{dataset.name} 需要通过对应的专用下载能力处理：{dataset.download_tool}"
            )
        return await provider.download(request, on_progress=on_progress)

    async def search_variables(self, dataset_id: str, query: str = "") -> tuple[str, ...]:
        dataset = self.describe(dataset_id)
        provider = self._dataset_providers.get(dataset.dataset_id)
        search = getattr(provider, "search_variables", None) if provider is not None else None
        if search is None:
            return tuple(
                variable.name
                for variable in dataset.variables
                if not query or query.casefold() in variable.name.casefold()
            )
        return await search(dataset_id, query)

    async def search_stations(
        self,
        dataset_id: str,
        query: str = "",
        area: tuple[float, float, float, float] | None = None,
        start_date: str = "",
        end_date: str = "",
    ) -> tuple[DatasetStation, ...]:
        dataset = self.describe(dataset_id)
        provider = self._dataset_providers.get(dataset.dataset_id)
        search = getattr(provider, "search_stations", None) if provider is not None else None
        if search is None:
            raise ValueError(f"{dataset.name} 不支持站点查询")
        return await search(dataset_id, query, area, start_date, end_date)


_DEFAULT_CATALOG: DatasetCatalog | None = None


def get_dataset_catalog() -> DatasetCatalog:
    global _DEFAULT_CATALOG
    if _DEFAULT_CATALOG is None:
        from aero.datasets.builtin_specs import BUILTIN_DATASET_SPECS
        from aero.datasets.providers.chirps import ChirpsProvider
        from aero.datasets.providers.ghcn_daily import GhcnDailyProvider
        from aero.datasets.providers.goes import GoesProvider
        from aero.datasets.providers.himawari import HimawariProvider
        from aero.datasets.providers.hrrr import HrrrProvider
        from aero.datasets.providers.jra3q import Jra3qProvider
        from aero.datasets.providers.jra55 import Jra55Provider
        from aero.datasets.providers.merra2 import Merra2Provider
        from aero.datasets.providers.mrms import MrmsProvider
        from aero.datasets.providers.ncep_reanalysis import NcepReanalysisProvider
        from aero.datasets.providers.noaa_isd import NoaaIsdProvider

        _DEFAULT_CATALOG = DatasetCatalog(
            (
                ChirpsProvider(),
                GhcnDailyProvider(),
                GoesProvider(),
                HimawariProvider(),
                HrrrProvider(),
                Jra3qProvider(),
                Jra55Provider(),
                Merra2Provider(),
                MrmsProvider(),
                NcepReanalysisProvider(),
                NoaaIsdProvider(),
            )
        )
        for dataset in BUILTIN_DATASET_SPECS:
            _DEFAULT_CATALOG.register_spec(dataset)
    return _DEFAULT_CATALOG
