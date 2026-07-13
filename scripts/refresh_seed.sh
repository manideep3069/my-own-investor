#!/bin/bash
# Weekly seed-database refresh for the my-own-investor dashboard.
#
# Pulls the fork, re-collects all data sources starting from the current seed
# (so slow-moving history like Polymarket series accumulates), rebuilds weekly
# features, and pushes the updated data-seed/moi.duckdb. The deployed dashboard
# (https://my-own-investor.streamlit.app) swaps the new seed in automatically.
#
# Run from anywhere: scripts/refresh_seed.sh
set -euo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO"

echo "== $(date) — refreshing seed database in $REPO"

git fetch origin
git checkout -q main
git reset -q --hard origin/main

VENV="$REPO/.venv"
if [ ! -x "$VENV/bin/python" ]; then
  uv venv --python 3.12 "$VENV"
fi
VIRTUAL_ENV="$VENV" uv pip install -q -r requirements.txt

export MOI_EDGAR_IDENTITY="${MOI_EDGAR_IDENTITY:-Maruthi Manideep Gorla maruthimanideepgorla@gmail.com}"

mkdir -p data
cp data-seed/moi.duckdb data/moi.duckdb
rm -f data/.seed-version   # this copy is about to become the new canonical seed
"$VENV/bin/moi" db init
"$VENV/bin/moi" collect all
"$VENV/bin/moi" features build
cp data/moi.duckdb data-seed/moi.duckdb

if git diff --quiet -- data-seed/moi.duckdb; then
  echo "Seed unchanged — nothing to push."
  exit 0
fi

git add data-seed/moi.duckdb
git -c user.name="Maruthi Manideep Gorla" \
    -c user.email="maruthimanideepgorla@gmail.com" \
    commit -q -m "chore: weekly seed database refresh"
git push -q origin main
echo "== Seed refreshed and pushed."
