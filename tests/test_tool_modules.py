"""Compatibility checks for domain-focused built-in tool modules."""

from aero.toolbox import builtin_tools
from aero.toolbox.registry import get_registry

MIGRATED_TOOLS = (
    "launch_sub_agent",
    "query_sub_agents",
    "cancel_sub_agent",
    "search_datasets",
    "search_dataset_variables",
    "search_dataset_stations",
    "describe_dataset",
    "download_dataset",
    "parse_isd_csv",
    "inspect_csv_table",
    "record_instruction",
    "show_instructions",
    "clear_instructions",
    "search_literature",
    "save_literature",
    "download_literature_pdf",
    "list_literature",
    "write_plan_document",
    "propose_execution",
    "configure_email_config",
    "check_email_config",
    "send_email",
    "inspect_nc",
    "subset_netcdf",
    "list_files",
    "list_figures",
    "delete_file",
    "read_file",
    "write_file",
    "edit_file",
    "read_pdf",
    "preview_image",
    "ensure_runtime_tools",
    "run_shell",
    "list_downloads",
    "query_download",
    "retry_download",
    "cleanup_downloads",
    "check_cds_config",
    "configure_cds_key",
    "list_llm_providers",
    "configure_llm_provider",
    "clear_llm_config",
    "clear_cds_config",
    "check_vision_model_config",
    "analyze_image",
    "configure_vision_model",
)


def test_migrated_tools_remain_available_from_compatibility_module():
    for name in MIGRATED_TOOLS:
        assert hasattr(builtin_tools, name)


def test_migrated_tools_are_registered():
    registry = get_registry()
    for name in MIGRATED_TOOLS:
        assert registry.get(name) is not None
