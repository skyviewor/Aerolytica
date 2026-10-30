"""Check the website mapping before making a release the default."""

import os
import sys
import tempfile
from pathlib import Path

from prepare import distribution_module, file_digest

module = distribution_module()
directory = Path(sys.argv[1]).resolve()
base = os.environ.get("AERO_DOWNLOAD_BASE", module.DOWNLOAD_BASE)

with tempfile.TemporaryDirectory(prefix="aero-public-check-") as temporary:
    for file in directory.rglob("*"):
        if file.is_file() and file.relative_to(directory).as_posix() not in {
            "stable.json",
            "install.sh",
        }:
            print(
                "Verifying public artifact: " + file.relative_to(directory).as_posix(), flush=True
            )
            module.download(
                module.artifact_url(base, file.relative_to(directory).as_posix()),
                Path(temporary) / "check",
                sha256=file_digest(file),
            )
print("Public release links verified.")
