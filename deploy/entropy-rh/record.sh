#!/usr/bin/env bash
# ---------------------------------------------------------------------------
# 采集溢价数据（不下单、不需要密钥）
#
#   bash record.sh              # 默认采 3.5 小时（覆盖一个美股盘中时段）
#   bash record.sh 30m          # 采 30 分钟（快速试一下）
#   bash record.sh 24h          # 采 24 小时（覆盖完整昼夜周期）
#   bash record.sh stop         # 提前停止
#
# 数据落在 REPO/logs/minutes.csv，用 tools/analyze.py 分析。
# 进程用 nohup 脱离终端 —— 关掉终端窗口也会继续跑。
# 注意：Mac 合盖休眠会中断采集，跑长任务前先关休眠或接电源。
# ---------------------------------------------------------------------------
set -o pipefail

REPO="${REPO:-$HOME/entropy-arb}"          # 服务器上改这里，或 export REPO=...
SYMBOL="${SYMBOL:-SNDK}"
HEDGE="${HEDGE:-lighter-rh}"
DURATION="${1:-3.5h}"

PIDFILE="$REPO/logs/record.pid"
LOGFILE="$REPO/logs/record.log"

# ---- 停止 ----
if [[ "$DURATION" == "stop" ]]; then
    if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
        kill -TERM "$(cat "$PIDFILE")" && echo "已发送停止信号，等待优雅退出…"
        sleep 3
        echo "状态：$(kill -0 "$(cat "$PIDFILE")" 2>/dev/null && echo 仍在运行 || echo 已停止)"
    else
        echo "没有正在运行的采集进程"
    fi
    rm -f "$PIDFILE"
    exit 0
fi

# ---- 前置检查 ----
if [[ ! -d "$REPO" ]]; then
    echo "找不到仓库目录：$REPO"
    echo "  设置：export REPO=/path/to/entropy-arb"
    exit 1
fi
cd "$REPO" || exit 1
if [[ ! -x .venv/bin/python ]]; then
    echo "找不到虚拟环境：$REPO/.venv（先跑 install.sh 或 python3 -m venv .venv）"
    exit 1
fi
if [[ ! -f config.yaml ]]; then
    echo "找不到 config.yaml（把 deploy/entropy-rh/config.rh.yaml 复制过来）"
    exit 1
fi
mkdir -p logs

# ---- 时长换算成秒（用 python 解析，避免 bash 子串在 set -u 下的坑）----
SECS=$(python3 - <<'PY' "$DURATION"
import sys
d = sys.argv[1]
u = d[-1].lower()
n = d[:-1]
try:
    f = float(n)
except ValueError:
    sys.exit(2)
table = {'h': int(f * 3600), 'm': int(f * 60), 's': int(f)}
if u not in table:
    sys.exit(3)
print(table[u])
PY
) || { echo "时长格式不对：$DURATION（用 30m / 3.5h / 24h）"; exit 1; }

if (( SECS < 60 )); then
    echo "时长太短（$SECS 秒），至少 1 分钟才有统计意义"
    exit 1
fi

# ---- 已经在跑？ ----
if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "采集已在运行（PID $(cat "$PIDFILE")）"
    echo "  看进度：tail -f $LOGFILE"
    echo "  停止：  bash $0 stop"
    exit 0
fi

# ---- 启动 ----
echo "=============================================="
echo " 开始采集溢价数据（不下单）"
echo "=============================================="
echo " 仓库   : $REPO"
echo " 品种   : $SYMBOL   对冲腿: $HEDGE"
echo " 时长   : $DURATION（$SECS 秒）"
echo " 数据   : $REPO/logs/minutes.csv"
echo " 日志   : $LOGFILE"
echo "=============================================="

nohup .venv/bin/python main.py \
    --record-only --no-dashboard \
    --symbol "$SYMBOL" --hedge "$HEDGE" \
    > "$LOGFILE" 2>&1 &

PID=$!
echo "$PID" > "$PIDFILE"

# 到点自动停（脱离终端，所以要用独立后台进程计时）
( sleep "$SECS"; kill -TERM "$PID" 2>/dev/null; rm -f "$PIDFILE" ) &
echo "$!" > "$REPO/logs/record.timer.pid"

# ---- 等 20 秒看它活没活 ----
echo
echo "PID $PID，等待 20 秒确认连通…"
for i in $(seq 1 20); do
    sleep 1
    if ! kill -0 "$PID" 2>/dev/null; then
        echo
        echo "X 进程退出了，日志如下："
        tail -25 "$LOGFILE"
        rm -f "$PIDFILE"
        exit 1
    fi
    printf "\r   %2d 秒…" "$i"
done
echo
echo

if [[ -f logs/minutes.csv ]]; then
    echo "OK 已在采集，已写入 $(wc -l < logs/minutes.csv) 行"
    echo "   表头：$(head -1 logs/minutes.csv)"
else
    echo "进程活着，但还没落盘（minutes.csv 每分钟写一次，再等等）"
fi

echo
echo "----------------------------------------------"
echo " 看进度： tail -f $LOGFILE"
echo " 看数据： tail -3 $REPO/logs/minutes.csv"
echo " 提前停： bash $0 stop"
echo " 跑完后： cd $REPO && .venv/bin/python tools/analyze.py --fees-bps 4.5"
echo "----------------------------------------------"
echo
echo "⚠️  Mac 合盖会休眠中断采集。跑长任务请接电源 + 关休眠："
echo "    系统设置 → 锁定屏幕 / 电池 → 关闭休眠"
