#!/usr/bin/env bash
# =============================================================================
# entropy-arb 启停脚本 —— Entropy(io) × Lighter Robinhood 链 / SNDK
#
#   ./run.sh record    仅采集盘口数据（不下单，不需要密钥）
#   ./run.sh live      实盘交易（会发真实订单，需要 .env）
#   ./run.sh stop      优雅停止（SIGTERM → 引擎自行收尾）
#   ./run.sh status    查看进程、持仓日志尾部
#   ./run.sh logs      实时跟踪日志
#
# 放在 ~/entropy-arb/ 下使用。
# =============================================================================
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$DIR"

SYMBOL="${SYMBOL:-SNDK}"
HEDGE="${HEDGE:-lighter-rh}"
PY="${PY:-python3}"
LOG_DIR="$DIR/logs"
PID_FILE="$DIR/.bot.pid"
mkdir -p "$LOG_DIR"

is_running() {
    [[ -f "$PID_FILE" ]] && kill -0 "$(cat "$PID_FILE")" 2>/dev/null
}

do_start() {
    local mode="$1"
    if is_running; then
        echo "已经在运行了：pid=$(cat "$PID_FILE")。先 ./run.sh stop"
        exit 1
    fi

    local cmd=("$PY" main.py --symbol "$SYMBOL" --hedge "$HEDGE" --no-dashboard)
    [[ "$mode" == "record" ]] && cmd+=(--record-only)

    if [[ "$mode" == "live" ]]; then
        echo "=============================================="
        echo "  即将启动【实盘】模式，会发送真实订单"
        echo "  品种 $SYMBOL   对冲腿 $HEDGE"
        echo "  配置 $DIR/config.yaml"
        echo "=============================================="
        read -r -p "确认请输入 yes（其他任意输入取消）: " ans
        [[ "$ans" == "yes" ]] || { echo "已取消"; exit 1; }
    fi

    local out="$LOG_DIR/stdout_${mode}_$(date +%Y%m%d_%H%M%S).log"
    nohup "${cmd[@]}" >> "$out" 2>&1 &
    echo $! > "$PID_FILE"
    sleep 3
    if is_running; then
        echo "已启动 [$mode] pid=$(cat "$PID_FILE")"
        echo "输出: $out"
        echo "引擎日志: $LOG_DIR/engine.log"
        tail -n 15 "$out"
    else
        echo "启动失败，查看输出: $out"
        tail -n 40 "$out"
        rm -f "$PID_FILE"
        exit 1
    fi
}

case "${1:-}" in
    record) do_start record ;;
    live)   do_start live ;;
    stop)
        if is_running; then
            pid=$(cat "$PID_FILE")
            echo "停止 pid=$pid ..."
            kill -TERM "$pid"
            for _ in $(seq 1 20); do
                kill -0 "$pid" 2>/dev/null || break
                sleep 1
            done
            kill -0 "$pid" 2>/dev/null && { echo "未响应，强制结束"; kill -9 "$pid"; }
            rm -f "$PID_FILE"
            echo "已停止"
        else
            echo "没有运行中的进程"
            rm -f "$PID_FILE"
        fi
        ;;
    status)
        if is_running; then
            echo "运行中 pid=$(cat "$PID_FILE")  ($(ps -o etime= -p "$(cat "$PID_FILE")" | tr -d ' '))"
        else
            echo "未运行"
        fi
        echo "--- 最近成交 ---"
        tail -n 5 "$LOG_DIR/trades.csv" 2>/dev/null || echo "(无 trades.csv)"
        echo "--- 引擎日志尾部 ---"
        tail -n 12 "$LOG_DIR/engine.log" 2>/dev/null || echo "(无 engine.log)"
        ;;
    logs)
        tail -f "$LOG_DIR/engine.log" ;;
    *)
        sed -n '2,12p' "${BASH_SOURCE[0]}"
        exit 1 ;;
esac
