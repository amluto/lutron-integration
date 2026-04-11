#!/usr/bin/env bash

set -euo pipefail

cd "$(dirname "$0")"

usage() {
  cat <<'EOF'
Usage:
  ./update-version.sh --bump <major|minor|patch|stable|alpha|beta|rc|post|dev>
  ./update-version.sh <version>
EOF
}

if [[ $# -eq 2 && $1 == "--bump" ]]; then
  uv version --package lutron-integration --frozen --bump "$2" >/dev/null
elif [[ $# -eq 1 ]]; then
  uv version --package lutron-integration --frozen "$1" >/dev/null
else
  usage >&2
  exit 2
fi

version="$(uv version --package lutron-integration --short)"

uv version --package lutron-integration-tools --frozen "$version" >/dev/null
uv add --package lutron-integration-tools --frozen "lutron-integration==$version" >/dev/null

uv lock >/dev/null

printf '%s\n' "$version"
