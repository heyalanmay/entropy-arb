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
#
# 代理说明（重要）：
#   引擎的 REST 走 aiohttp（不读代理，永远直连），WS 走 websockets（会读
#   系统/环境代理）。macOS 上如果开了 Clash/Surge 的「系统代理」，websockets
#   会去走 SOCKS，然后报 “requires python-socks” 直接连不上，采集全程 0 行。
#   所以本脚本默认强制 WS 也直连（no_proxy=*），和 REST 保持同一条链路 ——
#   两条腿延迟一致，测出来的基差才是真的。
#   若你所在网络真的必须走代理，用：USE_PROXY=1 bash record.sh 24h
#
# 注意：Mac 合盖休眠会中断采集，跑长任务前先关休眠或接电源。
# ---------------------------------------------------------------------------
set -o pipefail

REPO="${REPO:-$HOME/entropy-arb}"          # 服务器上改这里，或 export REPO=...
SYMBOL="${SYMBOL:-SNDK}"
HEDGE="${HEDGE:-lighter-rh}"
USE_PROXY="${USE_PROXY:-0}"
DURATION="${1:-3.5h}"

PIDFILE="$REPO/logs/record.pid"
LOGFILE="$REPO/logs/record.log"

# ---- 停止 ----
if [[ "$DURATION" == "stop" ]]; then
    for f in "$REPO/logs/record.timer.pid" "$PIDFILE"; do
        if [[ -f "$f" ]] && kill -0 "$(cat "$f")" 2>/dev/null; then
            kill -TERM "$(cat "$f")" 2>/dev/null
        fi
        rm -f "$f"
    done
    sleep 2
    if pgrep -f "main.py --record-only" >/dev/null 2>&1; then
        pkill -f "main.py --record-only"
        sleep 1
    fi
    echo "状态: $(pgrep -f 'main.py --record-only' >/dev/null 2>&1 && echo 仍在运行 || echo 已停止)"
    exit 0
fi

# ---- 前置检查 ----
if [[ ! -d "$REPO" ]]; then
    echo "找不到仓库目录: $REPO"
    echo "  设置: export REPO=/path/to/entropy-arb"
    exit 1
fi
cd "$REPO" || exit 1
if [[ ! -x .venv/bin/python ]]; then
    echo "找不到虚拟环境: $REPO/.venv （先跑 install.sh 或 python3 -m venv .venv）"
    exit 1
fi
if [[ ! -f config.yaml ]]; then
    echo "找不到 config.yaml （把 deploy/entropy-rh/config.rh.yaml 复制过来）"
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
) || { echo "时长格式不对: $DURATION  （用 30m / 3.5h / 24h）"; exit 1; }

if (( SECS < 60 )); then
    echo "时长太短（$SECS 秒），至少 1 分钟才有统计意义"
    exit 1
fi

# ---- 已经在跑？ ----
if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "采集已在运行（PID $(cat "$PIDFILE") ）"
    echo "  看进度: tail -f $LOGFILE"
    echo "  停止:   bash $0 stop"
    exit 0
fi

# ---- 代理处理 ----
if [[ "$USE_PROXY" == "1" ]]; then
    # 走代理：websockets 连 SOCKS 需要 python-socks，缺就装
    if ! .venv/bin/python -c "import python_socks" >/dev/null 2>&1; then
        echo "USE_PROXY=1，正在安装 python-socks（websockets 走 SOCKS 代理的依赖）…"
        .venv/bin/pip install -q "python-socks[asyncio]" || {
            echo "安装失败。改用直连: bash $0 $DURATION"; exit 1; }
    fi
    NETMODE="经代理（USE_PROXY=1）"
else
    # 默认直连：屏蔽环境代理 + macOS 系统代理（urllib 会读 sysconf）
    export no_proxy='*' NO_PROXY='*'
    unset http_proxy https_proxy all_proxy HTTP_PROXY HTTPS_PROXY ALL_PROXY \
          ws_proxy wss_proxy WS_PROXY WSS_PROXY 2>/dev/null || true
    NETMODE="直连（已屏蔽系统/环境代理，与 REST 同链路）"
fi

# ---- 启动 ----
echo "=============================================="
echo " 开始采集溢价数据（不下单）"
echo "=============================================="
echo " 仓库   : $REPO"
echo " 品种   : $SYMBOL   对冲腿: $HEDGE"
echo " 时长   : $DURATION  = $SECS 秒"
echo " 网络   : $NETMODE"
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

# ---- 等 40 秒，确认两条腿真的连上了（不只是进程活着）----
echo
echo " PID $PID ，等待 40 秒确认两腿连通…"
CONNECTED=0
for i in $(seq 1 40); do
    sleep 1
    if ! kill -0 "$PID" 2>/dev/null; then
        echo
        echo "X 进程退出了，日志如下:"
        tail -25 "$LOGFILE"
        rm -f "$PIDFILE"
        exit 1
    fi
    # 两条腿都出现 connected 才算通
    if [[ $(grep -c "connected" "$LOGFILE" 2>/dev/null) -ge 2 ]]; then
        CONNECTED=1
        printf "\r   %2d 秒 —— 两腿已连接        \n" "$i"
        break
    fi
    printf "\r   %2d 秒…" "$i"
done
echo

if (( CONNECTED == 0 )); then
    echo "X 40 秒内 WS 没连上，采集不会产生数据。最近的错误:"
    grep -m3 "ws error" "$LOGFILE" 2>/dev/null | sed 's/^/    /'
    echo
    if grep -q "python-socks" "$LOGFILE" 2>/dev/null; then
        echo "诊断: websockets 被系统代理（Clash/Surge 的 SOCKS）劫持了。"
        echo "  本脚本已尝试屏蔽，若仍报此错说明用的是旧版脚本 —— 重新拉一次:"
        echo "  curl -fsSL https://raw.githubusercontent.com/heyalanmay/entropy-arb/main/deploy/entropy-rh/record.sh -o $0"
    else
        echo "诊断: 网络到 Hyperliquid / Lighter 不通。若本机必须走代理，试:"
        echo "  USE_PROXY=1 bash $0 $DURATION"
    fi
    bash "$0" stop >/dev/null 2>&1
    exit 1
fi

# ---- 等第一行落盘（每分钟写一次，最多等 90 秒）----
echo " 等第一行数据落盘（每分钟写一次）…"
for i in $(seq 1 90); do
    sleep 1
    if [[ -f logs/minutes.csv ]] && [[ $(wc -l < logs/minutes.csv) -ge 2 ]]; then
        echo "OK 采集正常，已写入 $(wc -l < logs/minutes.csv) 行"
        echo "   表头: $(head -1 logs/minutes.csv)"
        echo "   最新: $(tail -1 logs/minutes.csv)"
        break
    fi
    printf "\r   %2d 秒…" "$i"
done
echo

echo "----------------------------------------------"
echo " 看进度: tail -f $LOGFILE"
echo " 看数据: tail -3 $REPO/logs/minutes.csv"
echo " 提前停: bash $0 stop"
echo " 跑完后: cd $REPO && .venv/bin/python tools/analyze.py --fees-bps 4.5"
echo "----------------------------------------------"
echo
echo "注意: Mac 合盖会休眠中断采集。跑长任务请接电源 + 关休眠:"
echo "      系统设置 → 锁定屏幕 / 电池 → 关闭休眠"
