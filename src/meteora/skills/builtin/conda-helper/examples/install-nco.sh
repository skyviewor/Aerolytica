#!/bin/bash
# Install NCO into the meteora-agent sandbox on demand
# Usage: bash install-nco.sh
set -e

echo "=== Checking meteora-agent environment ==="
if conda info --envs 2>/dev/null | grep -q meteora-agent; then
    echo "meteora-agent exists, appending NCO..."
    conda install -n meteora-agent -c conda-forge nco -y
else
    echo "Creating meteora-agent and installing NCO..."
    conda create -n meteora-agent -c conda-forge nco -y
fi

echo ""
echo "=== Symlinking NCO tools to PATH ==="
CONDA_PREFIX="${CONDA_PREFIX:-$HOME/miniconda3}"
for tool in ncks ncrcat ncap2 ncatted ncra ncea ncpdq ncwa ncdiff; do
    src="$CONDA_PREFIX/envs/meteora-agent/bin/$tool"
    dest="$CONDA_PREFIX/bin/$tool"
    if [ -f "$src" ]; then
        ln -sf "$src" "$dest"
        echo "  $tool -> $dest"
    fi
done

echo ""
echo "=== Verify ==="
ncks --version 2>&1 | head -1
echo "NCO installation complete"
