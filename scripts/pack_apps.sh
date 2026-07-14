#!/usr/bin/env bash
# Pack $BASE_DIR/scalebench/apps into split parts + a SHA256SUMS manifest for a
# GitHub release. Companion producer for scripts/fetch_apps.sh -- upload every
# resulting apps.tar.part.* AND SHA256SUMS as assets on the same release.
#
# Usage (from the scalebench repo root, after `source .env`):
#   bash scripts/pack_apps.sh [OUT_DIR]        # default OUT_DIR: dist/apps
#   PART_SIZE=1800M bash scripts/pack_apps.sh  # tune part size (< 2 GiB)

set -euo pipefail

: "${BASE_DIR:?BASE_DIR is not set -- 'source .env' first (see README Step 1)}"

SRC_PARENT="$BASE_DIR/scalebench"           # holds the apps/ dir to pack
OUT="${1:-dist/apps}"
PART_SIZE="${PART_SIZE:-1800M}"             # stay under GitHub's 2 GiB/file limit

[[ -d "$SRC_PARENT/apps" ]] || { echo "error: $SRC_PARENT/apps not found" >&2; exit 1; }

mkdir -p "$OUT"
echo "==> tarring $SRC_PARENT/apps ($(du -sh "$SRC_PARENT/apps" | cut -f1)) into $PART_SIZE parts"
# APK/XAPK are already ZIP-compressed, so tar without extra compression (faster,
# no size win). Split into apps.tar.part.00, .01, ... (lexical = cat order).
tar -cf - -C "$SRC_PARENT" apps | split -b "$PART_SIZE" -d -a 2 - "$OUT/apps.tar.part."

echo "==> writing SHA256SUMS"
( cd "$OUT" && sha256sum apps.tar.part.* > SHA256SUMS )

echo "==> release assets ready in $OUT (upload all of these):"
ls -lh "$OUT"
