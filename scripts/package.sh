#!/usr/bin/env bash
# Produce out/"WoW Addon Updater.zip" in Decky's expected layout.
set -euo pipefail
cd "$(dirname "$0")/.."

NAME="WoW Addon Updater"

pnpm install --frozen-lockfile
pnpm run build

rm -rf out
mkdir -p "out/$NAME"
git rev-parse --short=7 HEAD > "out/$NAME/BUILD" 2>/dev/null || echo unknown > "out/$NAME/BUILD"

cp -r dist py_modules "out/$NAME/"
cp main.py plugin.json package.json LICENSE VERSION "out/$NAME/"

find "out/$NAME" -name __pycache__ -type d -prune -exec rm -rf {} +
(cd out && zip -r "$NAME.zip" "$NAME" >/dev/null)
ls -la out
