#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if ! command -v uv >/dev/null 2>&1; then
    printf '%s\n' \
        "setup.sh: uv is required." \
        "Install uv 0.11.32 from https://docs.astral.sh/uv/ and retry." >&2
    exit 1
fi

exec "$REPO_ROOT/scripts/check" "$@"
