#!/usr/bin/env bash
# =============================================================================
# 安装 Entropy × Lighter-RH 的配置到 ~/entropy-arb
#
#   bash deploy/entropy-rh/install.sh            # 只装配置（采集阶段够用）
#   bash deploy/entropy-rh/install.sh --live     # 额外安装实盘签名 SDK
#
# 安全设计：任何已存在的 config.yaml / .env 都会先备份再改动，绝不裸覆盖。
# =============================================================================
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
LIVE=0
[[ "${1:-}" == "--live" ]] && LIVE=1

cd "$ROOT"
[[ -f main.py ]] || { echo "错误：没在 entropy-arb 仓库根目录（$ROOT 下没有 main.py）"; exit 1; }
echo "仓库目录: $ROOT"

TS="$(date +%Y%m%d_%H%M%S)"
mkdir -p logs deploy/entropy-rh

# ---- config.yaml -----------------------------------------------------------
if [[ -f config.yaml ]]; then
    cp config.yaml "config.yaml.bak.$TS"
    echo "已备份旧配置 → config.yaml.bak.$TS"
fi
cp "$HERE/config.rh.yaml" config.yaml
echo "已写入 config.yaml（Entropy × lighter-rh）"

# ---- .env（密钥，绝不裸覆盖）------------------------------------------------
if [[ -f .env ]]; then
    cp .env ".env.bak.$TS"
    echo "已备份旧密钥 → .env.bak.$TS"
    echo ""
    echo "检测到已有 .env，脚本不会覆盖它。请手工确认下面这些键都已填写："
    for k in HL_PRIVATE_KEY HL_ACCOUNT_ADDRESS LIGHTER_ACCOUNT_INDEX \
             LIGHTER_API_KEY_INDEX LIGHTER_API_PRIVATE_KEY; do
        if grep -qE "^[[:space:]]*${k}=.+[^[:space:]]" .env 2>/dev/null; then
            echo "  [有] $k"
        else
            echo "  [缺] $k   ← 需要补"
        fi
    done
    echo ""
    echo "参考模板：$HERE/env.rh.template"
else
    cp "$HERE/env.rh.template" .env
    chmod 600 .env
    echo "已生成 .env 模板（权限 600）—— 现在去填：vi .env"
fi

# ---- 依赖 ------------------------------------------------------------------
PY="${PY:-python3}"
echo ""
echo "安装基础依赖（采集阶段所需）..."
"$PY" -m pip install -q --upgrade pip
"$PY" -m pip install -q -r requirements.txt

if [[ $LIVE -eq 1 ]]; then
    echo "安装实盘签名 SDK（hyperliquid + lighter）..."
    "$PY" -m pip install -q -r requirements-live.txt
else
    echo ""
    echo "跳过实盘 SDK。要实盘时再跑：bash deploy/entropy-rh/install.sh --live"
fi

chmod +x "$HERE/run.sh" "$HERE/preflight.py" 2>/dev/null || true

echo ""
echo "=============================================================="
echo " 装好了。下一步："
echo "   1. 填密钥：        vi .env"
echo "   2. 跑预检：        $PY deploy/entropy-rh/preflight.py --symbol SNDK"
echo "   3. 采集数据：      bash deploy/entropy-rh/run.sh record"
echo "   4. 分析出阈值：    $PY tools/analyze.py"
echo "   5. 实盘：          bash deploy/entropy-rh/run.sh live"
echo "=============================================================="
