# meteora-agent Sandbox Install Pattern

## Principles

- **On demand**: Install only when a tool is missing; never pre-install
- **Unified sandbox**: All tools go into one `meteora-agent` conda environment
- **Never touch base**: If the user's base env has dependency conflicts, use an isolated env — do not repair base
- **Ask before install**: Always get explicit user consent before executing

## Step-by-Step

### 1. Check if meteora-agent exists

```bash
conda info --envs | grep meteora-agent
```

### 2. Create or append

**First time (env does not exist):**
```bash
conda create -n meteora-agent -c conda-forge <package> -y
```

**Append (env already exists):**
```bash
conda install -n meteora-agent -c conda-forge <package> -y
```

**Batch install (avoids redundant dependency resolution):**
```bash
conda install -n meteora-agent -c conda-forge nco cdo eccodes -y
```

### 3. Symlink to PATH

```bash
ln -sf ~/miniconda3/envs/meteora-agent/bin/<tool> ~/miniconda3/bin/<tool>
```

Symlink every installed tool. `~/miniconda3/bin/` is normally on PATH — tools are available immediately; no Meteora restart needed.

### 4. Verify

```bash
which <tool> && <tool> --version 2>&1 | head -1
```

### 5. Retry

After successful install, retry the failed operation.

## User Proposal Template

```
Detected missing <tool_name> (<purpose>). Need to install <package_name>
into the Meteora sandbox `meteora-agent`. This will NOT affect your
main environment. Proceed?
```

## Platform Notes

**macOS**: Prefer conda-forge. Fallback: `brew install <pkg>`.
**Linux**: Prefer conda-forge. Fallback: `apt-get install -y <pkg>`.
