#!/bin/sh
# Regenerate requirements.lock (runtime deps pinned by hash) for the Docker image.
#
# pip-compile resolves straight from PyPI, bypassing safe-chain, so the lock is
# constrained to exactly the versions safe-chain already scanned and installed
# into .venv (`safe-chain pip install -e '.[dev]'`). Only the hashes come from
# PyPI.
#
# Pass --upgrade to first upgrade the venv to the latest releases (still through
# safe-chain) before locking.
set -eu
upgrade=
case "${1:-}" in
    --upgrade) upgrade="-U --upgrade-strategy eager" ;;
    "") ;;
    *) echo "usage: $0 [--upgrade]" >&2; exit 2 ;;
esac
cd "$(dirname "$0")/.."
[ -d .venv ] || python3 -m venv .venv
PATH="$PWD/.venv/bin:$PATH" safe-chain pip install -q $upgrade -e '.[dev]' pip-tools
constraints=$(mktemp)
trap 'rm -f "$constraints"' EXIT
.venv/bin/pip freeze --exclude-editable > "$constraints"
.venv/bin/pip-compile -q --generate-hashes --strip-extras --no-upgrade \
    --constraint "$constraints" --output-file requirements.lock pyproject.toml
echo "wrote requirements.lock"
