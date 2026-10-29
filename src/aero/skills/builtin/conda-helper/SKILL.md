---
name: conda-helper
description: Use when a managed CLI tool is missing. Installs on demand into Aero's private Micromamba runtime and never touches user Conda environments.
---

# Managed Runtime Helper

## Runtime Ownership

`uv` installs the Aero CLI. `aero setup` and `ensure_runtime_tools` manage a
separate scientific runtime:

- Micromamba and Python paths are platform-specific; use `aero doctor` to inspect them.
- The fixed scientific environment is named `aero-agent` on every platform.

The user's Conda, Mamba, Miniconda, Anaconda, base environment, and named
environments are outside Aero's control.

## Required Flow

1. When a supported native command is missing,
   call `ensure_runtime_tools` with the command names.
2. Ask for installation consent through that tool's confirmation.
3. Let the tool download Aero's managed Micromamba if necessary, recreate the
   fixed `aero-agent` prefix, install mapped conda-forge packages, and verify
   that binaries resolve inside the managed environment.
4. Retry the original operation after success.

Do not manually create environments, install Mamba, activate an environment,
or create PATH symlinks. Aero prepends its private executable directory when
executing commands. On Windows, CDO/NCO are not assumed available; use
`transform_scientific_data` for supported GRIB/NetCDF operations and
`run_python_script` for task-specific scripts. Never suggest WSL or Cygwin.

## Pip-only Packages

Install pip-only Python packages with the managed interpreter:

```text
python -m pip install -U <package>
```

`cnmaps` is always pip-only:

```text
python -m pip install -U cnmaps
```

Never put `cnmaps` or `cnmaps-data` in a conda/mamba transaction.

## Hard Rules

1. Install on demand for CLI use; the desktop first-run setup may prepare its private runtime.
2. Never run `conda create`, `conda install`, or `conda activate`.
3. Never use a user-provided `mamba` or `micromamba`.
4. Never reference `~/miniconda3`, `~/anaconda3`, or a base environment.
5. A missing managed environment is rebuilt by `ensure_runtime_tools`; it is
   not a reason to fall back to user software.
6. Use one fixed `aero-agent` runtime for all managed tools.

See `references/package-mapping.md`, `references/sandbox-pattern.md`, and
`references/troubleshooting.md`.
