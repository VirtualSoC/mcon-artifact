#!/bin/bash
# =============================================================
# anbox-test.sh  (2025-12-08 latest enhanced version)
#
# Usage:
#   Start containers and connect: ./anbox_test.sh start <N> <adb_output_file>
#   Stop ADB:        ./anbox_test.sh stop-adb file_name
#   Stop containers:        ./anbox_test.sh stop-containers file_name
# =============================================================

# export DISPLAY=:1
# export XAUTHORITY=/home/server/.Xauthority
# export DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/1000/bus"


ANBOX_BACKEND="${ANBOX_BACKEND:-multipass}"
ANBOX_VM="${ANBOX_VM:-${AMBOX_VM:-anbox}}"
AMBOX_VM="$ANBOX_VM"

case "$ANBOX_BACKEND" in
    local|multipass) ;;
    *)
        echo "[ERROR] ANBOX_BACKEND must be 'local' or 'multipass'"
        exit 1
        ;;
esac

# Resolve paths from the script's own location (override with ANBOX_BASE_DIR).
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="${ANBOX_BASE_DIR:-$SCRIPT_DIR}"
CONNECT_LOG_DIR="$BASE_DIR/anbox_connect_logs"
mkdir -p "$CONNECT_LOG_DIR"

# Timestamp
TS=$(date +"%m%d-%H%M")

PID_FILE="$CONNECT_LOG_DIR/anbox_connect_pids_${TS}.txt"
CONTAINER_FILE="$CONNECT_LOG_DIR/anbox_containers_${TS}.txt"

# Log file
MAIN_LOG="$CONNECT_LOG_DIR/anbox_main_${TS}.log"

log() {
    echo "[$(date '+%F %T.%3N')] $*"
}

quote_cmd() {
    printf '%q ' "$@"
}

amc_cmd() {
    if [[ "$ANBOX_BACKEND" == "local" ]]; then
        amc "$@"
    else
        local cmd
        cmd=$(quote_cmd sudo amc "$@")
        multipass exec "$ANBOX_VM" -- bash -lc "$cmd"
    fi
}

gateway_share() {
    if [[ "$ANBOX_BACKEND" == "local" ]]; then
        anbox-cloud-appliance.gateway session share "$1"
    else
        local cmd
        cmd=$(quote_cmd sudo anbox-cloud-appliance.gateway session share "$1")
        multipass exec "$ANBOX_VM" -- bash -lc "$cmd"
    fi
}


# Redirect all output to the log while also displaying it on screen
exec > >(tee -a "$MAIN_LOG") 2>&1

echo "[INFO] using log file: $MAIN_LOG"

# -------------------------------------------------------------
# Function: stop all ADB
# -------------------------------------------------------------
stop_adb() {
    local file="$1"

    if [[ -z "$file" ]]; then
        echo "[ERROR] please provide a PID file name, e.g.: ./script.sh stop-adb pidfile.txt"
        exit 1
    fi

    if [[ ! -f "$file" ]]; then
        echo "[WARN] specified PID file does not exist: $file"
        exit 1
    fi

    echo "[INFO] stopping adb connections (from file: $file)..."

    while read pid; do
        if [[ -n "$pid" ]]; then
            if kill -0 "$pid" 2>/dev/null; then
                echo "Killing PID $pid"
                kill -9 "$pid"
            else
                echo "PID $pid does not exist or is not accessible"
            fi
        fi
    done < "$file"

    rm -f "$file"
    echo "[INFO] all adb connections closed."
    exit 0
}

# -------------------------------------------------------------
# Function: stop containers
# -------------------------------------------------------------
stop_containers() {
    FILE="$2"   # read the file name passed by the user

    if [[ -z "$FILE" ]]; then
        echo "[ERROR] please provide a container file name, e.g.:"
        echo "  ./anbox-test.sh stop-containers anbox_containers_1206-2102.txt"
        exit 1
    fi

    # If there is no absolute path, prepend BASE_DIR
    if [[ "$FILE" != /* ]]; then
        FILE="$BASE_DIR/$FILE"
    fi

    if [[ ! -f "$FILE" ]]; then
        echo "[ERROR] container file not found: $FILE"
        exit 1
    fi

    echo "[INFO] using container file: $FILE"
    echo "[INFO] stopping and deleting the following containers:"
    cat "$FILE"

    while read ID; do
        [[ -z "$ID" ]] && continue
        echo "[INFO] stopping container $ID"
        amc_cmd stop "$ID" </dev/null
        echo "[INFO] deleting container $ID"
        amc_cmd delete "$ID" --yes </dev/null
    done < "$FILE"

    echo "[INFO] container stop and delete complete."
    exit 0
}


if [[ "$1" == "stop-adb" ]]; then
    stop_adb "$2"
fi
if [[ "$1" == "stop-containers" ]]; then stop_containers "$@"; fi


# -------------------------------------------------------------
# START mode
# -------------------------------------------------------------
if [[ "$1" != "start" ]]; then
    echo "Usage:"
    echo "  ./anbox_test.sh start <N> <adb_output_file>"
    echo "  ./anbox_test.sh stop-adb"
    echo "  ./anbox_test.sh stop-containers"
    exit 1
fi

N="$2"
ADB_OUTPUT="${3:-$CONNECT_LOG_DIR/anbox_adb_${N}_${TS}.txt}"

# Create required files
touch "$PID_FILE" "$CONTAINER_FILE" "$ADB_OUTPUT"

echo "[INFO] starting workflow N=$N"
echo "" > "$CONTAINER_FILE"
echo "" > "$ADB_OUTPUT"

# -------------------------------------------------------------
# PART 1: create containers
# -------------------------------------------------------------

# -------------------------------------------------------------
# [NEW] Background monitor for amc ls (once per second), record running containers
# -------------------------------------------------------------

# LS_LOG="$CONNECT_LOG_DIR/anbox_running_${TS}.log"
# mkdir -p "$CONNECT_LOG_DIR"
# : > "$LS_LOG"

# log "[INFO] start amc ls background monitor (1s polling) -> $LS_LOG"

# (
# while true; do
#     multipass exec "$AMBOX_VM" -- bash -c "
#         sudo amc ls \
#         | grep ' running ' \
#         | grep -oE '\|[[:space:]]+[a-z0-9]{20,}[[:space:]]+\|' \
#         | awk -F'|' '{gsub(/ /, \"\", \$2); print \"'\"\$(date '+%F %T.%3N')\"'\" , \"running\", \$2}'
#     " 2>/dev/null >> "$LS_LOG"

#     sleep 1
# done
# ) &

# LS_MONITOR_PID=$!
# log "[INFO] amc ls monitor thread PID = $LS_MONITOR_PID"

LS_LOG="$CONNECT_LOG_DIR/anbox_running_${TS}.log"
mkdir -p "$CONNECT_LOG_DIR"
: > "$LS_LOG"

log "[INFO] start amc ls background monitor (only record containers newly entering running) -> $LS_LOG"

(
# Record the "previous round" set of running containers
PREV_RUNNING_IDS=""

while true; do
    # Get all running container IDs in the current round (the regex rule you verified works)
    CURR_RUNNING_IDS=$(amc_cmd ls 2>/dev/null \
        | grep ' running ' \
        | grep -oE '\|[[:space:]]+[a-z0-9]{20,}[[:space:]]+\|' \
        | awk -F'|' '{gsub(/ /, "", $2); print $2}')

    # Compare: find "containers newly entering running in this round"
    for ID in $CURR_RUNNING_IDS; do
        if ! echo "$PREV_RUNNING_IDS" | grep -qw "$ID"; then
            echo "$(date '+%F %T.%3N') running $ID" >> "$LS_LOG"
        fi
    done

    # Update the previous-round running set
    PREV_RUNNING_IDS="$CURR_RUNNING_IDS"

    sleep 1
done
) &

LS_MONITOR_PID=$!
log "[INFO] amc ls monitor thread PID = $LS_MONITOR_PID"


CONTAINER_IDS=()

for ((i=1; i<=N; i++)); do
    START_TS=$(date '+%F %T')
    log "[INFO] starting to create container #$i..."

            OUT=$(amc_cmd launch jammy:android15:amd64 \
                --no-wait \
            --enable-graphics --gpu-type nvidia --enable-streaming \
            --memory 3GB --disk-size 10GB --cpus 1 2>&1)

    END_TS=$(date '+%F %T')
    log "[INFO] create command for container #$i returned (start=$START_TS, end=$END_TS)"

    if echo "$OUT" | grep -qi "error"; then
        log "[ERROR] creation failed:"
        log "$OUT"
        continue
    fi

    ID=$(echo "$OUT" | grep -Eo "^[a-z0-9]{20,}" | tail -n 1)

    if [[ -z "$ID" ]]; then
        log "[ERROR] failed to parse container ID"
        log "$OUT"
        continue
    fi

    log "[INFO] created successfully: $ID"
    echo "$ID" >> "$CONTAINER_FILE"
    CONTAINER_IDS+=("$ID")
done

if (( ${#CONTAINER_IDS[@]} != N )); then
    log "[ERROR] only created ${#CONTAINER_IDS[@]} of $N containers"
    if kill -0 "$LS_MONITOR_PID" 2>/dev/null; then
        kill -9 "$LS_MONITOR_PID"
    fi
    exit 1
fi


# for ((i=1; i<=N; i++)); do
#     echo "[INFO] creating container #$i..."

#     OUT=$(multipass exec "$AMBOX_VM" -- \
#          bash -c "sudo amc launch jammy:android15:amd64 \
#          --enable-graphics --gpu-type nvidia --enable-streaming \
#          --memory 3GB --disk-size 5GB --cpus 2" 2>&1)

#     if echo "$OUT" | grep -qi "error"; then
#         echo "[ERROR] creation failed:"
#         echo "$OUT"
#         continue
#     fi

#     # take the last ID segment
#     ID=$(echo "$OUT" | grep -Eo "^[a-z0-9]{20,}" | tail -n 1)

#     if [[ -z "$ID" ]]; then
#         echo "[ERROR] parse failed"
#         echo "$OUT"
#         continue
#     fi

#     echo "[INFO] created successfully: $ID"
#     echo "$ID" >> "$CONTAINER_FILE"
#     CONTAINER_IDS+=("$ID")
# done

# -------------------------------------------------------------
# PART 2: wait for running
# -------------------------------------------------------------
echo "[INFO] waiting for containers to be running..."

MAX_WAIT=$((120 * N))
WAIT=0

while true; do
    LS=$(amc_cmd ls)

    OK=true
    for ID in "${CONTAINER_IDS[@]}"; do
        LINE=$(echo "$LS" | grep "$ID")
        if echo "$LINE" | grep -vq "running"; then
            OK=false
        fi
    done

    if $OK; then
        echo "[INFO] all containers are running"
        break
    fi

    if (( WAIT >= MAX_WAIT )); then
        echo "[ERROR] some containers are still not running"
        if kill -0 "$LS_MONITOR_PID" 2>/dev/null; then
            kill -9 "$LS_MONITOR_PID"
        fi
        exit 1
    fi

    sleep 3
    WAIT=$((WAIT+3))
done

# -------------------------------------------------------------
# [NEW] shut down the amc ls monitor thread
# -------------------------------------------------------------

sleep 5

if kill -0 "$LS_MONITOR_PID" 2>/dev/null; then
    log "[INFO] stopping amc ls monitor thread PID=$LS_MONITOR_PID"
    kill -9 "$LS_MONITOR_PID"
fi


# -------------------------------------------------------------
# PART 3: session extraction
# -------------------------------------------------------------
SESSION_IDS=()

for ID in "${CONTAINER_IDS[@]}"; do
    LINE=$(amc_cmd ls | grep "$ID")
    SESSION=$(echo "$LINE" | grep -o "session=[a-z0-9]*" | cut -d= -f2)
    SESSION_IDS+=("$SESSION")
    echo "[INFO] $ID session=$SESSION"
done

# -------------------------------------------------------------
# PART 4: obtain URL
# -------------------------------------------------------------
URLS=()

for S in "${SESSION_IDS[@]}"; do
    OUT=$(gateway_share "$S")

    URL=$(echo "$OUT" | grep -Eo "https://[^ ]+")
    URLS+=("$URL")
    echo "[INFO] session=$S URL=$URL"
done

# -------------------------------------------------------------
# PART 5: tmux launches anbox-connect (auto yes)
# -------------------------------------------------------------

# echo "" > "$PID_FILE"

# i=1
# for URL in "${URLS[@]}"; do
#     LOG="$CONNECT_LOG_DIR/session_${i}_${TS}.log"
#     SESSION_NAME="anbox_$i"

#     #################################################################
#     # 1. Create a tmux session and run anbox-connect inside it
#     #################################################################
#     # # tmux new-session -d -s "$SESSION_NAME" "
#     # #     echo '[INFO] running anbox-connect $URL';
#     # #     anbox-connect $URL -k | tee -a \"$LOG\";
#     # #     echo '[INFO] done (tmux session kept open)';
#     # #     bash
#     # # "
#     tmux new-session -d -s "$SESSION_NAME" "
#         echo '[INFO] running anbox-connect $URL';
#         anbox-connect \"$URL\" -k 2>&1 | tee -a \"$LOG\";
#         echo '[INFO] done (tmux session kept open)';
#         bash
#     "

#     # SESSION_NAME="anbox_$i"

#     # tmux new-session -d -s "$SESSION_NAME" "
#     #     echo '[INFO] running anbox-connect $URL';
#     #     anbox-connect \"$URL\" -k 2>&1;
#     #     echo '[INFO] done (tmux session kept open)';
#     #     bash
#     # "


#     #################################################################
#     # 2. Get the real tmux PID (used for kill)
#     #################################################################
#     REAL_PID=$(tmux list-panes -t "$SESSION_NAME" -F '#{pane_pid}')
#     echo "[INFO] actual PID = $REAL_PID"
#     echo "$REAL_PID" >> "$PID_FILE"


#     #################################################################
#     # 3. Inject keystrokes into tmux: Left + Enter (select yes)
#     #################################################################
#     tmux send-keys -t "$SESSION_NAME" Left
#     tmux send-keys -t "$SESSION_NAME" Enter

#     #################################################################
#     # 4. Collect the adb connect line
#     #################################################################
#     # sleep 2
#     # PANE_OUTPUT=$(tmux capture-pane -t "$SESSION_NAME" -p)
#     # ADB_LINE=$(echo "$PANE_OUTPUT" | grep "adb connect")

#     # if [[ -n "$ADB_LINE" ]]; then
#     #     echo "$ADB_LINE" >> "$ADB_OUTPUT"
#     #     echo "[INFO] record ADB info to $ADB_OUTPUT"
#     # fi
#     sleep 5    # recommended at least 5 seconds

#     PANE_OUTPUT=$(tmux capture-pane -t "$SESSION_NAME" -p)
#     ADB_LINE=$(echo "$PANE_OUTPUT" | grep "adb connect")

#     if [[ -n "$ADB_LINE" ]]; then
#         echo "$ADB_LINE" >> "$ADB_OUTPUT"
#         echo "[INFO] record ADB info to $ADB_OUTPUT"
#     else
#         echo "[WARN] adb connect line not found"
#     fi


#     i=$((i + 1))
# done

echo "" > "$PID_FILE"

i=1
for URL in "${URLS[@]}"; do
    LOG="$CONNECT_LOG_DIR/session_${i}_${TS}.log"
    SESSION_NAME="anbox_$i"

    # Run anbox-connect inside a tmux session so it stays alive: the ADB bridge
    # over the WebRTC data channel drops if anbox-connect exits. Teardown kills
    # these `anbox_*` sessions.
    tmux new-session -d -s "$SESSION_NAME" "
        echo '[INFO] running anbox-connect $URL';
        anbox-connect \"$URL\" -k 2>&1 | tee -a \"$LOG\";
        echo '[INFO] anbox-connect exited (session kept open)';
        bash
    "

    REAL_PID=$(tmux list-panes -t "$SESSION_NAME" -F '#{pane_pid}')
    echo "[INFO] tmux pane PID = $REAL_PID"
    echo "$REAL_PID" >> "$PID_FILE"

    # anbox-connect prints "adb connect 127.0.0.1:<port>" once the bridge is up
    # but does NOT run it. Poll the pane for the endpoint, then connect so the
    # tenant appears in `adb devices` (which is how the harness discovers it).
    ENDPOINT=""
    for _ in $(seq 1 30); do
        PANE_OUTPUT=$(tmux capture-pane -t "$SESSION_NAME" -p 2>/dev/null)
        ENDPOINT=$(echo "$PANE_OUTPUT" | grep -oE "127\.0\.0\.1:[0-9]+" | head -n1)
        [[ -n "$ENDPOINT" ]] && break
        sleep 2
    done

    if [[ -n "$ENDPOINT" ]]; then
        echo "$ENDPOINT" >> "$ADB_OUTPUT"
        echo "[INFO] adb connect $ENDPOINT"
        adb connect "$ENDPOINT"
    else
        echo "[WARN] no 'adb connect' endpoint found for session $i"
    fi

    i=$((i + 1))
done

# -------------------------------------------------------------
# Clean up this run's temporary per-session logs.
# -------------------------------------------------------------

log "[INFO] cleaning up this run's session logs: session_*_${TS}.log"

rm -f $CONNECT_LOG_DIR/session_*_${TS}.log


echo "[INFO] ALL DONE."
exit 0
