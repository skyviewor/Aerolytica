"""A rerun may skip existing files only if PyPI has the exact same bytes."""

from __future__ import annotations

import hashlib
import json
import sys
import tomllib
import urllib.error
import urllib.request
from pathlib import Path


def main(directory: Path):
    version = tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"]
    try:
        with urllib.request.urlopen(
            f"https://pypi.org/pypi/aerolytica/{version}/json", timeout=20
        ) as response:
            data = json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return
        raise
    remote = {file["filename"]: file["digests"]["sha256"] for file in data["urls"]}
    for path in directory.iterdir():
        if path.name in remote:
            with path.open("rb") as handle:
                if hashlib.file_digest(handle, "sha256").hexdigest() != remote[path.name]:
                    raise SystemExit("PyPI already contains different bytes: " + path.name)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
