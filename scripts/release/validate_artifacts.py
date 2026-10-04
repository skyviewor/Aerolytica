"""Fail release identity/resource checks before publishing anything."""

import argparse
import email
import tarfile
import tomllib
import zipfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path, nargs="?")
    parser.add_argument("--tag")
    args = parser.parse_args()
    project = tomllib.loads(Path("pyproject.toml").read_text())["project"]
    if project["name"] != "aerolytica":
        raise SystemExit("The distribution must be named aerolytica")
    if args.tag is not None and args.tag != "v" + project["version"]:
        raise SystemExit("Release tag does not match project version")
    if args.directory:
        wheels = list(args.directory.glob("*.whl"))
        sdists = list(args.directory.glob("*.tar.gz"))
        if len(wheels) != 1 or len(sdists) != 1:
            raise SystemExit("Expected exactly one wheel and one sdist")
        with zipfile.ZipFile(wheels[0]) as wheel:
            names = wheel.namelist()
            metadata = email.message_from_bytes(
                wheel.read(next(name for name in names if name.endswith(".dist-info/METADATA")))
            )
            if metadata["Name"] != project["name"] or metadata["Version"] != project["version"]:
                raise SystemExit("Wheel identity mismatch")
            for required in (
                "aero/cli/main.py",
                "aero/cli/entrypoint.py",
                "aero/release_keys.json",
                "aero/server/static/index.html",
                "aero/cli/environment.yaml",
            ):
                if required not in names:
                    raise SystemExit("Missing wheel resource: " + required)
            if not any(name.startswith("aero/skills/builtin/") for name in names):
                raise SystemExit("Built-in skills are missing")
            if not wheels[0].name.endswith("-py3-none-any.whl"):
                raise SystemExit("The client wheel must be platform-independent")
        with tarfile.open(sdists[0]) as source:
            metadata_file = next(
                item for item in source.getmembers() if item.name.endswith("/PKG-INFO")
            )
            metadata = email.message_from_bytes(source.extractfile(metadata_file).read())
            if metadata["Name"] != project["name"] or metadata["Version"] != project["version"]:
                raise SystemExit("Source distribution identity mismatch")
            for required in (
                "src/aero/cli/main.py",
                "src/aero/release_keys.json",
                "scripts/release/prepare.py",
                "webui/package-lock.json",
            ):
                if not any(name.endswith("/" + required) for name in source.getnames()):
                    raise SystemExit("Missing source distribution resource: " + required)
    print("Release identity and resources verified.")


if __name__ == "__main__":
    main()
