#!/usr/bin/env bash
# Download the prebuilt Redroid outer-VM image into
# $BASE_DIR/img/redroid/redroid.qcow2 and verify its SHA-256.
#
# The image is a MINIMAL Ubuntu + Docker + redroid + binder_linux VM built by
# scripts/build_redroid_image.sh. It ships as a single qcow2 asset with a
# .sha256 sidecar on the same GitHub release.
#
# Usage (from the scalebench repo root, after `source .env`):
#   bash scripts/fetch_redroid_image.sh
# Override the source URL with:
#   MCON_REDROID_URL=<direct-url-to-redroid.qcow2> bash scripts/fetch_redroid_image.sh

set -euo pipefail

# Direct URL to redroid.qcow2. The matching .sha256 sidecar is fetched from the
# same location with a .sha256 suffix appended.
IMG_URL="${MCON_REDROID_URL:-https://github.com/VirtualSoC/mcon-artifact/releases/download/prebuilt-redroid-vm/redroid.qcow2}"

: "${BASE_DIR:?BASE_DIR is not set -- 'source .env' first (see README Step 1)}"
command -v curl >/dev/null || { echo "error: curl is required" >&2; exit 1; }
command -v sha256sum >/dev/null || { echo "error: sha256sum is required" >&2; exit 1; }

DEST="$BASE_DIR/img/redroid/redroid.qcow2"
mkdir -p "$(dirname "$DEST")"

echo "==> fetching $IMG_URL"
curl -fL --retry 3 -C - "$IMG_URL" -o "$DEST"

echo "==> fetching checksum ${IMG_URL}.sha256"
curl -fL --retry 3 "${IMG_URL}.sha256" -o "${DEST}.sha256"

echo "==> verifying checksum"
( cd "$(dirname "$DEST")" && sha256sum -c "$(basename "$DEST").sha256" )

echo "==> done: $DEST ($(du -h "$DEST" | cut -f1))"
echo "    redroid.sh uses this path by default (REDROID_IMG_PATH to override)."
