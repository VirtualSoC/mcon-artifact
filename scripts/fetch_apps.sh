#!/usr/bin/env bash
# Download the top-50 app corpus into $BASE_DIR/scalebench/apps.
#
# The corpus (~6.2 GB of APK/XAPK files) is hosted as a SPLIT tar archive on a
# GitHub release (GitHub caps release assets at 2 GB/file, so it ships as several
# parts + a SHA256SUMS manifest). This script downloads every part listed in the
# manifest, verifies its SHA-256, reassembles the tar, and extracts it.
#
# Produce the assets this script consumes with the companion scripts/pack_apps.sh.
#
# Usage (from the scalebench repo root, after `source .env`):
#   bash scripts/fetch_apps.sh
# Override with MCON_APPS_URL=<url> to fetch the corpus from a different release.

set -euo pipefail

# Release directory that holds SHA256SUMS and the apps.tar.part.* assets.
RELEASE_URL="${MCON_APPS_URL:-https://github.com/VirtualSoC/mcon-docs/releases/download/binary-release}"

: "${BASE_DIR:?BASE_DIR is not set -- 'source .env' first (see README Step 1)}"

if [[ "$RELEASE_URL" == *"<"* ]]; then
  echo "error: set MCON_APPS_URL (or edit RELEASE_URL in this script) to your release URL" >&2
  exit 1
fi
command -v curl >/dev/null || { echo "error: curl is required" >&2; exit 1; }

DEST_PARENT="$BASE_DIR/scalebench"          # the tar contains a top-level apps/ dir
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "==> fetching manifest from $RELEASE_URL"
curl -fL --retry 3 "$RELEASE_URL/SHA256SUMS" -o "$WORK/SHA256SUMS"

echo "==> downloading parts"
while read -r _sha name; do
  [[ -n "${name:-}" ]] || continue
  echo "    $name"
  curl -fL --retry 3 -C - "$RELEASE_URL/$name" -o "$WORK/$name"
done < "$WORK/SHA256SUMS"

echo "==> verifying checksums"
( cd "$WORK" && sha256sum -c SHA256SUMS )

echo "==> reassembling and extracting into $DEST_PARENT/apps"
mkdir -p "$DEST_PARENT"
cat "$WORK"/apps.tar.part.* > "$WORK/apps.tar"
tar -xf "$WORK/apps.tar" -C "$DEST_PARENT"

echo "==> done: $(ls "$DEST_PARENT/apps" | wc -l) files in $DEST_PARENT/apps"
