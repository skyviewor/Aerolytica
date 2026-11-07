"""One-time, explicitly approved recovery of the cancelled 0.1.3 staging upload."""

from __future__ import annotations

import json
import os

import oss2

OLD_WHEEL = "fb2509e74469ff2b7bfbbbbfe6d2b5ed414bce00ac371844681080bceed20c57"
TARGET = "download/releases/0.1.3/"


def cleanup(bucket) -> int:
    stable = json.loads(bucket.get_object("download/stable.json").read())["manifest"]
    if stable.get("version") != "0.1.2" or (
        stable.get("runtime", {}).get("environment") != "production"
    ):
        raise RuntimeError("Production stable changed; recovery refused")
    if bucket.object_exists(TARGET + "latest.json"):
        raise RuntimeError("Version was promoted; recovery refused")
    keys = [entry.key for entry in oss2.ObjectIteratorV2(bucket, prefix=TARGET)]
    if not keys:
        print("No unpublished 0.1.3 objects remain.")
        return 0
    if any(not key.startswith(TARGET) or "/" in key[len(TARGET):] for key in keys):
        raise RuntimeError("New build paths exist; recovery refused")
    old = json.loads(bucket.get_object(TARGET + "release.json").read())["manifest"]
    if old.get("version") != "0.1.3" or old.get("wheel", {}).get("sha256") != OLD_WHEEL:
        raise RuntimeError("Staged manifest differs from the approved cancelled upload")
    # Delete the manifest last so an interrupted run can revalidate and resume.
    for key in sorted(keys, key=lambda value: value.endswith("release.json")):
        bucket.delete_object(key)
    print(f"Removed {len(keys)} approved unpublished 0.1.3 objects; stable untouched.")
    return len(keys)


def main() -> None:
    if os.environ.get("RECOVERY_CONFIRM") != "DELETE UNPUBLISHED 0.1.3":
        raise RuntimeError("Explicit recovery confirmation is required")
    if os.environ.get("OSS_PREFIX", "").strip("/") != "download":
        raise RuntimeError("Recovery is restricted to download/")
    endpoint = os.environ["OSS_ENDPOINT"]
    if not endpoint.startswith("https://"):
        raise RuntimeError("OSS endpoint must use HTTPS")
    bucket = oss2.Bucket(
        oss2.Auth(os.environ["OSS_ACCESS_KEY_ID"], os.environ["OSS_ACCESS_KEY_SECRET"]),
        endpoint, os.environ["OSS_BUCKET"], connect_timeout=20,
    )
    cleanup(bucket)


if __name__ == "__main__":
    try:
        main()
    except (oss2.exceptions.OssError, OSError):
        raise SystemExit("OSS recovery failed; no credential details are logged.") from None
