#!/usr/bin/env bash

PATH=$PATH:$HOME/Android/Sdk/emulator:$HOME/Android/Sdk/platform-tools:$HOME/Android/Sdk/cmdline-tools/latest/bin/

ACTION=${1:-run}
shift || true

COUNT=${1:-1}
ADB_BASE_PORT=${2:-5555}
CONSOLE_BASE_PORT=${3:-55554}
AVD_PREFIX=${4:-avd-batch}
IMAGE_PACKAGE="system-images;android-34;google_apis;x86_64"
SDK_ROOT="${ANDROID_SDK_ROOT:-${HOME}/Android/Sdk}"

SCRIPT_DIR=$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" && pwd )
BASE_DIR=${BASE_DIR:-${SCRIPT_DIR}}
LOG_DIR=${LOG_DIR:-${BASE_DIR}/log}
mkdir -p "${LOG_DIR}"
LOG_DIR=$(cd "${LOG_DIR}" >/dev/null 2>&1 && pwd || echo "${LOG_DIR}")
SUMMARY_LOG="${LOG_DIR}/avd_multi_summary.log"
AVD_PRIME_AUTO=${AVD_PRIME_AUTO:-1}
AVD_NO_WINDOW=${AVD_NO_WINDOW:-0}

ensure_adb() {
    if ! command -v adb >/dev/null 2>&1; then
        echo "$(date) ERROR: adb not found. Please ensure platform-tools are installed." >&2
        exit 1
    fi
}

ensure_creation_tools() {
    if ! command -v sdkmanager >/dev/null 2>&1; then
        echo "$(date) ERROR: sdkmanager not found. Please ensure ANDROID_SDK_ROOT is set and tools are installed." >&2
        exit 1
    fi

    if ! command -v avdmanager >/dev/null 2>&1; then
        echo "$(date) ERROR: avdmanager not found. Please install Android SDK platform tools." >&2
        exit 1
    fi

    if [[ ! -d "${SDK_ROOT}/emulator" ]]; then
        echo "$(date) ERROR: Emulator binaries not found under ${SDK_ROOT}/emulator." >&2
        exit 1
    fi
}

start_adb_server() {
    ensure_adb
    adb kill-server >/dev/null 2>&1
    adb -a start-server >/dev/null 2>&1
}

avd_is_running() {
    local target_name="$1"
    while IFS= read -r pid; do
        [[ -z "$pid" ]] && continue
        [[ ! -r "/proc/$pid/cmdline" ]] && continue
        if tr '\0' ' ' < "/proc/$pid/cmdline" | grep -Fq -- "-avd ${target_name}"; then
            return 0
        fi
    done < <(pgrep -f "qemu-system" || true)
    return 1
}

find_serial_for_avd() {
    local target_name="$1"
    ensure_adb
    while read -r serial; do
        local avd_name
        avd_name=$(adb -s "${serial}" emu avd name 2>/dev/null | tr -d '\r')
        if [[ "${avd_name}" == "${target_name}" ]]; then
            echo "${serial}"
            return 0
        fi
    done < <(adb devices | awk '/^emulator-/{print $1}')
    return 1
}

get_avail_kb() {
    df --output=avail -k "$1" 2>/dev/null | tail -1 | tr -d ' '
}

run_avds() {
    ensure_creation_tools
    start_adb_server

    echo "Ensuring system image ${IMAGE_PACKAGE} is installed..."
    yes | sdkmanager --install "${IMAGE_PACKAGE}" >/dev/null

    mkdir -p "${HOME}/.android/avd"

    mksdcard_bin=""
    if [[ -x "${SDK_ROOT}/emulator/mksdcard" ]]; then
        mksdcard_bin="${SDK_ROOT}/emulator/mksdcard"
    elif command -v mksdcard >/dev/null 2>&1; then
        mksdcard_bin="$(command -v mksdcard)"
    fi

    echo "Logs will be written to: ${LOG_DIR}"

    for (( i=1; i<=COUNT; i++ )); do
        NAME="${AVD_PREFIX}-${i}"
        ADB_PORT=$((ADB_BASE_PORT + (i-1)*2))
        CONSOLE_PORT=$((CONSOLE_BASE_PORT + (i-1)*2))
        local -a gpu_env=()
        local offload_val
        if (( AVD_PRIME_AUTO )); then
            offload_val=$(( ((i-1) % 2) + 1 ))
            echo "$(date) INFO: ${NAME} using PRIME Render Offload = ${offload_val}" | tee -a "${SUMMARY_LOG}"
            gpu_env=("__NV_PRIME_RENDER_OFFLOAD=${offload_val}" "__GLX_VENDOR_LIBRARY_NAME=nvidia")
        elif [[ -n "${__NV_PRIME_RENDER_OFFLOAD:-}" && -n "${__GLX_VENDOR_LIBRARY_NAME:-}" ]]; then
            echo "$(date) INFO: ${NAME} using user-provided PRIME Offload = ${__NV_PRIME_RENDER_OFFLOAD}" | tee -a "${SUMMARY_LOG}"
            gpu_env=("__NV_PRIME_RENDER_OFFLOAD=${__NV_PRIME_RENDER_OFFLOAD}" "__GLX_VENDOR_LIBRARY_NAME=${__GLX_VENDOR_LIBRARY_NAME}")
        fi

        CREATED=0
        AVD_DIR="${HOME}/.android/avd/${NAME}.avd"
        CONFIG_FILE="${AVD_DIR}/config.ini"

        if [[ -d "${AVD_DIR}" ]] || avdmanager list avd | grep -q "Name: ${NAME}$"; then
            echo "AVD ${NAME} already exists; updating configuration if needed."
        else
            echo "Creating AVD ${NAME}..."
            create_attempts=0
            create_ok=0
            sdcard_file="${AVD_DIR}/sdcard.img"
            log_file="${LOG_DIR}/avd_create_${NAME}.log"

            while (( create_attempts < 3 )); do
                create_attempts=$((create_attempts+1))

                [[ -f "${log_file}" ]] && mv "${log_file}" "${log_file}.bak.$(date +%s)" 2>/dev/null || true
                [[ -f "${sdcard_file}" ]] && rm -f "${sdcard_file}" 2>/dev/null || true

                # Provision a 2 GiB SD card image.
                if [[ -n "${mksdcard_bin}" ]]; then
                    "${mksdcard_bin}" 2048M "${sdcard_file}" >> "${log_file}" 2>&1 || true
                fi

                if [[ -f "${sdcard_file}" ]]; then
                    yes | avdmanager create avd -n "${NAME}" -k "${IMAGE_PACKAGE}" --device "pixel_5" --sdcard "${sdcard_file}" > "${log_file}" 2>&1
                else
                    yes | avdmanager create avd -n "${NAME}" -k "${IMAGE_PACKAGE}" --device "pixel_5" --sdcard 2G > "${log_file}" 2>&1
                fi

                sleep 1

                if [[ -f "${CONFIG_FILE}" ]]; then
                    create_ok=1
                    CREATED=1
                    break
                fi

                echo "Failed creating AVD ${NAME}, retrying..."
                sleep 2
            done

            [[ $create_ok == 0 ]] && echo "ERROR: Failed to create ${NAME}" && continue
        fi

        # Wait for config.ini to appear.
        if (( CREATED == 1 )); then
            attempts=0
            while [[ ! -f "${CONFIG_FILE}" && ${attempts} -lt 20 ]]; do
                sleep 1
                attempts=$((attempts+1))
            done
            [[ ! -f "${CONFIG_FILE}" ]] && echo "Broken AVD ${NAME}" && rm -rf "${AVD_DIR}" && continue
        fi

        [[ -f "${CONFIG_FILE}" && ! -s "${CONFIG_FILE}" ]] && echo "Empty config, removing ${NAME}" && rm -rf "${AVD_DIR}" && continue

        # Tune config.ini for the benchmark profile.
        grep -q '^disk.dataPartition.size=' "${CONFIG_FILE}" \
            && sed -i 's/^disk.dataPartition.size=.*/disk.dataPartition.size=8G/' "${CONFIG_FILE}" \
            || echo 'disk.dataPartition.size=8G' >> "${CONFIG_FILE}"

        grep -q '^cachePartition=' "${CONFIG_FILE}" \
            && sed -i 's/^cachePartition=.*/cachePartition=true/' "${CONFIG_FILE}" \
            || echo 'cachePartition=true' >> "${CONFIG_FILE}"

        grep -q '^cachePartition.size=' "${CONFIG_FILE}" \
            && sed -i 's/^cachePartition.size=.*/cachePartition.size=256M/' "${CONFIG_FILE}" \
            || echo 'cachePartition.size=256M' >> "${CONFIG_FILE}"

        grep -q '^hw.ramSize=' "${CONFIG_FILE}" && sed -i 's/^hw.ramSize=.*/hw.ramSize=2048/' "${CONFIG_FILE}" || echo 'hw.ramSize=2048' >> "${CONFIG_FILE}"
        grep -q '^hw.cpu.ncore=' "${CONFIG_FILE}" && sed -i 's/^hw.cpu.ncore=.*/hw.cpu.ncore=4/' "${CONFIG_FILE}" || echo 'hw.cpu.ncore=4' >> "${CONFIG_FILE}"
        grep -q '^hw.lcd.width=' "${CONFIG_FILE}" && sed -i 's/^hw.lcd.width=.*/hw.lcd.width=1080/' "${CONFIG_FILE}" || echo 'hw.lcd.width=1080' >> "${CONFIG_FILE}"
        grep -q '^hw.lcd.height=' "${CONFIG_FILE}" && sed -i 's/^hw.lcd.height=.*/hw.lcd.height=1920/' "${CONFIG_FILE}" || echo 'hw.lcd.height=1920' >> "${CONFIG_FILE}"
        grep -q '^hw.lcd.density=' "${CONFIG_FILE}" && sed -i 's/^hw.lcd.density=.*/hw.lcd.density=420/' "${CONFIG_FILE}" || echo 'hw.lcd.density=420' >> "${CONFIG_FILE}"

        grep -q '^vulkan=' "${CONFIG_FILE}" \
            && sed -i 's/^vulkan=.*/vulkan=off/' "${CONFIG_FILE}" \
            || echo 'vulkan=off' >> "${CONFIG_FILE}"

        grep -q '^GLDirectMem=' "${CONFIG_FILE}" \
            && sed -i 's/^GLDirectMem=.*/GLDirectMem=on/' "${CONFIG_FILE}" \
            || echo 'GLDirectMem=on' >> "${CONFIG_FILE}"

        if avd_is_running "${NAME}"; then
            echo "Emulator ${NAME} is already running; skipping launch."
            continue
        fi

        echo "Launching emulator ${NAME}..."
        if ((${#gpu_env[@]})); then
            echo "  GPU env: ${gpu_env[*]}" | tee -a "${SUMMARY_LOG}"
        fi
        local -a emulator_cmd=(
            "${SDK_ROOT}/emulator/emulator"
            -avd "${NAME}"
            -ports "${CONSOLE_PORT},${ADB_PORT}"
            -no-snapshot
            -netdelay none
            -netspeed full
            -gpu host
            -feature -Vulkan
            -verbose
        )
        if (( AVD_NO_WINDOW )); then
            echo "  Headless mode enabled for ${NAME}" | tee -a "${SUMMARY_LOG}"
            emulator_cmd+=(-no-window)
        fi
        # Disable Vulkan while keeping OpenGL host acceleration enabled
        if ((${#gpu_env[@]})); then
            env "${gpu_env[@]}" "${emulator_cmd[@]}" > "${LOG_DIR}/emulator_${NAME}.log" 2>&1 &
        else
            "${emulator_cmd[@]}" > "${LOG_DIR}/emulator_${NAME}.log" 2>&1 &
        fi
        # -no-window
        sleep 1

        local serial
        serial=$(find_serial_for_avd "${NAME}")
        if [[ -n "${serial}" ]]; then
            echo "  -> registered as ${serial}"
        fi
    done

    echo "Started ${COUNT} emulator instance(s) with prefix ${AVD_PREFIX}."
}


stop_avds() {
    echo "Stopping ALL running Android emulator instances..."
    PIDS=$(ps -ef | grep qemu-system | grep "\-avd" | grep -v grep | awk '{print $2}')
    [[ -z "$PIDS" ]] && echo "No emulator processes found." && return

    echo "$PIDS" | while read -r pid; do
        AVD_NAME=$(ps -p $pid -o args= | sed -n 's/.*-avd \([^ ]*\).*/\1/p')
        echo "  -> Killing PID $pid (AVD: $AVD_NAME)"
        kill -9 $pid
    done
}

remove_avds() {
    stop_avds
    for (( i=1; i<=COUNT; i++ )); do
        NAME="${AVD_PREFIX}-${i}"
        AVD_PATH="${HOME}/.android/avd/${NAME}.avd"
        INI_PATH="${HOME}/.android/avd/${NAME}.ini"
        echo "Removing AVD ${NAME}..."
        [[ -d "${AVD_PATH}" ]] && rm -rf "${AVD_PATH}"
        [[ -f "${INI_PATH}" ]] && rm -f "${INI_PATH}"
    done
}

case "${ACTION}" in
    run) run_avds ;;
    stop) stop_avds ;;
    rm|remove|delete) remove_avds ;;
    *)
cat <<EOF
Usage: $0 <run|stop|rm> [count] [adb_base_port] [console_base_port] [avd_prefix]

Examples:
  $0 run 16 5555 55555 studio-batch
  $0 stop 16 5555 55555 studio-batch
  $0 rm 16 5555 55555 studio-batch
EOF
        exit 1
        ;;
esac
