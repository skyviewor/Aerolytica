"""Built-in dataset providers."""

from aero.datasets.providers.chirps import ChirpsProvider
from aero.datasets.providers.ncep_reanalysis import NcepReanalysisProvider
from aero.datasets.providers.noaa_isd import NoaaIsdProvider

__all__ = ["ChirpsProvider", "NcepReanalysisProvider", "NoaaIsdProvider"]
