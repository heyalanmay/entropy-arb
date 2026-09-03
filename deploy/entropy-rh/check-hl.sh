#!/usr/bin/env bash
# =============================================================================
# check-hl.sh — 检查 Hyperliquid 账户是否已准备好跑 Entropy(io dex)
# =============================================================================
#
# 用法（在本机终端里跑，不需要连服务器）：
#     bash check-hl.sh 0x你的HL主账户地址
#
# 它会检查三件事，并直接告诉你哪里没准备好：
#   1. 主 perps 账户有多少钱
#   2. io dex 清算所有多少钱  ← 最关键，Entropy 实际用的是这个
#   3. 你的真实 taker 费率，和 config.rh.yaml 里填的 4.5 对不对得上
#
# 纯只读，不下单、不动钱、不需要私钥。
# =============================================================================

set -uo pipefail

HL_API="https://api.hyperliquid.xyz/info"
CONFIG_FEE="4.5"          # config.rh.yaml 里 entropy.taker_fee_bps 的当前值

ADDR="${1:-}"

hr() { printf '%s\n' "----------------------------------------------"; }

echo "=============================================="
echo " Hyperliquid 账户检查"
echo "=============================================="

# ---------------------------------------------------------------- 参数校验
if [[ -z "$ADDR" ]]; then
    echo ""
    echo "用法：bash check-hl.sh 0x你的HL主账户地址"
    echo ""
    echo "地址在哪看：登录 https://app.hyperliquid.xyz 后，"
    echo "            右上角账户名旁边那串 0x 开头的就是。"
    echo ""
    exit 1
fi

if [[ ! "$ADDR" =~ ^0x[a-fA-F0-9]{40}$ ]]; then
    echo ""
    echo "地址格式不对：$ADDR"
    echo "应该是 0x 开头 + 40 位十六进制，例如 0x1234...（共 42 个字符）"
    echo ""
    exit 1
fi

echo " 地址    : $ADDR"
echo ""

command -v python3 >/dev/null 2>&1 || { echo "错误：本机没找到 python3"; exit 1; }
command -v curl    >/dev/null 2>&1 || { echo "错误：本机没找到 curl";    exit 1; }

# ---------------------------------------------------------------- 拉取数据
# 带重试：单次失败多半是网络抖动，重试一次基本就好
hl_query() {
    local payload="$1" attempt out
    for attempt in 1 2 3; do
        out=$(curl -s -m 20 -X POST "$HL_API" -H "Content-Type: application/json" \
              -d "$payload" 2>/dev/null)
        if [[ -n "$out" ]]; then
            printf '%s' "$out"
            return 0
        fi
        sleep 1
    done
    printf '%s' ""
}

echo "正在查询 Hyperliquid（4 个账户 + 费率，约 2 秒）..."
MAIN_JSON=$(hl_query "{\"type\":\"clearinghouseState\",\"user\":\"$ADDR\"}")
IO_JSON=$(hl_query "{\"type\":\"clearinghouseState\",\"user\":\"$ADDR\",\"dex\":\"io\"}")
SPOT_JSON=$(hl_query "{\"type\":\"spotClearinghouseState\",\"user\":\"$ADDR\"}")
FEE_JSON=$(hl_query "{\"type\":\"userFees\",\"user\":\"$ADDR\"}")

if [[ -z "$IO_JSON" || -z "$FEE_JSON" || -z "$MAIN_JSON" || -z "$SPOT_JSON" ]]; then
    echo ""
    echo "查询失败：连不上 api.hyperliquid.xyz（重试 3 次都不行）"
    echo "  1. 检查网络"
    echo "  2. 稍等一分钟再跑"
    echo "  注意：这不是你的余额变了，是查不到数据。"
    exit 1
fi

# ---------------------------------------------------------------- 解析 + 输出
python3 - "$MAIN_JSON" "$IO_JSON" "$SPOT_JSON" "$FEE_JSON" "$CONFIG_FEE" <<'PYEOF'
import json, sys

main_raw, io_raw, spot_raw, fee_raw, cfg_fee = sys.argv[1:6]

def parse(raw):
    try:
        return json.loads(raw)
    except Exception:
        return None

def money(d, key):
    if not d: return None
    try:
        return float(d["marginSummary"][key])
    except Exception:
        return None

main_d, io_d, spot_d, fee_d = parse(main_raw), parse(io_raw), parse(spot_raw), parse(fee_raw)

ok = True

def width(s):
    """终端显示宽度：CJK 字符占两格。"""
    return sum(2 if ord(c) > 0x2E80 else 1 for c in s)

def line(label, val, note=""):
    pad = " " * max(1, 15 - width(label))
    print(f"    {label}{pad}: {val}{('   ' + note) if note else ''}")

# ---- 1. 现货账户（最容易踩坑的地方：充值默认到 spot，不是 perp）
print("[1] 现货账户 Spot  ← 充值默认落在这里")
spot_usdc = 0.0
if spot_d is None:
    print("    （解析失败）")
else:
    bals = spot_d.get("balances") or []
    shown = False
    for b in bals:
        try:
            total = float(b.get("total", 0) or 0)
        except Exception:
            continue
        if total > 0:
            line(b.get("coin", "?"), f"${total:,.2f}")
            shown = True
            if b.get("coin") == "USDC":
                spot_usdc = total
    if not shown:
        print("    （空）")
print()

# ---- 2. 主 perps
print("[2] 主 perps 账户（普通合约用的钱）")
if main_d is None:
    print("    （解析失败）")
else:
    mv = money(main_d, "accountValue")
    mn = money(main_d, "totalNtlPos")
    line("账户总值", f"${mv:,.2f}" if mv is not None else "?")
    line("持仓名义", f"${mn:,.2f}" if mn is not None else "?")
print()

# ---- 3. io dex
print("[3] io dex perp 账户  ← Entropy 实际用的是这个")
io_val = None
if io_d is None:
    print("    （解析失败）")
    ok = False
else:
    io_val = money(io_d, "accountValue")
    io_pos = money(io_d, "totalNtlPos")
    line("账户总值", f"${io_val:,.2f}" if io_val is not None else "?")
    line("持仓名义", f"${io_pos:,.2f}" if io_pos is not None else "?")
    print()
    if io_val is None:
        print("    ? 读不到余额，把上面命令的原始返回发我")
        ok = False
    elif io_val <= 0:
        print("    X 没钱 —— Entropy 下不了单")
        if spot_usdc > 0:
            print(f"      你的 ${spot_usdc:,.2f} USDC 在【现货账户】里，需要转到 io dex")
            print()
            print("      转法（网页，推荐）：")
            print("        app.hyperliquid.xyz → Portfolio → Transfer（或转入/转出）")
            print("        从 Spot  →  到 Perp")
            print("        Perp dex 选【io】（不是默认的主 dex），金额填 50")
            print()
            print("      如果界面没有 dex 下拉框：先去交易页搜 SNDK，")
            print("      选 io:SNDK，切过去后右上角会弹转账提示。")
        else:
            print("      原因：io dex 有独立清算所，主账户的钱不会自动进去")
            print("      解决：先在 app.hyperliquid.xyz 充值 USDC（Arbitrum 链，最低 5）")
        ok = False
    else:
        print(f"    OK 已注资 ${io_val:,.2f}")
        # cap 150 时，逐仓 10x 只需约 15 保证金，但留 3 倍缓冲更稳
        need = 45.0
        if io_val < need:
            print(f"    提示：当前 cap 150，建议 io dex 至少 ${need:,.0f} 保证金")
            print(f"          现在 ${io_val:,.2f}，偏少")
print()

# ---- 4. 费率
print("[4] taker 费率（全策略最致命的一个数）")
actual = None
if fee_d:
    for k in ("userCrossRate", "cross"):
        v = fee_d.get(k)
        if v is not None:
            try:
                actual = float(v) * 10000
                src = k
                break
            except Exception:
                pass
    if actual is None and isinstance(fee_d.get("feeSchedule"), dict):
        try:
            actual = float(fee_d["feeSchedule"]["cross"]) * 10000
            src = "feeSchedule.cross"
        except Exception:
            pass

try:
    cfg = float(cfg_fee)
except Exception:
    cfg = None

if actual is None:
    print("    （读不到费率，把 userFees 原始返回发我）")
    ok = False
else:
    line("你的实际费率", f"{actual:.2f} bps")
    line("config 填的值", f"{cfg:.1f} bps" if cfg is not None else "?")
    print()
    if cfg is not None:
        diff = abs(actual - cfg)
        if diff < 0.05:
            print("    OK 一致，config 不用改")
        elif actual > cfg:
            print(f"    X 填低了 {actual - cfg:.2f} bps —— 这会让引擎把亏钱单当赚钱单放行")
            print(f"      必须改：config.rh.yaml 的 entropy.taker_fee_bps -> {actual:.2f}")
            ok = False
        else:
            print(f"    提示：你的实际费率比 config 低 {cfg - actual:.2f} bps（可能有 staking 折扣）")
            print(f"      建议改成 {actual:.2f}，能多赚一点")

# ---- 结论
print()
print("==============================================")
if ok:
    print(" 第 2 步完成")
    print("==============================================")
    print(" io dex 有钱、费率对得上。可以去做第 3 步（Lighter RH）。")
else:
    print(" 还没准备好")
    print("==============================================")
    if spot_usdc > 0 and (io_val is None or io_val <= 0):
        print(f" 一句话：你的 ${spot_usdc:,.2f} 在现货账户，")
        print("         转到 io dex 的 perp 账户就能跑。")
        print("         （钱没丢，只是放错了地方）")
    else:
        print(" 按上面标 X 的提示修完，再跑一次这条命令确认。")
print()
PYEOF
