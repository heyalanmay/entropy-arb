#!/usr/bin/env bash
# =============================================================================
# 在你自己的 Mac 上执行 —— 一键把采集部署到新 VPS
#
#   bash push-to-vps.sh ubuntu@1.2.3.4
#   bash push-to-vps.sh root@1.2.3.4 24h
#   SSH_PORT=2222 bash push-to-vps.sh ubuntu@1.2.3.4 72h
#   SYMBOL=ANTH bash push-to-vps.sh ubuntu@1.2.3.4 24h
#
# 做了什么：
#   1. 把 fork 代码打成 121K 的包（不含 .git）
#   2. scp 到 VPS 的 /tmp
#   3. ssh 上去跑 vps-setup-remote.sh（建 venv、装依赖、起看门狗、自检）
#
# 为什么用传包而不是在 VPS 上 git clone：
#   国内 VPS 连 GitHub 经常慢或断，传包 100% 可控，且不依赖出网。
#
# 前置：你能 ssh 上去（密钥或密码都行，密码会提示输入，可能要输 2~3 次）。
# =============================================================================
set -euo pipefail

TARGET="${1:-}"
DUR="${2:-72h}"
SYMBOL="${SYMBOL:-SNDK}"
HEDGE="${HEDGE:-lighter-rh}"
PORT="${SSH_PORT:-22}"

if [[ -z "$TARGET" ]]; then
    echo "用法: bash $0 <user@ip> [时长]"
    echo "  例: bash $0 ubuntu@1.2.3.4 72h"
    exit 1
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FORK="$(cd "$HERE/../.." && pwd)/entropy-arb-fork"

[[ -d "$FORK" ]] || { echo "找不到 fork 目录: $FORK"; exit 1; }

SSH_OPTS=(-o StrictHostKeyChecking=accept-new -o ConnectTimeout=15 -p "$PORT")

echo "=============================================="
echo " 部署采集到 $TARGET"
echo " 品种 $SYMBOL  对冲腿 $HEDGE  时长 $DUR"
echo "=============================================="

# ---- 1. 打包 ----
TGZ=/tmp/entropy-rh.tgz
rm -f "$TGZ"
tar czf "$TGZ" -C "$FORK" --exclude=.git .
echo ">> 已打包 $(du -h "$TGZ" | cut -f1)"

# ---- 2. 上传 ----
echo ">> 上传代码 + 安装脚本"
scp "${SSH_OPTS[@]}" "$TGZ" "$TARGET:/tmp/entropy-rh.tgz"
scp "${SSH_OPTS[@]}" "$HERE/vps-setup-remote.sh" "$TARGET:/tmp/vps-setup-remote.sh"

# ---- 3. 远程安装 ----
echo ">> 远程安装（约 1~2 分钟，含装依赖）"
# shellcheck disable=SC2029
ssh "${SSH_OPTS[@]}" "$TARGET" \
    "SYMBOL=$SYMBOL HEDGE=$HEDGE bash /tmp/vps-setup-remote.sh $DUR"

echo
echo "=============================================="
echo " 完成。取回数据用:"
echo "   scp ${SSH_OPTS[*]} $TARGET:~/entropy-rh/logs/minutes.csv ./minutes-vps.csv"
echo " 分析（在你 Mac 上，需要 entropy-arb 的 venv）:"
echo "   cd /Users/ylh/WorkBuddy/2026-09-02-23-29-43/entropy-arb && \\"
echo "   .venv/bin/python ../deploy/entropy-rh/analyze-peaks.py ../minutes-vps.csv"
echo "=============================================="
