#!/usr/bin/env bash
# ============================================================
#  check-rh.sh  ——  Lighter-Robinhood 链账户只读自检
#  纯只读，不需要私钥，不动你的钱。
#  用途：注册 + 生成 API key 之后，验证「账户在、key 对、能下 bot 要的腿」
#
#  用法：
#    bash check-rh.sh 0x你的钱包地址
#    bash check-rh.sh 0x你的钱包地址 4          # 第 2 个参数=api_key_index（默认 4）
#
#  输出结论：账户是否注册、account_index 是多少（填 env 用）、
#           api_key_index 是否注册成功、链上余额请在网页看（USDG）
# ============================================================
set -euo pipefail

RH_API="https://api.rh.lighter.xyz"

ADDR="${1:-}"
API_IDX="${2:-4}"          # Lighter 保留 0~3 给网页端，key 用 4~254

if [[ -z "$ADDR" || ! "$ADDR" =~ ^0x[0-9a-fA-F]{40}$ ]]; then
    echo "用法：bash check-rh.sh 0x你的钱包地址 [api_key_index]"
    echo "  api_key_index 默认 4（Lighter 保留 0~3 给网页端，别用）"
    exit 1
fi

echo "=============================================="
echo "Lighter-Robinhood 账户检查"
echo "=============================================="
echo "钱包地址    : $ADDR"
echo "api_key_index: $API_IDX"
echo "正在查询 RH 链..."

ok=1

# ---- 1. 账户是否存在 ----
RESP=$(curl -s -m 25 "$RH_API/api/v1/accountsByL1Address?l1_address=$ADDR" 2>/dev/null)
CODE=$(echo "$RESP" | python3 -c "import json,sys;print(json.load(sys.stdin).get('code'))" 2>/dev/null || echo "ERR")
ACCT_IDX=$(echo "$RESP" | python3 -c "
import json,sys
d=json.load(sys.stdin)
accs=d.get('accounts') or d.get('account_indices') or []
if accs:
    print(accs[0] if isinstance(accs[0],int) else accs[0].get('account_index'))
" 2>/dev/null || echo "")

echo
echo "[1] 子账户（account_index）"
if [[ "$CODE" == "21100" || -z "$ACCT_IDX" ]]; then
    echo "    X 还没注册 —— RH 链是独立于主网 Lighter 的另一套账户"
    echo "      解决：浏览器打开 https://robinhoodchain.lighter.xyz"
    echo "            用同一个钱包连接 → 创建子账户 → 生成 API key"
    echo "      注册完再跑一次这条命令。"
    echo
    echo "=============================================="
    echo " 还没准备好"
    echo "=============================================="
    exit 1
else
    echo "    OK 已注册，account_index = $ACCT_IDX"
    echo "    （把这个值填进 env 的 LIGHTER_ACCOUNT_INDEX）"
    ok=1
fi

# ---- 2. API key 是否注册 ----
RESP2=$(curl -s -m 25 "$RH_API/api/v1/apikeys?account_index=$ACCT_IDX&api_key_index=$API_IDX" 2>/dev/null)
CODE2=$(echo "$RESP2" | python3 -c "import json,sys;print(json.load(sys.stdin).get('code'))" 2>/dev/null || echo "ERR")

echo
echo "[2] API key 注册（api_key_index=$API_IDX）"
if [[ "$CODE2" == "200" ]]; then
    echo "    OK key 已注册，bot 能用这个 index 下 bot 腿"
    echo "    （把这个值填进 env 的 LIGHTER_API_KEY_INDEX）"
elif [[ "$CODE2" == "21109" ]]; then
    echo "    X key 没注册（index=$API_IDX 不存在）"
    echo "      解决：在 robinhoodchain.lighter.xyz 的 API 页面"
    echo "            生成 api_key_index = $API_IDX 的 key"
    echo "      注意：0~3 被网页端保留，别用；用 4~254"
    ok=0
else
    echo "    ? 接口返回异常（$RESP2）"
    ok=0
fi

# ---- 3. 余额（只读拿不到，提示去网页看）----
echo
echo "[3] 链上余额"
echo "    只读接口查不到余额，请在网页确认："
echo "    robinhoodchain.lighter.xyz → 账户页应显示 USDG（充 USDC 进去会显示成 USDG，正常）"
echo "    要够一腿：$30 以上（bot 单笔最多 $30，留余量）"

# ---- 结论 ----
echo
echo "=============================================="
if [[ "${ok:-1}" == "1" ]]; then
    echo " 第 3 步（RH）完成"
    echo "=============================================="
    echo " 账户在、key 对。把这两个数填进 env.rh.template："
    echo "   LIGHTER_ACCOUNT_INDEX=$ACCT_IDX"
    echo "   LIGHTER_API_KEY_INDEX=$API_IDX"
    echo " 再去网页确认 USDG 余额 ≥ \$30，就可以进第 4 步（等服务器）。"
else
    echo " 还没准备好"
    echo "=============================================="
    echo " 按上面标 X 的提示修完，再跑一次确认。"
fi
echo
