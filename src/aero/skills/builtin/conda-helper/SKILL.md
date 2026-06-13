---
name: conda-helper
description: Use when executing shell commands that fail because a CLI tool is missing, or when a download/data-processing tool returns a "tool_missing" error. Covers installing missing tools into the unified aero-agent conda sandbox on demand — never pre-install.
---

# conda-helper

## Core Philosophy

**Install on demand, never pre-install.** Aero starts with nothing. Only when a shell command fails (`command not found`) or a tool returns `tool_missing`, detect, propose, install, and retry.

## aero-agent Unified Sandbox

All Aero runtime CLI tools (NCO, CDO, eccodes, GDAL, netcdf toolchain, etc.) go into **one** `aero-agent` conda environment. Not one env per tool.

Benefits:
- Does not pollute the user's base environment
- Immune to base environment dependency conflicts (isolated env bypasses them)
- Shared library dependencies across tools — no duplicate installs

## Trigger Scenarios

Consult this skill when:

1. `shell_command` returns `command not found: ncks` (or ncrcat, ncap2, ncatted, cdo, gdal_translate, etc.)
2. A download or data-processing tool result contains `"tool_missing": true`
3. Error message mentions "NCO CLI tools not installed", "command not found", etc.
4. Any scenario requiring conda-based runtime dependency installation

## Install Flow

### Step 1: Identify the package name

Infer the conda package name from the missing command or error message:

| Missing Command | conda Package | Channel |
|----------------|---------------|---------|
| ncks / ncrcat / ncap2 / ncatted | nco | conda-forge |
| cdo | cdo | conda-forge |
| eccodes / grib_ls / grib_dump | eccodes | conda-forge |
| gdal_translate / gdalwarp | gdal | conda-forge |
| ncdump / ncgen | libnetcdf | conda-forge |

If the command is not in the table, reverse-lookup with:
```
conda search <command_name> -c conda-forge
```

See `references/package-mapping.md` for the full mapping.

### Step 2: Check or create aero-agent

Use base only for the minimum conda environment-management commands. Do not
install packages into base, including mamba.

Check whether the sandbox exists:

```
conda info --envs | grep aero-agent
```

If it does not exist, create it with base conda:

```
conda create -n aero-agent -c conda-forge python=3.12 -y
```

This is the only kind of base-environment operation allowed: creating or
updating the `aero-agent` environment by name. Never install runtime packages
into base.

### Step 3: Install mamba inside aero-agent

Use `mamba` for runtime package installs because it resolves conda-forge
dependencies faster than conda. But mamba itself must live in `aero-agent`,
not in base.

If `~/miniconda3/envs/aero-agent/bin/mamba` is missing:

```
conda install -n aero-agent -c conda-forge mamba -y
```

If this fails, fall back to plain `conda install -n aero-agent ...`; do not
install mamba into base.

### Step 4: Propose to the user and get consent

**Must get explicit user consent before installing.** Environment changes are irreversible.

Proposal template:
```
Detected missing <tool_name> (<purpose>). Need to install <package_name>
into the Aero sandbox `aero-agent`. This will NOT affect your
main environment. Proceed?
```

### Step 5: Install

**First time (aero-agent does not exist):**
```
conda create -n aero-agent -c conda-forge python=3.12 -y
```

**Append (aero-agent already exists):**
```
~/miniconda3/envs/aero-agent/bin/mamba install -p ~/miniconda3/envs/aero-agent -c conda-forge <package> -y
```

**pip-only Python packages:**
```
~/miniconda3/envs/aero-agent/bin/python -m pip install -U <package>
```

`cnmaps` is pip-only for Aero. Never install `cnmaps` with conda or mamba,
and never include `cnmaps` in a conda/mamba package list. Use:

```
~/miniconda3/envs/aero-agent/bin/python -m pip install -U cnmaps
```

**Multiple packages at once (avoids redundant dependency resolution):**
```
~/miniconda3/envs/aero-agent/bin/mamba install -p ~/miniconda3/envs/aero-agent -c conda-forge nco cdo -y
```

**Fallback if mamba is unavailable inside aero-agent:**
```
conda install -n aero-agent -c conda-forge <package> -y
```

### Step 6: Symlink into PATH

```
ln -sf ~/miniconda3/envs/aero-agent/bin/<tool> ~/miniconda3/bin/<tool>
```

Symlink every installed tool. `~/miniconda3/bin/` is normally on PATH.

### Step 7: Verify and retry

```
which <tool> && <tool> --version 2>&1 | head -1
```

On success, retry the original failed operation.

## Hard Rules

1. **Never pre-install** — install only when a tool is actually missing
2. **Never install into base** — do not install mamba or runtime packages into base
3. **Ask permission first** — always ask the user; wait for explicit consent
4. **One sandbox** — all tools go into `aero-agent`; do not create per-tool environments
5. **Prefer env-local mamba** — install and use mamba only inside `aero-agent`
6. **Symlink to PATH** — `~/miniconda3/bin/` is on PATH; symlinks take effect immediately

## Common Pitfalls

See `references/troubleshooting.md` for:
- `RemoveError: platformdirs` — base env conflict, use isolated env
- `dyld: Library not loaded` — missing dynamic library, symlink or set DYLD_LIBRARY_PATH
- Slow conda solve — install and use mamba instead
- pip-only packages — install via `~/miniconda3/envs/aero-agent/bin/python -m pip install -U <pkg>`
- `cnmaps` specifically is pip-only — never install it with conda or mamba

## Platform Notes

**macOS**: Prefer conda-forge. Fallback: `brew install <pkg>`.
**Linux**: Prefer conda-forge. Fallback: `apt-get install -y <pkg>`.
