#!/usr/bin/env bash
set -euo pipefail

ACTION=${1:-run}
shift || true

COUNT=${1:-1}
BASE_MONITOR_PORT=${2:-55555}
BASE_ADB_PORT=${3:-5555}

LOG_DIR="${BASE_DIR}/log"
mkdir -p "${LOG_DIR}"

BASE_USERDATA_BKP="${GUEST_IMG_PATH}/userdata_bkp.qcow2"
DEFAULT_USERDATA="${GUEST_IMG_PATH}/userdata.qcow2"

create_userdata_overlay() {
    local idx=$1
    local ud_img="${GUEST_IMG_PATH}/userdata_multi_${idx}.qcow2"

    if [[ -f "${ud_img}" ]]; then
        echo "${ud_img}"
        return 0
    fi

    local backing=""
    if [[ -f "${DEFAULT_USERDATA}" ]]; then
        backing="${DEFAULT_USERDATA}"
    elif [[ -f "${BASE_USERDATA_BKP}" ]]; then
        backing="${BASE_USERDATA_BKP}"
    else
        echo "$(date) ERROR: no userdata backing image found for instance ${idx}" >&2
        return 1
    fi

    if ! bin/qemu-img create -f qcow2 -b "${backing}" "${ud_img}" >/dev/null 2>&1; then
        cp -f "${backing}" "${ud_img}"
    fi
    echo "${ud_img}"
}

start_instance() {
    local idx=$1
    local monitor_port=$2
    local adb_port=$3

    local ud_img
    ud_img=$(create_userdata_overlay "${idx}") || return 1

    local -a gpu_env=()
    local auto_prime=${VSOC_PRIME_AUTO:-1}
    if (( auto_prime )); then
        local offload_val=$(( (idx % 2) + 1 ))
        echo "$(date) INFO: instance ${idx} 使用 PRIME Render Offload = ${offload_val}" | tee -a "${LOG_DIR}/bliss_multi_summary.log"
        gpu_env=("__NV_PRIME_RENDER_OFFLOAD=${offload_val}" "__GLX_VENDOR_LIBRARY_NAME=nvidia")
    elif [[ -n "${__NV_PRIME_RENDER_OFFLOAD:-}" && -n "${__GLX_VENDOR_LIBRARY_NAME:-}" ]]; then
        echo "$(date) INFO: instance ${idx} 使用用户提供的 PRIME Offload = ${__NV_PRIME_RENDER_OFFLOAD}" | tee -a "${LOG_DIR}/bliss_multi_summary.log"
        gpu_env=("__NV_PRIME_RENDER_OFFLOAD=${__NV_PRIME_RENDER_OFFLOAD}" "__GLX_VENDOR_LIBRARY_NAME=${__GLX_VENDOR_LIBRARY_NAME}")
    fi

    local pidfile="${LOG_DIR}/bliss_multi_${idx}.pid"
    local qlog="${LOG_DIR}/bliss_multi_${idx}.log"
    local serial_log="${LOG_DIR}/bliss_multi_kernel_${idx}.log"

    local qemu_args=(
        bin/qemu-system-x86_64
        -accel kvm -cpu max -m 2048 -smp 1
        -kernel "${GUEST_IMG_PATH}/kernel"
        -append "nokaslr no_timer_check syscall_hardening=off root=/dev/ram0 androidboot.hardware=redroid androidboot.fstab_suffix=redroid androidboot.selinux=permissive console=ttyS0"
        -initrd "${GUEST_IMG_PATH}/ramdisk.img"
        -drive index=0,if=virtio,id=system,file="${GUEST_IMG_PATH}/system.img",format=raw,readonly=on
        -drive index=1,if=virtio,id=vendor,file="${GUEST_IMG_PATH}/vendor.img",format=raw,readonly=on
        -drive index=2,if=virtio,id=userdata,file="${ud_img}",format=qcow2
        -display none
        -device teleport,gl_debug=off,gl_log_level=0,display_width=1080,display_height=1920,window_width=540,window_height=960,refresh_rate=60,display_count=1,headless_mode=on,bridge_port=${adb_port}
        -netdev user,id=cell -device virtio-net-pci,netdev=cell
        -netdev user,id=wlan -device virtio-net-pci,netdev=wlan
        -name "bliss-multi-${idx}",debug-threads=on
    )

    if [[ "${VSOC_DEBUG_STDOUT:-0}" == "1" ]]; then
        echo "$(date) INFO: [fg] instance ${idx} monitor=${monitor_port} adb=${adb_port}"
        local -a debug_cmd=("${qemu_args[@]}" -monitor stdio -serial stdio)
        if ((${#gpu_env[@]})); then
            env "${gpu_env[@]}" "${debug_cmd[@]}"
        else
            "${debug_cmd[@]}"
        fi
    else
        rm -f "${pidfile}"
        local -a daemon_cmd=("${qemu_args[@]}" \
            -daemonize -pidfile "${pidfile}" \
            -monitor none \
            -serial "file:${serial_log}" \
            -D "${qlog}")
        if ((${#gpu_env[@]})); then
            env "${gpu_env[@]}" "${daemon_cmd[@]}"
        else
            "${daemon_cmd[@]}"
        fi
    fi
}

stop_instances() {
    local killed=0
    mapfile -t pids < <(pgrep -f "bliss-multi-" || true)
    if ((${#pids[@]} == 0)); then
        echo "$(date) INFO: no bliss-multi qemu processes running"
        return 0
    fi

    echo "$(date) INFO: stopping ${#pids[@]} qemu processes"
    for pid in "${pids[@]}"; do
        kill "${pid}" >/dev/null 2>&1 || true
    done

    for pid in "${pids[@]}"; do
        for _ in {1..10}; do
            kill -0 "${pid}" >/dev/null 2>&1 || break
            sleep 1
        done
        if kill -0 "${pid}" >/dev/null 2>&1; then
            kill -9 "${pid}" >/dev/null 2>&1 || true
        else
            ((killed++))
        fi
    done

    rm -f "${LOG_DIR}"/bliss_multi_*.pid
    echo "$(date) INFO: stopped ${killed}/${#pids[@]} qemu processes" | tee -a "${LOG_DIR}/bliss_multi_summary.log"
}

remove_instances() {
    stop_instances || true
    for ((i=0; i<COUNT; i++)); do
        rm -f "${GUEST_IMG_PATH}/userdata_multi_${i}.qcow2"
        rm -f "${LOG_DIR}/bliss_multi_${i}.pid" "${LOG_DIR}/bliss_multi_${i}.log" "${LOG_DIR}/bliss_multi_kernel_${i}.log"
    done
    echo "$(date) INFO: removed overlays/logs for ${COUNT} instance(s)" | tee -a "${LOG_DIR}/bliss_multi_summary.log"
}

run_instances() {
    pushd "${BASE_DIR}" >/dev/null
    ulimit -n 100000
    for ((i=0; i<COUNT; i++)); do
        start_instance "${i}" $((BASE_MONITOR_PORT + i)) $((BASE_ADB_PORT + i)) || true
        sleep 1
    done
    echo "$(date) INFO: launched ${COUNT} instance(s). Monitor ports ${BASE_MONITOR_PORT}..$((BASE_MONITOR_PORT + COUNT - 1)), ADB ports ${BASE_ADB_PORT}..$((BASE_ADB_PORT + COUNT - 1))" | tee -a "${LOG_DIR}/bliss_multi_summary.log"
    echo "Connect via: adb connect localhost:${BASE_ADB_PORT}" | tee -a "${LOG_DIR}/bliss_multi_summary.log"
    popd >/dev/null
}

case "${ACTION}" in
    run) run_instances ;;
    stop) stop_instances ;;
    rm|remove|delete) remove_instances ;;
    *)
        cat <<EOF
Usage: $0 <run|stop|rm> [count] [base_monitor_port] [base_adb_port]
  run  : launch COUNT instances (default 4)
  stop : stop up to COUNT instances
  rm   : stop + delete per-instance userdata/logs
Example: BASE_DIR=/home/server/Desktop/vsoc GUEST_IMG_PATH=\$BASE_DIR/img/bliss \
         $0 run 16 60000 15555
EOF
        exit 1
        ;;
esac
