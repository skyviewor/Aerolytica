"""Scoped OSS upload; never prints credentials or writes a credential file."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path

import oss2
from packaging.version import Version
from prepare import distribution_module


def digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def remote_digest(bucket, key: str) -> str:
    response = bucket.get_object(key)
    checksum = hashlib.sha256()
    try:
        while chunk := response.read(1024 * 1024):
            checksum.update(chunk)
        return checksum.hexdigest()
    finally:
        close = getattr(response, "close", None)
        if close:
            close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("stage", "promote"))
    parser.add_argument("--directory", type=Path, default=Path("dist/release/download"))
    args = parser.parse_args()
    names = ("OSS_ACCESS_KEY_ID", "OSS_ACCESS_KEY_SECRET", "OSS_BUCKET", "OSS_ENDPOINT")
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        raise SystemExit("Missing configuration: " + ", ".join(missing))
    endpoint = os.environ["OSS_ENDPOINT"]
    if not endpoint.startswith("https://"):
        raise SystemExit("OSS_ENDPOINT must use HTTPS")
    prefix = os.environ.get("OSS_PREFIX", "download").strip("/")
    if not prefix or ".." in prefix.split("/"):
        raise SystemExit("OSS_PREFIX must be a non-root safe object prefix")
    if os.environ.get("AERO_ENVIRONMENT") == "testing" and prefix == "download":
        raise SystemExit("Testing must use a separate prefix, for example download/testing")
    bucket = oss2.Bucket(
        oss2.Auth(os.environ[names[0]], os.environ[names[1]]),
        endpoint,
        os.environ["OSS_BUCKET"],
        connect_timeout=20,
    )
    directory = args.directory.resolve()
    release = json.loads((directory / "stable.json").read_text())
    module = distribution_module()
    module.validate_manifest(release["manifest"])
    module.assert_release_environment(
        release["manifest"], os.environ.get("AERO_ENVIRONMENT", "production")
    )
    profile = release["manifest"].get("runtime")
    if profile and os.environ.get("AERO_DOWNLOAD_BASE", "").rstrip("/") != profile["download_base"]:
        raise SystemExit("OSS download mapping does not match the signed release configuration")
    version = release["manifest"]["version"]
    files = [path for path in directory.rglob("*") if path.is_file()]
    if args.action == "stage":
        for path in sorted(files):
            relative = path.relative_to(directory).as_posix()
            if relative in {"install.sh", "stable.json"}:
                continue
            key = f"{prefix}/{relative}"
            value = digest(path)
            if bucket.object_exists(key):
                if remote_digest(bucket, key) != value:
                    raise RuntimeError("Immutable OSS artifact differs: " + relative)
                continue
            bucket.put_object_from_file(
                key,
                str(path),
                headers={
                    "Cache-Control": "public,max-age=31536000,immutable",
                    "x-oss-meta-sha256": value,
                    "Content-Type": "application/octet-stream",
                },
            )
            if remote_digest(bucket, key) != value:
                raise RuntimeError("OSS upload verification failed: " + relative)
        print("Immutable release artifacts uploaded and verified.")
    else:
        key = f"{prefix}/stable.json"
        if bucket.object_exists(key):
            previous = json.loads(bucket.get_object(key).read())
            module.assert_release_environment(
                previous["manifest"], os.environ.get("AERO_ENVIRONMENT", "production")
            )
            if Version(previous["manifest"]["version"]) > Version(version):
                print("Older release retained; stable entry was not downgraded.")
                return
        for name in ("install.sh", "stable.json"):
            bucket.put_object_from_file(
                f"{prefix}/{name}",
                str(directory / name),
                headers={
                    "Cache-Control": "no-cache,no-store,must-revalidate",
                    "Content-Type": "text/plain;charset=utf-8"
                    if name.endswith(".sh")
                    else "application/json",
                },
            )
        print("Stable entry promoted to " + version)


if __name__ == "__main__":
    try:
        main()
    except (oss2.exceptions.OssError, OSError):
        # OSS SDK exceptions can carry signed request information. Do not log them.
        raise SystemExit("OSS request failed; verify permissions, endpoint and network.") from None
