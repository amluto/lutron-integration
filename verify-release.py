#!/usr/bin/env python3

from __future__ import annotations

import pathlib
import subprocess
import sys
import tomllib


ROOT = pathlib.Path(__file__).resolve().parent
PACKAGE_FILES = [
    ROOT / "packages" / "lutron-integration" / "pyproject.toml",
    ROOT / "packages" / "lutron-integration-tools" / "pyproject.toml",
]


def load_pyproject(path: pathlib.Path) -> dict:
    with path.open("rb") as f:
        return tomllib.load(f)


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: ./verify-release.py <version>", file=sys.stderr)
        return 2

    requested_version = sys.argv[1]

    subprocess.run(["./check.sh"], cwd=ROOT, check=True)

    versions: dict[str, str] = {}
    for package_file in PACKAGE_FILES:
        data = load_pyproject(package_file)
        name = data["project"]["name"]
        versions[name] = data["project"]["version"]

    unique_versions = set(versions.values())
    if len(unique_versions) != 1:
        raise SystemExit(
            "Package versions do not match: "
            + ", ".join(f"{name}={version}" for name, version in sorted(versions.items()))
        )

    actual_version = unique_versions.pop()
    if actual_version != requested_version:
        raise SystemExit(
            f"Requested release version {requested_version!r} does not match package version "
            f"{actual_version!r}"
        )

    tools_dependencies = load_pyproject(PACKAGE_FILES[1])["project"]["dependencies"]
    expected_dependency = f"lutron-integration=={requested_version}"
    if expected_dependency not in tools_dependencies:
        raise SystemExit(
            "lutron-integration-tools dependency is not pinned to the release version: "
            + ", ".join(tools_dependencies)
        )

    print(f"verified release {requested_version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
