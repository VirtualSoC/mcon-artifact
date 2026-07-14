#!/bin/bash
# =============================================================
# anbox-test.sh  (2025-12-08 最新增强版)
#
# 用法：
#   启动容器并连接： ./anbox_test.sh start <N> <adb_output_file>
#   关闭 ADB：        ./anbox_test.sh stop-adb file_name
#   停止容器：        ./anbox_test.sh stop-containers file_name
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

# 时间戳
TS=$(date +"%m%d-%H%M")

PID_FILE="$CONNECT_LOG_DIR/anbox_connect_pids_${TS}.txt"
CONTAINER_FILE="$CONNECT_LOG_DIR/anbox_containers_${TS}.txt"

# 日志文件
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


# 将所有输出重定向进日志，同时显示到屏幕
exec > >(tee -a "$MAIN_LOG") 2>&1

echo "[INFO] 使用日志文件：$MAIN_LOG"

# -------------------------------------------------------------
# 功能：停止所有 ADB
# -------------------------------------------------------------
stop_adb() {
    local file="$1"

    if [[ -z "$file" ]]; then
        echo "[ERROR] 请提供 PID 文件名，例如： ./script.sh stop-adb pidfile.txt"
        exit 1
    fi

    if [[ ! -f "$file" ]]; then
        echo "[WARN] 指定的 PID 文件不存在: $file"
        exit 1
    fi

    echo "[INFO] 正在停止 adb 连接（来自文件: $file）..."

    while read pid; do
        if [[ -n "$pid" ]]; then
            if kill -0 "$pid" 2>/dev/null; then
                echo "Killing PID $pid"
                kill -9 "$pid"
            else
                echo "PID $pid 不存在或无法访问"
            fi
        fi
    done < "$file"

    rm -f "$file"
    echo "[INFO] 所有 adb 已关闭。"
    exit 0
}

# -------------------------------------------------------------
# 功能：停止容器
# -------------------------------------------------------------
stop_containers() {
    FILE="$2"   # 读取用户传入的文件名

    if [[ -z "$FILE" ]]; then
        echo "[ERROR] 请提供容器文件名，例如："
        echo "  ./anbox-test.sh stop-containers anbox_containers_1206-2102.txt"
        exit 1
    fi

    # 如果没有绝对路径，则补上 BASE_DIR
    if [[ "$FILE" != /* ]]; then
        FILE="$BASE_DIR/$FILE"
    fi

    if [[ ! -f "$FILE" ]]; then
        echo "[ERROR] 找不到容器文件：$FILE"
        exit 1
    fi

    echo "[INFO] 使用容器文件：$FILE"
    echo "[INFO] 停止并删除以下容器："
    cat "$FILE"

    while read ID; do
        [[ -z "$ID" ]] && continue
        echo "[INFO] 停止容器 $ID"
        amc_cmd stop "$ID" </dev/null
        echo "[INFO] 删除容器 $ID"
        amc_cmd delete "$ID" --yes </dev/null
    done < "$FILE"

    echo "[INFO] 容器 stop and delete 完成。"
    exit 0
}


if [[ "$1" == "stop-adb" ]]; then
    stop_adb "$2"
fi
if [[ "$1" == "stop-containers" ]]; then stop_containers "$@"; fi


# -------------------------------------------------------------
# START 模式
# -------------------------------------------------------------
if [[ "$1" != "start" ]]; then
    echo "用法："
    echo "  ./anbox_test.sh start <N> <adb_output_file>"
    echo "  ./anbox_test.sh stop-adb"
    echo "  ./anbox_test.sh stop-containers"
    exit 1
fi

N="$2"
ADB_OUTPUT="${3:-$CONNECT_LOG_DIR/anbox_adb_${N}_${TS}.txt}"

# 创建必要文件
touch "$PID_FILE" "$CONTAINER_FILE" "$ADB_OUTPUT"

echo "[INFO] 启动流程 N=$N"
echo "" > "$CONTAINER_FILE"
echo "" > "$ADB_OUTPUT"

# -------------------------------------------------------------
# PART 1：创建容器
# -------------------------------------------------------------

# -------------------------------------------------------------
# [新增] 后台监控 amc ls（1s 一次），记录 running 容器
# -------------------------------------------------------------

# LS_LOG="$CONNECT_LOG_DIR/anbox_running_${TS}.log"
# mkdir -p "$CONNECT_LOG_DIR"
# : > "$LS_LOG"

# log "[INFO] 启动 amc ls 后台监控（1s 轮询） -> $LS_LOG"

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
# log "[INFO] amc ls 监控线程 PID = $LS_MONITOR_PID"

LS_LOG="$CONNECT_LOG_DIR/anbox_running_${TS}.log"
mkdir -p "$CONNECT_LOG_DIR"
: > "$LS_LOG"

log "[INFO] 启动 amc ls 后台监控（仅记录新进入 running 的容器） -> $LS_LOG"

(
# 记录“上一轮”的 running 容器集合
PREV_RUNNING_IDS=""

while true; do
    # 取当前轮所有 running 容器 ID（你验证成功的正则规则）
    CURR_RUNNING_IDS=$(amc_cmd ls 2>/dev/null \
        | grep ' running ' \
        | grep -oE '\|[[:space:]]+[a-z0-9]{20,}[[:space:]]+\|' \
        | awk -F'|' '{gsub(/ /, "", $2); print $2}')

    # 对比：找出“本轮新增进入 running 的容器”
    for ID in $CURR_RUNNING_IDS; do
        if ! echo "$PREV_RUNNING_IDS" | grep -qw "$ID"; then
            echo "$(date '+%F %T.%3N') running $ID" >> "$LS_LOG"
        fi
    done

    # 更新上一轮 running 集合
    PREV_RUNNING_IDS="$CURR_RUNNING_IDS"

    sleep 1
done
) &

LS_MONITOR_PID=$!
log "[INFO] amc ls 监控线程 PID = $LS_MONITOR_PID"


CONTAINER_IDS=()

for ((i=1; i<=N; i++)); do
    START_TS=$(date '+%F %T')
    log "[INFO] 开始创建第 $i 个容器..."

            OUT=$(amc_cmd launch jammy:android15:amd64 \
                --no-wait \
            --enable-graphics --gpu-type nvidia --enable-streaming \
            --memory 3GB --disk-size 10GB --cpus 1 2>&1)

    END_TS=$(date '+%F %T')
    log "[INFO] 第 $i 个容器创建命令返回（开始=$START_TS，结束=$END_TS）"

    if echo "$OUT" | grep -qi "error"; then
        log "[ERROR] 创建失败："
        log "$OUT"
        continue
    fi

    ID=$(echo "$OUT" | grep -Eo "^[a-z0-9]{20,}" | tail -n 1)

    if [[ -z "$ID" ]]; then
        log "[ERROR] 容器 ID 解析失败"
        log "$OUT"
        continue
    fi

    log "[INFO] 创建成功：$ID"
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
#     echo "[INFO] 创建第 $i 个容器..."

#     OUT=$(multipass exec "$AMBOX_VM" -- \
#          bash -c "sudo amc launch jammy:android15:amd64 \
#          --enable-graphics --gpu-type nvidia --enable-streaming \
#          --memory 3GB --disk-size 5GB --cpus 2" 2>&1)

#     if echo "$OUT" | grep -qi "error"; then
#         echo "[ERROR] 创建失败："
#         echo "$OUT"
#         continue
#     fi

#     # 取最后一段 ID
#     ID=$(echo "$OUT" | grep -Eo "^[a-z0-9]{20,}" | tail -n 1)

#     if [[ -z "$ID" ]]; then
#         echo "[ERROR] 解析失败"
#         echo "$OUT"
#         continue
#     fi

#     echo "[INFO] 创建成功：$ID"
#     echo "$ID" >> "$CONTAINER_FILE"
#     CONTAINER_IDS+=("$ID")
# done

# -------------------------------------------------------------
# PART 2：等待 running
# -------------------------------------------------------------
echo "[INFO] 等待容器 running..."

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
        echo "[INFO] 所有容器已 running"
        break
    fi

    if (( WAIT >= MAX_WAIT )); then
        echo "[ERROR] 仍有容器未 running"
        if kill -0 "$LS_MONITOR_PID" 2>/dev/null; then
            kill -9 "$LS_MONITOR_PID"
        fi
        exit 1
    fi

    sleep 3
    WAIT=$((WAIT+3))
done

# -------------------------------------------------------------
# [新增] 关闭 amc ls 监控线程
# -------------------------------------------------------------

sleep 5

if kill -0 "$LS_MONITOR_PID" 2>/dev/null; then
    log "[INFO] 停止 amc ls 监控线程 PID=$LS_MONITOR_PID"
    kill -9 "$LS_MONITOR_PID"
fi


# -------------------------------------------------------------
# PART 3：session 提取
# -------------------------------------------------------------
SESSION_IDS=()

for ID in "${CONTAINER_IDS[@]}"; do
    LINE=$(amc_cmd ls | grep "$ID")
    SESSION=$(echo "$LINE" | grep -o "session=[a-z0-9]*" | cut -d= -f2)
    SESSION_IDS+=("$SESSION")
    echo "[INFO] $ID session=$SESSION"
done

# -------------------------------------------------------------
# PART 4：获取 URL
# -------------------------------------------------------------
URLS=()

for S in "${SESSION_IDS[@]}"; do
    OUT=$(gateway_share "$S")

    URL=$(echo "$OUT" | grep -Eo "https://[^ ]+")
    URLS+=("$URL")
    echo "[INFO] session=$S URL=$URL"
done

# -------------------------------------------------------------
# PART 5：tmux 启动 anbox-connect（自动 yes）
# -------------------------------------------------------------

# echo "" > "$PID_FILE"

# i=1
# for URL in "${URLS[@]}"; do
#     LOG="$CONNECT_LOG_DIR/session_${i}_${TS}.log"
#     SESSION_NAME="anbox_$i"

#     #################################################################
#     # 1. 创建 tmux 会话并在其中运行 anbox-connect
#     #################################################################
#     # # tmux new-session -d -s "$SESSION_NAME" "
#     # #     echo '[INFO] 运行 anbox-connect $URL';
#     # #     anbox-connect $URL -k | tee -a \"$LOG\";
#     # #     echo '[INFO] 执行完成（tmux 会话中保持打开）';
#     # #     bash
#     # # "
#     tmux new-session -d -s "$SESSION_NAME" "
#         echo '[INFO] 运行 anbox-connect $URL';
#         anbox-connect \"$URL\" -k 2>&1 | tee -a \"$LOG\";
#         echo '[INFO] 执行完成（tmux 会话中保持打开）';
#         bash
#     "

#     # SESSION_NAME="anbox_$i"

#     # tmux new-session -d -s "$SESSION_NAME" "
#     #     echo '[INFO] 运行 anbox-connect $URL';
#     #     anbox-connect \"$URL\" -k 2>&1;
#     #     echo '[INFO] 执行完成（tmux 会话中保持打开）';
#     #     bash
#     # "


#     #################################################################
#     # 2. 获取 tmux 的真正 PID（用于 kill）
#     #################################################################
#     REAL_PID=$(tmux list-panes -t "$SESSION_NAME" -F '#{pane_pid}')
#     echo "[INFO] 实际 PID = $REAL_PID"
#     echo "$REAL_PID" >> "$PID_FILE"


#     #################################################################
#     # 3. 向 tmux 注入按键：Left + Enter（选择 yes）
#     #################################################################
#     tmux send-keys -t "$SESSION_NAME" Left
#     tmux send-keys -t "$SESSION_NAME" Enter

#     #################################################################
#     # 4. 收集 adb connect 行
#     #################################################################
#     # sleep 2
#     # PANE_OUTPUT=$(tmux capture-pane -t "$SESSION_NAME" -p)
#     # ADB_LINE=$(echo "$PANE_OUTPUT" | grep "adb connect")

#     # if [[ -n "$ADB_LINE" ]]; then
#     #     echo "$ADB_LINE" >> "$ADB_OUTPUT"
#     #     echo "[INFO] 记录 ADB 信息到 $ADB_OUTPUT"
#     # fi
#     sleep 5    # 建议至少 5 秒

#     PANE_OUTPUT=$(tmux capture-pane -t "$SESSION_NAME" -p)
#     ADB_LINE=$(echo "$PANE_OUTPUT" | grep "adb connect")

#     if [[ -n "$ADB_LINE" ]]; then
#         echo "$ADB_LINE" >> "$ADB_OUTPUT"
#         echo "[INFO] 记录 ADB 信息到 $ADB_OUTPUT"
#     else
#         echo "[WARN] 未找到 adb connect 行"
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
