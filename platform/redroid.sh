#!/usr/bin/env bash
# Outer-VM launcher for the Redroid baseline: boots the prebuilt qcow2 (Ubuntu +
# Docker + redroid + init.sh) and forwards SSH (22) and the adb bridge ports
# (5555-5655) back to the host. See docs/setup.md > Redroid.

cd "${BASE_DIR:?set BASE_DIR (see scalebench/env.example)}"

ulimit -n 100000

# Prebuilt outer-VM image; override with REDROID_IMG_PATH.
REDROID_IMG_PATH="${REDROID_IMG_PATH:-${BASE_DIR}/img/redroid/redroid.qcow2}"
# Size the VM to your host. The paper machine used 36 vCPU / 180 GiB; 8 vCPU /
# 16 GiB is a sane floor for low tenant counts.
REDROID_VM_CPUS="${REDROID_VM_CPUS:-8}"
REDROID_VM_MEM="${REDROID_VM_MEM:-16G}"
REDROID_SEED_IMG="${REDROID_SEED_IMG:-${BASE_DIR}/img/redroid/redroid-seed.img}"
REDROID_VM_PASSWORD="${REDROID_VM_PASSWORD:-redroid}"
REDROID_DISPLAY="${REDROID_DISPLAY:-sdl,gl=on}"
REDROID_SERIAL_LOG="${REDROID_SERIAL_LOG:-${BASE_DIR}/log/redroid-serial.log}"
REDROID_QEMU_LOG="${REDROID_QEMU_LOG:-${BASE_DIR}/log/redroid-qemu.log}"

find_ovmf() {
    if [[ -n "${REDROID_OVMF_PATH:-}" ]]; then
        [[ -f "${REDROID_OVMF_PATH}" ]] || {
            echo "redroid.sh: REDROID_OVMF_PATH does not exist: ${REDROID_OVMF_PATH}" >&2
            exit 1
        }
        echo "${REDROID_OVMF_PATH}"
        return 0
    fi

    local candidate
    for candidate in /usr/share/qemu/OVMF.fd /usr/share/ovmf/OVMF.fd /snap/multipass/current/qemu/OVMF.fd; do
        if [[ -f "${candidate}" ]]; then
            echo "${candidate}"
            return 0
        fi
    done

    echo "redroid.sh: OVMF firmware not found; install the 'ovmf' package or set REDROID_OVMF_PATH" >&2
    exit 1
}

ensure_seed_img() {
    if [[ -f "${REDROID_SEED_IMG}" && "${REDROID_RECREATE_SEED:-0}" != "1" ]]; then
        return 0
    fi
    command -v mkfs.vfat >/dev/null || {
        echo "redroid.sh: mkfs.vfat is required to create ${REDROID_SEED_IMG} (install dosfstools)" >&2
        exit 1
    }
    command -v mcopy >/dev/null || {
        echo "redroid.sh: mcopy is required to create ${REDROID_SEED_IMG} (install mtools)" >&2
        exit 1
    }

    local tmpdir
    tmpdir="$(mktemp -d)"
    cat >"${tmpdir}/meta-data" <<EOF
instance-id: redroid-outer
local-hostname: redroid-outer
EOF
    cat >"${tmpdir}/user-data" <<EOF
#cloud-config
ssh_pwauth: true
chpasswd:
  expire: false
  users:
    - {name: redroid, password: ${REDROID_VM_PASSWORD}, type: text}
EOF
    cat >"${tmpdir}/network-config" <<EOF
version: 2
ethernets:
  redroid0:
    match:
      name: "en*"
    dhcp4: true
    dhcp6: false
EOF
    mkdir -p "$(dirname "${REDROID_SEED_IMG}")"
    rm -f "${REDROID_SEED_IMG}"
    truncate -s 16M "${REDROID_SEED_IMG}"
    mkfs.vfat -n CIDATA "${REDROID_SEED_IMG}" >/dev/null
    mcopy -i "${REDROID_SEED_IMG}" \
        "${tmpdir}/user-data" "${tmpdir}/meta-data" "${tmpdir}/network-config" ::/
    rm -rf "${tmpdir}"
}

if [[ ! -f "${REDROID_IMG_PATH}" ]]; then
    echo "redroid.sh: outer-VM image not found at ${REDROID_IMG_PATH}" >&2
    echo "  download the prebuilt image and set REDROID_IMG_PATH (see docs/setup.md)" >&2
    exit 1
fi

OVMF_PATH="$(find_ovmf)"
ensure_seed_img
mkdir -p "$(dirname "${REDROID_SERIAL_LOG}")" "$(dirname "${REDROID_QEMU_LOG}")"

# hostfwd list: SSH (host REDROID_SSH_PORT -> guest 22) + adb bridge ports 5555-5655.
HOSTFWD="hostfwd=tcp::${REDROID_SSH_PORT:-2222}-:22"
for p in $(seq 5555 5655); do
    HOSTFWD+=",hostfwd=tcp::${p}-:${p}"
done

bin/qemu-system-x86_64 -bios "${OVMF_PATH}" -enable-kvm -cpu host \
    -smp "${REDROID_VM_CPUS}" -m "${REDROID_VM_MEM}" \
    -netdev "user,id=net0,${HOSTFWD}" -device virtio-net-pci,netdev=net0 \
    -device virtio-scsi-pci,id=scsi0 \
    -device virtio-vga-gl \
    -display "${REDROID_DISPLAY}" \
    -usb -device usb-tablet \
    -serial "file:${REDROID_SERIAL_LOG}" \
    -D "${REDROID_QEMU_LOG}" \
    -drive "file=${REDROID_IMG_PATH},if=none,format=qcow2,discard=unmap,id=redroiddisk" \
    -device scsi-hd,drive=redroiddisk,bus=scsi0.0 \
    -drive "file=${REDROID_SEED_IMG},format=raw,if=virtio,readonly=on"

cd - >/dev/null
