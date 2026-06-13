# Common conda Issues & Workarounds

## RemoveError: 'platformdirs' is a dependency of conda

**Symptom**: Every `conda install` fails with this error, even for unrelated packages.

**Cause**: Base environment dependency consistency is broken; conda transaction verification fails.

**Fix**: Do NOT repair base. Use an isolated environment instead:

```bash
conda create -n aero-agent -c conda-forge python=3.12 -y
```

Isolated environments are immune to base environment issues.

## dyld: Library not loaded

**Symptom**: After symlinking a tool, running it fails with:
```
dyld: Library not loaded: @rpath/libxxx.dylib
```

**Cause**: Dynamic library not linked into the system library path.

**Fix**:
```bash
# 1. Find the missing library
find ~/miniconda3/pkgs -name "libxxx.dylib" -not -path "*/envs/*" 2>/dev/null

# 2. Symlink it
ln -sf <found_path> ~/miniconda3/lib/libxxx.dylib

# 3. If still failing, set DYLD_LIBRARY_PATH
export DYLD_LIBRARY_PATH=~/miniconda3/envs/aero-agent/lib:$DYLD_LIBRARY_PATH
```

## Slow conda solve (environment solving hangs)

**Fix**: Use mamba instead (much faster dependency resolution):

```bash
conda create -n aero-agent -c conda-forge python=3.12 -y
conda install -n aero-agent -c conda-forge mamba -y
~/miniconda3/envs/aero-agent/bin/mamba install -p ~/miniconda3/envs/aero-agent -c conda-forge <package> -y
```

## pip-only packages

Some tools are only available via pip. They can also be installed into the aero-agent sandbox:

```bash
~/miniconda3/envs/aero-agent/bin/python -m pip install -U <package>
```

`cnmaps` must be installed this way. Do not use conda or mamba for `cnmaps`.

Then symlink the binary:
```bash
ln -sf ~/miniconda3/envs/aero-agent/bin/<tool> ~/miniconda3/bin/<tool>
```

## Package not found on conda-forge

Try `conda-forge` first. If not available, try:
- `defaults` channel: `conda install -n aero-agent <package> -y`
- `pip`: `~/miniconda3/envs/aero-agent/bin/python -m pip install -U <package>`
- System package manager: `brew install <package>` (macOS) or `apt-get install -y <package>` (Linux)
