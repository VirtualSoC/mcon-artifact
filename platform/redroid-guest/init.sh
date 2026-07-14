#!/usr/bin/env bash
# Redroid container control -- runs INSIDE the outer VM at /home/redroid/init.sh.
#
# The mconbench redroid driver calls:
#     cd /home/redroid && ./init.sh run <count>      # cold-boot <count> containers
#     ./init.sh stop                                 # stop all
#     ./init.sh rm                                   # remove all (clean slate)
#
# Container i (0-based) exposes adb on BASE_PORT+i; the outer VM forwards that
# guest port to the same host port, so the host addresses tenant i as
# localhost:(5555+i) -- exactly what BaselineDriver.serial() expects.
set -euo pipefail

CMD="${1:-}"
ARG2="${2:-}"
ARG3="${3:-}"

# redroid image WITH libndk arm64 translation, built by
# scripts/build_redroid_image.sh (zhouziyang/libndk_translation v0.2.3, Android 13).
IMAGE="${REDROID_IMAGE:-redroid/redroid:13.0.0-ndk}"
DEFAULT_BASE_PORT="${DEFAULT_BASE_PORT:-5555}"
BASE_PORT="${BASE_PORT:-$DEFAULT_BASE_PORT}"
COUNT=128
CONTAINER_PREFIX="redroid"
MAX_PARALLEL_STARTS=16
# host = render on the outer VM's GPU (virtio-vga-gl, matches the paper);
# guest = software render (use on a headless outer VM without a GL stack).
GPU_MODE="${REDROID_GPU_MODE:-host}"

# libndk arm-translation runtime props (Android 13 / ndk_translation 0.2.3).
NDK_PROPS=(
  ro.product.cpu.abilist=x86_64,arm64-v8a,x86,armeabi-v7a,armeabi
  ro.product.cpu.abilist64=x86_64,arm64-v8a
  ro.product.cpu.abilist32=x86,armeabi-v7a,armeabi
  ro.dalvik.vm.isa.arm=x86
  ro.dalvik.vm.isa.arm64=x86_64
  ro.enable.native.bridge.exec=1
  ro.vendor.enable.native.bridge.exec=1
  ro.vendor.enable.native.bridge.exec64=1
  ro.dalvik.vm.native.bridge=libndk_translation.so
  ro.ndk_translation.version=0.2.3
)

usage() { echo "Usage: $0 {run <count> [base_port] | stop | rm}"; exit 1; }

load_modules() {
  # binder is also loaded at boot (see build_redroid_image.sh); harmless if set.
  sudo modprobe binder_linux devices="binder,hwbinder,vndbinder" || true
}

container_name() { echo "${CONTAINER_PREFIX}$1"; }
exists()  { docker ps -a --format '{{.Names}}' | grep -qx "$(container_name "$1")"; }
running() { docker ps    --format '{{.Names}}' | grep -qx "$(container_name "$1")"; }

start_container() {
  local idx="$1" port="$2" name
  name="$(container_name "$idx")"
  if exists "$idx"; then
    running "$idx" || docker start "$name" >/dev/null 2>&1 || true
    return 0
  fi
  docker run -d --privileged \
    --name "$name" \
    -v /data \
    -p "${port}:5555" \
    "$IMAGE" \
    androidboot.redroid_width=1080 \
    androidboot.redroid_height=1920 \
    androidboot.redroid_dpi=240 \
    androidboot.redroid_fps=60 \
    androidboot.redroid_gpu_mode="${GPU_MODE}" \
    ro.setupwizard.mode=DISABLED \
    "${NDK_PROPS[@]}" >/dev/null 2>&1 || echo "[init] failed to start ${name}" >&2
}

do_run() {
  local total="${1:-$COUNT}" base_port="${2:-$BASE_PORT}" start_index="${3:-0}"
  load_modules
  local -a pids=()
  for ((i = 0; i < total; i++)); do
    start_container "$((start_index + i))" "$((base_port + i))" &
    pids+=($!)
    if ((${#pids[@]} >= MAX_PARALLEL_STARTS)); then
      wait "${pids[0]}" || true
      pids=("${pids[@]:1}")
    fi
  done
  for pid in "${pids[@]}"; do wait "$pid" || true; done
}

case "$CMD" in
  run)
    [[ "$ARG2" =~ ^[0-9]+$ ]] && COUNT="$ARG2"
    [[ "$ARG3" =~ ^[0-9]+$ ]] && BASE_PORT="$ARG3"
    start_index=$((BASE_PORT - DEFAULT_BASE_PORT))
    ((start_index < 0)) && start_index=0
    do_run "$COUNT" "$BASE_PORT" "$start_index"
    ;;
  stop)
    mapfile -t names < <(docker ps --format '{{.Names}}' | grep "^${CONTAINER_PREFIX}" || true)
    for name in "${names[@]:-}"; do
      [[ -n "$name" ]] || continue
      echo "Stopping $name..."
      docker stop -t 10 "$name" >/dev/null 2>&1 || true
    done
    ;;
  rm)
    mapfile -t names < <(docker ps -a --format '{{.Names}}' | grep "^${CONTAINER_PREFIX}" || true)
    for name in "${names[@]:-}"; do
      [[ -n "$name" ]] || continue
      echo "Removing $name..."
      docker rm -f "$name" >/dev/null 2>&1 || true
    done
    docker volume prune -f >/dev/null 2>&1 || true
    ;;
  *)
    usage
    ;;
esac
