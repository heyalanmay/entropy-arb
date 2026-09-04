#!/usr/bin/env bash
# =============================================================================
# 在 VPS 上执行 —— 从已上传的压缩包部署采集（不依赖 GitHub 出网）
#
# 由 push-to-vps.sh 自动上传并调用，一般不需要手动跑。
#   bash /tmp/vps-setup-remote.sh 72h
#   SYMBOL=ANTH bash /tmp/vps-setup-remote.sh 24h
#
# 进程管理用 PID 文件，不用 pgrep/pkill 匹配命令行 —— 后者的模式
# "entropy-rh/main.py" 永远匹配不上真实命令行
# （实际是 ".../entropy-rh/.venv/bin/python main.py ..."），
# 会导致看门狗每 30 秒误判"进程没了"而反复起新进程。
# =============================================================================
set -euo pipefail

DUR="${1:-72h}"
SYMBOL="${SYMBOL:-SNDK}"
HEDGE="${HEDGE:-lighter-rh}"
DIR="${DIR:-$HOME/entropy-rh}"
TARBALL="${TARBALL:-/tmp/entropy-rh.tgz}"

RECPID="$DIR/logs/record.pid"
WATPID="$DIR/logs/watch.pid"
CSV="$DIR/logs/minutes.csv"
RECLOG="$DIR/logs/record.log"
WATLOG="$DIR/logs/watch.log"

echo "=============================================="
echo " 部署采集（只采集 / 不下单 / 不碰实盘）"
echo " 目录   : $DIR"
echo " 品种   : $SYMBOL   对冲腿: $HEDGE"
echo " 时长   : $DUR"
echo "=============================================="

# ---- 0. 时长 ----
UNIT="${DUR: -1}"
NUM="${DUR%?}"
case "$UNIT" in
    h) SECS=$(awk "BEGIN{printf \"%d\", $NUM*3600}") ;;
    m) SECS=$(awk "BEGIN{printf \"%d\", $NUM*60}")   ;;
    s) SECS="$NUM" ;;
    *) echo "时长格式不对: $DUR （用 30m / 24h / 72h）"; exit 1 ;;
esac
if (( SECS < 3600 )); then
    echo "提示: 不足 1 小时。要覆盖美股时段（北京 21:30-04:00）建议 >= 24h。"
fi

# ---- 1. 解包 ----
[[ -f "$TARBALL" ]] || { echo "X 找不到 $TARBALL"; exit 1; }
mkdir -p "$DIR"
cd "$DIR"
tar xzf "$TARBALL"
mkdir -p logs
echo ">> 代码已就位"

# ---- 2. Python ----
PY=""
for c in python3.12 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
[[ -n "$PY" ]] || { echo "X 找不到 python3"; exit 1; }
echo ">> Python: $($PY -V 2>&1)"

# ---- 3. 虚拟环境（Ubuntu 常见坑：ensurepip 缺失）----
if [[ ! -x .venv/bin/python ]]; then
    echo ">> 建虚拟环境"
    if ! $PY -m venv .venv 2>/tmp/venv.err; then
        echo "X venv 创建失败:"; tail -5 /tmp/venv.err
        echo
        echo "Ubuntu 常见原因 —— 缺 python3-venv，先执行："
        echo "  sudo apt update && sudo apt install -y python3-venv python3-pip"
        echo "然后重跑本脚本。"
        exit 1
    fi
fi

# ---- 4. 依赖 ----
echo ">> 装依赖（record-only 只需基础依赖）"
.venv/bin/pip install -q --upgrade pip 2>/dev/null || echo "   (pip 升级跳过)"
if [[ -n "${PIP_INDEX:-}" ]]; then
    .venv/bin/pip install -q -i "$PIP_INDEX" -r requirements.txt
else
    .venv/bin/pip install -q -r requirements.txt
fi
echo ">> 依赖 OK"

# ---- 5. 配置 ----
if [[ ! -f config.yaml ]]; then
    cp deploy/entropy-rh/config.rh.yaml config.yaml
    echo ">> 已写 config.yaml"
else
    echo ">> config.yaml 已存在，保留"
fi

# ---- 6. 停掉旧的（按 PID 文件，不匹配命令行）----
for f in "$WATPID" "$RECPID"; do
    if [[ -s "$f" ]] && kill -0 "$(cat "$f")" 2>/dev/null; then
        kill -TERM "$(cat "$f")" 2>/dev/null || true
    fi
    rm -f "$f"
done
sleep 1
[[ -f "$RECLOG" ]] && mv "$RECLOG" "$RECLOG.$(date +%s)" || true

# ---- 7. 看门狗（PID 文件管理）----
cat > "$DIR/record-watch.sh" <<'WATCHEOF'
#!/usr/bin/env bash
# 看门狗 —— 采集进程没了、或 CSV 超过 300 秒没更新，就重启。
# 全部走 PID 文件，不用 pgrep（匹配不准会反复起进程）。
set -u
DIR="__DIR__"
RECPID="$DIR/logs/record.pid"
CSV="$DIR/logs/minutes.csv"
RECLOG="$DIR/logs/record.log"
WATLOG="$DIR/logs/watch.log"
SYMBOL="__SYMBOL__"
HEDGE="__HEDGE__"
cd "$DIR"

start() {
    nohup .venv/bin/python main.py --record-only --no-dashboard \
        --symbol "$SYMBOL" --hedge "$HEDGE" >> "$RECLOG" 2>&1 &
    echo $! > "$RECPID"
    echo "[$(date '+%Y-%m-%dT%H:%M:%S%z')] 启动采集 pid=$(cat "$RECPID")" >> "$WATLOG"
}
alive() {
    [[ -s "$RECPID" ]] && kill -0 "$(cat "$RECPID")" 2>/dev/null
}

while true; do
    if ! alive; then
        echo "[$(date '+%Y-%m-%dT%H:%M:%S%z')] 采集进程不在，重启" >> "$WATLOG"
        start
        sleep 30
        continue
    fi
    if [[ -f "$CSV" ]]; then
        age=$(( $(date +%s) - $(stat -c %Y "$CSV") ))
        if (( age > 300 )); then
            echo "[$(date '+%Y-%m-%dT%H:%M:%S%z')] CSV 已 ${age}s 未更新，判定卡死，重启" >> "$WATLOG"
            kill -TERM "$(cat "$RECPID")" 2>/dev/null || true
            sleep 3
            start
            sleep 30
            continue
        fi
    fi
    sleep 60
done
WATCHEOF
# 注入实际参数（用 | 作分隔符，路径里可能有 /）
sed -i.bak -e "s|__DIR__|$DIR|" -e "s|__SYMBOL__|$SYMBOL|" -e "s|__HEDGE__|$HEDGE|" \
    "$DIR/record-watch.sh"
rm -f "$DIR/record-watch.sh.bak"
chmod +x "$DIR/record-watch.sh"
bash -n "$DIR/record-watch.sh" || { echo "X 看门狗脚本生成有误"; exit 1; }

# 先**直接**启动采集（不经过看门狗）—— 这样任何启动失败都能立刻拿到真实报错，
# 而不是等看门狗在那儿空转、健康检查只看到个空日志。
start_recorder() {
    nohup .venv/bin/python main.py --record-only --no-dashboard \
        --symbol "$SYMBOL" --hedge "$HEDGE" >> "$RECLOG" 2>&1 &
    echo $! > "$RECPID"
}
start_recorder
echo ">> 采集已启动 (pid $(cat "$RECPID"))"

# ---- 8. 连通自检（失败就前台跑一次，把真实报错打出来）----
echo
echo "等待 60 秒确认两腿连通…"
OK=0
for i in $(seq 1 60); do
    sleep 1
    if [[ $(grep -c "connected" "$RECLOG" 2>/dev/null) -ge 2 ]]; then
        echo "  ${i}s —— 两腿已连接"; OK=1; break
    fi
    printf "\r   %2d 秒…" "$i"
done
echo

if (( OK == 0 )); then
    echo "X 60 秒内没连上。后台日志尾部:"
    tail -20 "$RECLOG" 2>/dev/null
    echo
    echo "---- 前台直跑 12 秒，抓真实报错 ----"
    kill -TERM "$(cat "$RECPID")" 2>/dev/null || true
    sleep 2
    .venv/bin/python main.py --record-only --no-dashboard \
        --symbol "$SYMBOL" --hedge "$HEDGE" > /tmp/entropy-fore.log 2>&1 &
    FPID=$!
    sleep 12
    kill -TERM "$FPID" 2>/dev/null || true
    sleep 1
    tail -30 /tmp/entropy-fore.log
    echo "----------------------------------"
    echo
    echo "排查:"
    echo "  1) 出网被限？ curl -sI https://api.hyperliquid.xyz/info"
    echo "  2) 需代理？   HTTPS_PROXY=http://... PIP_INDEX=... bash $0 $DUR"
    rm -f "$RECPID"
    exit 1
fi

# ---- 8b. 采集确认健康，交给看门狗守着 ----
nohup bash "$DIR/record-watch.sh" >/dev/null 2>&1 &
echo $! > "$WATPID"
sleep 2
if [[ -s "$WATPID" ]] && kill -0 "$(cat "$WATPID")" 2>/dev/null; then
    echo ">> 看门狗已启动 (pid $(cat "$WATPID"))"
else
    echo "!! 看门狗没起来（不影响已运行的采集，但没有自动重启保护）"
fi

# ---- 8. 到点自停 ----
cat > "$DIR/record-stop-timer.sh" <<STOPEOF
#!/usr/bin/env bash
sleep $SECS
WAT="$DIR/logs/watch.pid"; REC="$DIR/logs/record.pid"
[[ -s "\$WAT" ]] && kill -TERM "\$(cat "\$WAT")" 2>/dev/null
[[ -s "\$REC" ]] && kill -TERM "\$(cat "\$REC")" 2>/dev/null
echo "[\$(date '+%Y-%m-%dT%H:%M:%S%z')] 到点，采集已停（共 \$(wc -l < $DIR/logs/minutes.csv 2>/dev/null || echo 0) 行）" >> "$DIR/logs/watch.log"
rm -f "\$WAT" "\$REC"
STOPEOF
chmod +x "$DIR/record-stop-timer.sh"
# 不用 setsid —— 它是 Linux util-linux 专有，缺失时会静默失败
nohup bash "$DIR/record-stop-timer.sh" >/dev/null 2>&1 &
echo ">> 已设定 $DUR 后自动停止"

# ---- 9. 落盘自检（每分钟写一行）----
for i in $(seq 1 90); do
    sleep 1
    if [[ -f "$CSV" ]] && [[ $(wc -l < "$CSV") -ge 2 ]]; then
        echo "OK 采集正常，已写入 $(wc -l < "$CSV") 行"
        echo "   最新: $(tail -1 "$CSV")"
        break
    fi
    printf "\r   %2d 秒…" "$i"
done
echo

echo "=============================================="
echo " 部署完成，可以断开 SSH（nohup + 看门狗）。"
echo " 行数  : wc -l $CSV"
echo " 进度  : tail -f $RECLOG"
echo " 看门狗: cat $WATLOG"
echo " 提前停: kill \$(cat $WATPID); kill \$(cat $RECPID)"
echo "=============================================="
