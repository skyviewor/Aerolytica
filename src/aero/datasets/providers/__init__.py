"""Built-in dataset providers."""

from aero.datasets.providers.chirps import ChirpsProvider
from aero.datasets.providers.ghcn_daily import GhcnDailyProvider
from aero.datasets.providers.goes import GoesProvider
from aero.datasets.providers.himawari import HimawariProvider
from aero.datasets.providers.hrrr import HrrrProvider
from aero.datasets.providers.jra3q import Jra3qProvider
from aero.datasets.providers.jra55 import Jra55Provider
from aero.datasets.providers.mrms import MrmsProvider
from aero.datasets.providers.ncep_reanalysis import NcepReanalysisProvider
from aero.datasets.providers.noaa_isd import NoaaIsdProvider

__all__ = [
    "ChirpsProvider",
    "GhcnDailyProvider",
    "GoesProvider",
    "HimawariProvider",
    "HrrrProvider",
    "Jra3qProvider",
    "Jra55Provider",
    "MrmsProvider",
    "NcepReanalysisProvider",
    "NoaaIsdProvider",
]
