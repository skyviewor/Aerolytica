"""Compatibility aggregation module for Aero's built-in tools.

Importing this module registers all built-in tools. New domain-focused tools
belong in ``aero.toolbox.tools`` and are re-exported here while older tools
are migrated incrementally.
"""

from aero.toolbox.tools.compat import (
    _download_progress_reporter,
    _find_config,
    _find_config_path,
    _find_project_dir,
    _fmt_duration,
    _fmt_size,
    _runtime_tools_ready,
    _short_path,
    scan_local_files,
)
from aero.toolbox.tools.datasets import (
    describe_dataset,
    download_dataset,
    search_dataset_stations,
    search_dataset_variables,
    search_datasets,
)
from aero.toolbox.tools.configuration import (
    check_cds_config,
    clear_cds_config,
    clear_llm_config,
    configure_cds_key,
    configure_llm_provider,
    list_llm_providers,
)


from aero.toolbox.tools.vision import (
    _ensure_vision_client,
    analyze_image,
    check_vision_model_config,
    configure_vision_model,
    get_vision_usage,
    reset_vision_usage,
)


from aero.toolbox.tools.netcdf import inspect_nc, subset_netcdf
from aero.toolbox.tools.observations import inspect_csv_table, parse_isd_csv


from aero.toolbox.tools.listing import delete_file, list_figures, list_files


from aero.toolbox.tools.download_records import (
    cleanup_downloads,
    list_downloads,
    query_download,
    retry_download,
)


from aero.toolbox.tools.files import edit_file, read_file, write_file


from aero.toolbox.runtime_manager import (
    RUNTIME_TOOL_PACKAGES as _RUNTIME_TOOL_PACKAGES,
)
from aero.toolbox.tools.runtime import ensure_runtime_tools, run_shell


from aero.toolbox.tools.literature import (
    download_literature_pdf,
    list_literature,
    save_literature,
    search_literature,
)


from aero.toolbox.tools.planning import propose_execution, write_plan_document


from aero.toolbox.tools.documents import preview_image, read_pdf


from aero.toolbox.tools.email import (
    check_email_config,
    configure_email_config,
    send_email,
)
from aero.toolbox.tools.era5 import (
    _reuse_existing_era5_file,
    _summarize_dataset,
    check_era5_availability,
    download_era5,
)
from aero.toolbox.tools.gfs import (
    check_gfs_availability,
    download_gfs,
    get_gfs_forecast_schedule,
    inspect_gfs_inventory,
)
from aero.toolbox.tools.gefs import (
    check_gefs_availability,
    download_gefs,
    get_gefs_forecast_schedule,
    lookup_gefs_parameter,
    search_gefs_variables,
)
from aero.toolbox.tools.grib import (
    _inspect_grib2_with_cfgrib,
    _load_gfs_parameter_lookup,
    _parse_grib2_messages,
    _parse_grib2_product_metadata,
    inspect_grib2,
)
from aero.toolbox.tools.ifs import (
    check_ifs_availability,
    download_ifs,
    get_ifs_forecast_schedule,
    search_ifs_variables,
)
from aero.toolbox.tools.parameters import (
    describe_cds_dataset,
    lookup_ecmwf_parameter,
    lookup_gfs_parameter,
    search_cds_variables,
    search_gfs_variables,
)
from aero.toolbox.tools.instructions import (
    clear_instructions,
    record_instruction,
    show_instructions,
)
from aero.toolbox.tools.subagents import cancel_sub_agent, launch_sub_agent, query_sub_agents
from aero.toolbox.tools.tool_rounds import get_max_tool_rounds, set_max_tool_rounds

__all__ = [
    "cancel_sub_agent",
    "check_cds_config",
    "clear_instructions",
    "clear_cds_config",
    "clear_llm_config",
    "cleanup_downloads",
    "check_email_config",
    "check_era5_availability",
    "check_gefs_availability",
    "check_gfs_availability",
    "check_ifs_availability",
    "configure_email_config",
    "configure_cds_key",
    "configure_llm_provider",
    "configure_vision_model",
    "describe_dataset",
    "describe_cds_dataset",
    "download_era5",
    "download_gefs",
    "download_gfs",
    "download_ifs",
    "download_dataset",
    "search_dataset_stations",
    "search_dataset_variables",
    "delete_file",
    "download_literature_pdf",
    "edit_file",
    "get_gefs_forecast_schedule",
    "get_gfs_forecast_schedule",
    "get_ifs_forecast_schedule",
    "get_max_tool_rounds",
    "inspect_nc",
    "inspect_csv_table",
    "inspect_gfs_inventory",
    "inspect_grib2",
    "list_downloads",
    "launch_sub_agent",
    "list_llm_providers",
    "list_figures",
    "list_files",
    "list_literature",
    "lookup_gefs_parameter",
    "lookup_ecmwf_parameter",
    "lookup_gfs_parameter",
    "propose_execution",
    "preview_image",
    "parse_isd_csv",
    "query_sub_agents",
    "query_download",
    "read_file",
    "read_pdf",
    "record_instruction",
    "save_literature",
    "search_cds_variables",
    "search_datasets",
    "search_gefs_variables",
    "search_gfs_variables",
    "search_ifs_variables",
    "search_literature",
    "send_email",
    "set_max_tool_rounds",
    "show_instructions",
    "subset_netcdf",
    "retry_download",
    "analyze_image",
    "check_vision_model_config",
    "get_vision_usage",
    "reset_vision_usage",
    "ensure_runtime_tools",
    "run_shell",
    "write_file",
    "write_plan_document",
    "_RUNTIME_TOOL_PACKAGES",
    "_runtime_tools_ready",
]
