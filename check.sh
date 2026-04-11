#!/usr/bin/env bash

set -u

cd "$(dirname "$0")"

status=0

uv run ty check . || status=1
uv run pytest || status=1

exit "$status"
