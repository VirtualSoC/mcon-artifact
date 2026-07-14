#!/usr/bin/env bash
# Build a MINIMAL Redroid outer-VM qcow2 from scratch (author side).
#
# Output (default $BASE_DIR/img/redroid/redroid.qcow2): a small Ubuntu guest with
# Docker, the binder_linux module, an SSH `redroid` user, the init.sh control
# script, and a baked redroid/redroid:13.0.0 image that has libndk arm64
# translation (github.com/zhouziyang/libndk_translation v0.2.3, supports
# Android 13). Reviewers download the shipped result with fetch_redroid_image.sh.
#
# Host requirements: docker, guestfs-tools (virt-customize + virt-sparsify),
# qemu-img, curl, git, gzip. Run as root so docker + libguestfs work:
#
#     source .env && sudo -E bash scripts/build_redroid_image.sh
#
# Tunables (env): ANDROID_VER, DISK_SIZE (final virtual, thin), BUILD_SIZE, OUT, UBUNTU_URL, REDROID_PASS.
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "run as root so docker + virt-customize work:  source .env && sudo -E bash $0" >&2
  exit 1
fi
: "${BASE_DIR:?BASE_DIR is not set -- 'source .env' first (use sudo -E to keep it)}"

ANDROID_VER="${ANDROID_VER:-13.0.0}"
BASE_IMAGE="redroid/redroid:${ANDROID_VER}-latest"
NDK_IMAGE="redroid/redroid:${ANDROID_VER}-ndk"
LIBNDK_REPO="https://github.com/zhouziyang/libndk_translation"
UBUNTU_URL="${UBUNTU_URL:-https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img}"
OUT="${OUT:-$BASE_DIR/img/redroid/redroid.qcow2}"
# Final *virtual* disk size of the shipped image. qcow2 is thin and we compress
# it, so this costs almost nothing on disk; the guest root fs grows to fill it on
# first boot (cloud-init growpart), giving capable hosts room for many
# containers. Real usage only grows as containers write.
DISK_SIZE="${DISK_SIZE:-2T}"
# Filesystem size DURING the build -- just big enough for the OS + Docker + the
# baked redroid image. Kept small so ext4 metadata stays tiny; the real
# expansion to DISK_SIZE happens on the reviewer's first boot.
BUILD_SIZE="${BUILD_SIZE:-40G}"
ROOT_PART="${ROOT_PART:-/dev/sda1}"          # root partition in the Ubuntu cloud image
REDROID_PASS="${REDROID_PASS:-redroid}"     # image login password -- CHANGE/rotate before shipping
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
GUEST_SRC="$HERE/../platform/redroid-guest"

for t in docker virt-customize virt-sparsify virt-resize qemu-img curl git gzip; do
  command -v "$t" >/dev/null || { echo "error: missing tool: $t" >&2; exit 1; }
done

echo "== build_redroid_image.sh :: redroid ${ANDROID_VER} + libndk, disk ${DISK_SIZE} (build fs ${BUILD_SIZE}) =="

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

echo "== [1/6] build ${NDK_IMAGE} with libndk arm64 translation =="
docker pull "$BASE_IMAGE"
git clone --depth 1 "$LIBNDK_REPO" "$WORK/libndk"
# Per-version tars: 11/12 are real files; 13/14 are symlinks to the shared 0.2.3
# payload. Resolve the requested version; fall back to the 12.0.0 tar (which the
# repo documents as the Android 13/14 payload).
LIBNDK_TAR="$WORK/libndk/libndk_translation-${ANDROID_VER}.tar"
LIBNDK_TAR="$(readlink -f "$LIBNDK_TAR" 2>/dev/null || echo "$LIBNDK_TAR")"
[[ -f "$LIBNDK_TAR" ]] || LIBNDK_TAR="$WORK/libndk/libndk_translation-12.0.0.tar"
if [[ ! -f "$LIBNDK_TAR" ]]; then
  echo "error: no libndk tar found in the clone. contents:" >&2
  ls -la "$WORK/libndk" >&2
  exit 1
fi
echo "   using libndk payload: $(basename "$LIBNDK_TAR")"
mkdir -p "$WORK/ctx"
cp "$LIBNDK_TAR" "$WORK/ctx/libndk.tar"
cat > "$WORK/ctx/Dockerfile" <<EOF
FROM ${BASE_IMAGE}
ADD libndk.tar /
EOF
docker build -t "$NDK_IMAGE" "$WORK/ctx"
docker save "$NDK_IMAGE" | gzip -1 > "$WORK/redroid-ndk.tar.gz"
echo "   baked image size: $(du -h "$WORK/redroid-ndk.tar.gz" | cut -f1)"

echo "== [2/6] fetch Ubuntu cloud image + expand root fs for the build =="
# Cache the cloud image so a re-run does not re-download ~600 MB.
CACHE_DIR="$BASE_DIR/img/redroid/.cache"
mkdir -p "$CACHE_DIR"
CACHE_IMG="$CACHE_DIR/$(basename "$UBUNTU_URL")"
if [[ -s "$CACHE_IMG" ]]; then
  echo "   using cached $CACHE_IMG"
else
  echo "   downloading $(basename "$UBUNTU_URL") -> cache"
  curl -fL --retry 3 -C - "$UBUNTU_URL" -o "$CACHE_IMG"
fi
cp --reflink=auto "$CACHE_IMG" "$WORK/base.img"
# Grow the filesystem to BUILD_SIZE so the OS + Docker + baked image fit. The
# final ${DISK_SIZE} virtual size is set cheaply at the end; the fs auto-expands
# to it on the reviewer's first boot (cloud-init growpart).
qemu-img create -f qcow2 "$WORK/build.img" "$BUILD_SIZE"
virt-resize --expand "$ROOT_PART" "$WORK/base.img" "$WORK/build.img"

echo "== [3/6] stage guest assets =="
# First boot (on a networked machine) installs Docker + deps, loads the baked
# redroid image, and enables binder. Deferred here -- NOT done in virt-customize
# -- so the build needs no network inside the libguestfs appliance (whose
# minimal DNS often cannot resolve the archives). Runs once, then disables itself.
cat > "$WORK/firstboot.sh" <<'EOF'
#!/bin/bash
set -x
export DEBIAN_FRONTEND=noninteractive
apt-get update -y
apt-get install -y docker.io openssh-server qemu-guest-agent mesa-utils \
                   libgl1-mesa-dri linux-generic "linux-modules-extra-$(uname -r)"
systemctl enable --now docker ssh
usermod -aG docker redroid
gunzip -c /opt/redroid-ndk.tar.gz | docker load
modprobe binder_linux devices=binder,hwbinder,vndbinder || true
systemctl disable redroid-firstboot.service
EOF
cat > "$WORK/redroid-firstboot.service" <<'EOF'
[Unit]
Description=First boot: install Docker, load redroid image, enable binder
After=network-online.target
Wants=network-online.target
[Service]
Type=oneshot
ExecStart=/opt/firstboot.sh
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
EOF

echo "== [4/6] customize the image with virt-customize (offline; no appliance network) =="
virt-customize -a "$WORK/build.img" \
  --write '/etc/modules-load.d/binder.conf:binder_linux' \
  --write '/etc/modprobe.d/binder.conf:options binder_linux devices=binder,hwbinder,vndbinder' \
  --run-command 'useradd -m -s /bin/bash -G sudo redroid || usermod -aG sudo redroid' \
  --password "redroid:password:${REDROID_PASS}" \
  --write '/etc/sudoers.d/redroid:redroid ALL=(ALL) NOPASSWD: /sbin/modprobe, /usr/sbin/modprobe' \
  --run-command 'chmod 440 /etc/sudoers.d/redroid' \
  --mkdir /home/redroid \
  --upload "$GUEST_SRC/init.sh:/home/redroid/init.sh" \
  --upload "$WORK/redroid-ndk.tar.gz:/opt/redroid-ndk.tar.gz" \
  --upload "$WORK/firstboot.sh:/opt/firstboot.sh" \
  --upload "$WORK/redroid-firstboot.service:/etc/systemd/system/redroid-firstboot.service" \
  --run-command 'chmod +x /home/redroid/init.sh /opt/firstboot.sh; chown -R redroid:redroid /home/redroid' \
  --run-command 'systemctl enable redroid-firstboot.service' \
  --run-command 'echo redroid-outer > /etc/hostname'

echo "== [5/6] sparsify + compress, then set the final ${DISK_SIZE} virtual size -> $OUT =="
mkdir -p "$(dirname "$OUT")"
virt-sparsify --compress "$WORK/build.img" "$OUT"
# Cheap: grows only the virtual size (qcow2 stays thin); the guest fs fills it on
# first boot via cloud-init growpart.
qemu-img resize "$OUT" "$DISK_SIZE"
[[ -n "${SUDO_USER:-}" ]] && chown "$SUDO_USER":"$SUDO_USER" "$OUT" || true

echo "== [6/6] done: $OUT ($(du -h "$OUT" | cut -f1)) =="
cat <<EOF

Next -- publish to the GitHub release (you do the upload):
  sha256sum "$OUT" > "${OUT}.sha256"
  # Upload BOTH files (redroid.qcow2 + redroid.qcow2.sha256) to the release.
  # Reviewers then run scripts/fetch_redroid_image.sh (download + verify).
EOF
