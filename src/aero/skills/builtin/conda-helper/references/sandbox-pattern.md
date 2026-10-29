# Aero Managed Runtime Pattern

## Ownership

- Aero CLI installation: `uv tool`
- Package manager and interpreter live in Aero's platform-specific private runtime.
- Fixed environment: `aero-agent`; inspect actual paths with `aero doctor`.

No user Conda installation participates in this flow.

## Tool Installation

For mapped CLI tools, call:

```text
ensure_runtime_tools({"tools": ["ncks", "cdo"]})
```

The tool requests confirmation, bootstraps managed Micromamba when absent,
recreates the fixed environment when absent, installs packages from
conda-forge, and verifies each executable.

For pip-only libraries:

```text
python -m pip install -U <package>
```

Use that command for `cnmaps`. Never install it with conda or mamba.

## Prohibited Fallbacks

Do not invoke user `conda`, `mamba`, or `micromamba`; do not activate base; do
not inspect or modify user environments; and do not symlink managed binaries
into user paths. If managed bootstrap fails, report the error and suggest
retrying `aero setup`—never switch package managers.
