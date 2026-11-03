#!/usr/bin/env bash
# Builds the RojoControl fork: stock Rojo v7.7.1 plus rojo-control.patch (external control, paused/live, sync once).
# Usage: rojo-plugin/build.sh [output.rbxm]   (default: target/RspRojo.rbxm)
set -euo pipefail

ROJO_TAG="v7.7.1"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(dirname "$HERE")"
OUT="${1:-$REPO/target/RspRojo.rbxm}"
SRC="$REPO/target/rojo-plugin-src"

rm -rf "$SRC"
mkdir -p "$(dirname "$OUT")"
git -c core.autocrlf=false clone --quiet --depth 1 --branch "$ROJO_TAG" https://github.com/rojo-rbx/rojo.git "$SRC"
git -C "$SRC" -c core.autocrlf=false submodule update --init --depth 1 plugin/Packages
cp "$HERE/RojoControl.lua" "$SRC/plugin/src/RojoControl.lua"
git -C "$SRC" apply "$HERE/rojo-control.patch"
(cd "$HERE" && rojo build "$SRC/plugin.project.json" --output "$OUT")
echo "built $OUT"
