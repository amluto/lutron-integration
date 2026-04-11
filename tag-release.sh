#!/usr/bin/env bash

set -euo pipefail

cd "$(dirname "$0")"

if [[ $# -ne 1 ]]; then
  echo "Usage: ./tag-release.sh <version>" >&2
  exit 2
fi

version="${1#v}"
tag="v${version}"

./verify-release.py "$version"

if git rev-parse "$tag" >/dev/null 2>&1; then
  echo "Tag $tag already exists" >&2
  exit 1
fi

git tag -a "$tag" -m "Release $tag"

printf 'created tag %s\n' "$tag"
