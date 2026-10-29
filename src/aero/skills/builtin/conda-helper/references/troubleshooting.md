# Managed Runtime Troubleshooting

## Managed Environment Is Missing

Call `ensure_runtime_tools`. It downloads Aero's Micromamba when needed and
recreates the private `aero-agent` environment. Do not search for or use a user
Conda installation.

If automatic bootstrap fails, check network access and retry:

```text
aero setup
```

Use `aero doctor` to inspect the managed Micromamba and Python paths.

## Tool Is Found Outside Aero

Executables are accepted only when they resolve inside Aero's private runtime
executable directory (`bin` on POSIX, `Scripts` on Windows). Call `ensure_runtime_tools` to install
and verify the managed copy; never reuse a binary from base or another user
environment.

## Package Is Not Mapped

Do not guess a raw package-manager command. Report the unknown command so its
mapping can be added to `package-mapping.md` and `RUNTIME_TOOL_PACKAGES`.

## Pip-only Packages

Use:

```text
python -m pip install -U <package>
```

`cnmaps` is pip-only and must never appear in a conda/mamba transaction.

## Dynamic-library or Solver Failure

Return the managed installation error. Do not repair user base, alter user
library paths, install system packages, or switch to a user Conda/Mamba.
