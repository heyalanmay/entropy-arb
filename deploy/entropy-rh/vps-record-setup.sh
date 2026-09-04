#!/usr/bin/env bash
# =============================================================================
# 在 VPS 上一键部署「只采集、不下单」—— 24/7 不中断
#
# 为什么必须搬到服务器上跑：
#   Mac 合盖/休眠会冻结进程；而且采集进程一旦被 kill，recorder 持有已打开的
#   文件句柄，期间的数据全部丢失（写完就随进程消失，磁盘上看不到）。
#   采集需要连续 24~72 小时覆盖完整美股时段，笔记本扛不住。
#
# 安全性：
#   · 只用 --record-only，**不下单、不需要任何 API 密钥**
#   · 装到独立目录 ~/entropy-rh，**完全不碰**正在跑的 ~/entropy-arb 实盘
#   · 代码直接 clone 公开 fork，不传密钥
#
# 用法（在 VPS 上执行）：
#   curl -fsSL https://raw.githubusercontent.com/heyalanmay/entropy-arb/main/deploy/entropy-rh/vps-record-setup.sh | bash
#   curl -fsSL ... | bash -s -- 72h        # 指定时长
#   SYMBOL=ANTH bash -s                    # 换品种
# =============================================================================
set -euo pipefail

DUR="${1:-72h}"
SYMBOL="${SYMBOL:-SNDK}"
HEDGE="${HEDGE:-lighter-rh}"
DIR="$HOME/entropy-rh"
RAW="https://raw.githubusercontent.com/heyalanmay/entropy-arb/main/deploy/entropy-rh"

echo "=============================================="
echo " 部署采集（只采集 / 不下单 / 不碰实盘）"
echo " 目录   : $DIR"
echo " 品种   : $SYMBOL   对冲腿: $HEDGE"
echo " 时长   : $DUR"
echo "=============================================="

# ---- 0. 时长解析（bash 原生，避免依赖 python 的 heredoc 解析）----
UNIT="${DUR: -1}"
NUM="${DUR%?}"
case "$UNIT" in
    h) SECS=$(awk "BEGIN{printf \"%d\", $NUM*3600}") ;;
    m) SECS=$(awk "BEGIN{printf \"%d\", $NUM*60}")   ;;
    s) SECS="$NUM" ;;
    *) echo "时长格式不对: $DUR （用 30m / 24h / 72h）"; exit 1 ;;
esac
echo " 秒数   : $SECS"

# ---- 1. 代码 ----
if [[ -d "$DIR/.git" ]]; then
    echo ">> 目录已存在，更新代码"
    git -C "$DIR" pull --ff-only -q || echo "   (pull 失败，沿用现有代码)"
else
    echo ">> clone 代码"
    git clone -q https://github.com/heyalanmay/entropy-arb.git "$DIR"
fi
cd "$DIR"

# ---- 2. 虚拟环境 ----
if [[ ! -x .venv/bin/python ]]; then
    echo ">> 建虚拟环境"
    python3 -m venv .venv
fi
echo ">> 装依赖（record-only 只需要基础依赖，几十秒）"
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

# ---- 3. 配置（record-only 不需要 .env / 密钥）----
if [[ ! -f config.yaml ]]; then
    cp deploy/entropy-rh/config.rh.yaml config.yaml
    echo ">> 已写 config.yaml"
else
    echo ">> config.yaml 已存在，保留"
fi
mkdir -p logs

# ---- 4. 停掉旧的 ----
pkill -f "entropy-rh/main.py" 2>/dev/null || true
pkill -f "record-watch" 2>/dev/null || true
sleep 1

# ---- 5. 看门狗：进程死了 / CSV 卡住 5 分钟就重启 ----
cat > "$DIR/record-watch.sh" <<EOF
#!/usr/bin/env bash
# 看门狗 —— 进程死掉或 CSV 停止增长超过 300 秒就重启
DIR="$DIR"
CSV="\$DIR/logs/minutes.csv"
cd "\$DIR"
while true; do
    if ! pgrep -f "entropy-rh/main.py --record-only" >/dev/null 2>&1; then
        echo "[\$(date -Is)] 采集进程不在，重启" >> "\$DIR/logs/watch.log"
        nohup .venv/bin/python main.py --record-only --no-dashboard \\
            --symbol "$SYMBOL" --hedge "$HEDGE" \\
            >> "\$DIR/logs/record.log" 2>&1 &
        sleep 30
        continue
    fi
    if [[ -f "\$CSV" ]]; then
        age=\$(( \$(date +%s) - \$(stat -c %Y "\$CSV") ))
        if (( age > 300 )); then
            echo "[\$(date -Is)] CSV 已 \${age}s 没更新，判定卡死，重启" >> "\$DIR/logs/watch.log"
            pkill -f "entropy-rh/main.py --record-only" || true
            sleep 3
            continue
        fi
    fi
    sleep 60
done
EOF
chmod +x "$DIR/record-watch.sh"
nohup bash "$DIR/record-watch.sh" >/dev/null 2>&1 &
echo ">> 看门狗已启动 (pid $!)"

# ---- 6. 到点自停 ----
nohup bash -c "sleep $SECS; pkill -f 'entropy-rh/main.py --record-only'; pkill -f record-watch; echo '[done] 采集结束' >> $DIR/logs/watch.log" >/dev/null 2>&1 &
echo ">> 已设定 $DUR 后自动停止"

# ---- 7. 等连通 ----
echo
echo "等待 45 秒确认两腿连通…"
OK=0
for i in $(seq 1 45); do
    sleep 1
    if [[ $(grep -c "connected" logs/record.log 2>/dev/null) -ge 2 ]]; then
        echo "  ${i}s —— 两腿已连接"
        OK=1
        break
    fi
    printf "\r   %2d 秒…" "$i"
done
echo

if (( OK == 0 )); then
    echo "X 45 秒内没连上。日志:"
    tail -20 logs/record.log
    echo
    echo "常见原因: VPS 出网被墙/需代理。若必须走代理:"
    echo "  export HTTPS_PROXY=http://...; 再重跑本脚本（websockets 会自动走）"
    exit 1
fi

# ---- 8. 等第一行落盘 ----
for i in $(seq 1 90); do
    sleep 1
    if [[ -f logs/minutes.csv ]] && [[ $(wc -l < logs/minutes.csv) -ge 2 ]]; then
        echo "OK 采集正常，已写入 $(wc -l < logs/minutes.csv) 行"
        echo "   最新: $(tail -1 logs/minutes.csv)"
        break
    fi
    printf "\r   %2d 秒…" "$i"
done
echo

echo "=============================================="
echo " 部署完成。可以关掉终端了（nohup + 看门狗）。"
echo " 看进度: ssh 进来跑  tail -f $DIR/logs/record.log"
echo " 看行数: wc -l $DIR/logs/minutes.csv"
echo " 提前停: pkill -f 'entropy-rh/main.py'; pkill -f record-watch"
echo " 取回数据（在你 Mac 上跑）:"
echo "   scp ubuntu@<VPS_IP>:~/entropy-rh/logs/minutes.csv ./minutes.csv"
echo " 分析:"
echo "   curl -fsSL $RAW/analyze-peaks.py -o /tmp/ap.py && python3 /tmp/ap.py minutes.csv"
echo "=============================================="
