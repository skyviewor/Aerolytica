#!/usr/bin/env sh
set -eu

# Compatibility entry point. Builds signed immutable assets; never uploads.
RELEASE_ROOT="$(CDPATH= cd "$(dirname "$0")/../.." && pwd)"
cd "$RELEASE_ROOT"
command -v uv >/dev/null 2>&1 || {
    printf 'uv is required to build Aerolytica distributions.\n' >&2
    exit 1
}
RELEASE_TAG="${RELEASE_TAG:-$(uv run --locked python -c 'import tomllib; print("v" + tomllib.load(open("pyproject.toml", "rb"))["project"]["version"])')}"
uv run --locked python scripts/release/prepare.py --tag "$RELEASE_TAG" "$@"
printf 'Signed assets are in dist/release/. No files have been published.\n'
