#!/bin/bash
# Camera attendance: get the latest version and install it. Double-click this file in Finder.
# Works when this folder was downloaded with git; otherwise download the new version and run
# its Install.command (settings, records and photos are kept either way).
set -euo pipefail
SRC="$(cd "$(dirname "$0")" && pwd)"
if git -C "$SRC" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  echo "Getting the latest version..."
  git -C "$SRC" pull --ff-only
  exec bash "$SRC/Install.command"
fi
echo "This folder was not downloaded with git, so it cannot update itself."
echo "Download the new version, unzip it, and double-click Install.command in it."
read -r -p "Press Enter to close this window." _ || true
