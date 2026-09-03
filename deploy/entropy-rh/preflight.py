#!/usr/bin/env python3
"""Entropy × Lighter-RH 上线预检 / preflight check.

在开真仓之前把这套组合的所有前置条件一次性验完：

  1. .env 密钥是否齐全（不校验正确性，只校验有没有填）
  2. Entropy：Hyperliquid 上 dex "io" 是否存在、io:SNDK 是否挂牌
  3. Lighter RH：SNDK 是否 active、market_id / 精度 / 最小下单量 / taker 费率
  4. 两条 ws 链路的真实延迟（各测 3 次取中位数）——延迟就是这套策略的隐形滑点
  5. 当前两处盘口 top-of-book，算出实时溢价，和 config 里的 midline 对照

用法（在 ~/entropy-arb 目录下）：

    python3 deploy/entropy-rh/preflight.py --symbol SNDK

退出码 0 = 全部通过，1 = 有阻塞项（不要开仓）。
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time

import aiohttp

try:
    from websockets.asyncio.client import connect as ws_connect
except ImportError:  # older websockets
    from websockets import connect as ws_connect  # type: ignore

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None

HL_API = "https://api.hyperliquid.xyz"
HL_WS = "wss://api.hyperliquid.xyz/ws"
RH_API = "https://api.rh.lighter.xyz"
RH_WS = "wss://api.rh.lighter.xyz/stream"
RH_CHAIN_ID = 466324

REST_TIMEOUT = 15.0
LATENCY_RUNS = 3

OK, WARN, FAIL = "OK", "WARN", "FAIL"
_results: list[tuple[str, str, str]] = []


def record(level: str, item: str, detail: str) -> None:
    _results.append((level, item, detail))
    mark = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]"}[level]
    print(f"  {mark} {item:<34} {detail}")


def hr(title: str) -> None:
    print(f"\n{title}\n" + "-" * 78)


# --------------------------------------------------------------------- 1. env

def check_env(env_file: str, hedge: str) -> None:
    hr("1. 密钥 / credentials")
    if load_dotenv is None:
        record(WARN, "python-dotenv", "未安装，跳过 .env 解析")
        return
    if not os.path.exists(env_file):
        record(FAIL, f"{env_file}", "文件不存在（cp env.rh.template .env 后填写）")
        return
    load_dotenv(env_file)

    def _v(name: str) -> str:
        return (os.getenv(name) or "").strip()

    hl_key, hl_addr = _v("HL_PRIVATE_KEY"), _v("HL_ACCOUNT_ADDRESS")
    record(OK if hl_key else FAIL, "HL_PRIVATE_KEY",
           f"{hl_key[:6]}…{hl_key[-4:]} (len={len(hl_key)})" if hl_key else "未填写")
    record(OK if hl_addr else FAIL, "HL_ACCOUNT_ADDRESS",
           hl_addr if hl_addr else "未填写")
    if hedge.startswith("lighter"):
        for name in ("LIGHTER_ACCOUNT_INDEX", "LIGHTER_API_KEY_INDEX",
                     "LIGHTER_API_PRIVATE_KEY"):
            v = _v(name)
            if name == "LIGHTER_API_PRIVATE_KEY":
                record(OK if v else FAIL, name,
                       f"{v[:6]}… (len={len(v)})" if v else "未填写")
            else:
                record(OK if v else FAIL, name, v if v else "未填写")
        ai = _v("LIGHTER_ACCOUNT_INDEX")
        if ai:
            print("         ↳ 确认这一套 key 是在 Robinhood 链 "
                  "(robinhoodchain.lighter.xyz) 上注册的，主网 key 用不了")


# -------------------------------------------------------------- 2. Hyperliquid

async def check_hl(symbol: str, dex: str) -> tuple[str, int, float | None]:
    hr(f"2. Entropy —— Hyperliquid dex '{dex}'")
    coin = ""
    sz_dec = 0
    mid = None
    async with aiohttp.ClientSession() as s:
        try:
            async with s.post(HL_API + "/info", json={"type": "perpDexs"},
                              timeout=aiohttp.ClientTimeout(total=REST_TIMEOUT)) as r:
                dexs = await r.json()
        except Exception as e:
            record(FAIL, "HL /info 可达性", f"{type(e).__name__}: {e}")
            return coin, sz_dec, mid
        names = [d.get("name", "") if isinstance(d, dict) else d for d in dexs or []]
        record(OK, "HL /info 可达", f"perpDexs = {names}")
        if dex not in names:
            record(FAIL, f"dex '{dex}'", f"不存在，可用：{names}")
            return coin, sz_dec, mid
        record(OK, f"dex '{dex}' 存在", f"index={names.index(dex)}")
        entry = next((d for d in dexs
                      if isinstance(d, dict) and d.get("name") == dex), {})
        record(OK, "builder 手续费",
               f"feeRecipient={entry.get('feeRecipient')} "
               f"(null 表示当前不收 deployer 附加费)")

        async with s.post(HL_API + "/info", json={"type": "meta", "dex": dex},
                          timeout=aiohttp.ClientTimeout(total=REST_TIMEOUT)) as r:
            meta = await r.json()
        want = f"{dex}:{symbol}"
        found = None
        for a in meta.get("universe") or []:
            if a.get("name") == want:
                found = a
                break
        if found is None:
            listed = [a.get("name") for a in meta.get("universe") or []]
            record(FAIL, f"{want} 挂牌", f"未找到。该 dex 共 {len(listed)} 个市场，"
                                         f"前 10 个：{listed[:10]}")
            return coin, sz_dec, mid
        coin = want
        sz_dec = int(found.get("szDecimals", 0))
        record(OK, f"{want} 已挂牌", f"szDecimals={sz_dec} "
                                     f"maxLeverage={found.get('maxLeverage')} "
                                     f"onlyIsolated={found.get('onlyIsolated')}")

        async with s.post(HL_API + "/info", json={"type": "allMids", "dex": dex},
                          timeout=aiohttp.ClientTimeout(total=REST_TIMEOUT)) as r:
            mids = await r.json()
        raw = (mids or {}).get(want)
        if raw:
            mid = float(raw)
            record(OK, f"{want} mid", f"{mid}")
    return coin, sz_dec, mid


# ---------------------------------------------------- 2b. taker 手续费核对

async def check_fees(cfg: dict, rh_taker_raw) -> None:
    cfg_entropy_fee = cfg["entropy_fee"]
    cfg_hedge_fee = cfg["hedge_fee"]
    upper, lower = cfg["upper"], cfg["lower"]
    """taker 费率是这套策略里最容易填错、也最致命的一个数。

    引擎会把两个场所的 taker 费叠加到门槛上，所以填少了不是「少赚一点」，
    而是把净利为负的单子当成正 edge 放行。这里用 HL 官方 userFees
    接口拿到账户真实费率，和 config 里填的值直接对账。
    """
    hr("4. taker 手续费核对 / 最容易填错的一项")
    addr = (os.getenv("HL_ACCOUNT_ADDRESS") or "").strip()
    if not addr:
        record(WARN, "HL 账户费率", "未设置 HL_ACCOUNT_ADDRESS，跳过核对")
        return
    try:
        async with aiohttp.ClientSession() as s:
            async with s.post(HL_API + "/info",
                              json={"type": "userFees", "user": addr},
                              timeout=aiohttp.ClientTimeout(total=REST_TIMEOUT)) as r:
                d = await r.json()
    except Exception as e:
        record(WARN, "HL 账户费率", f"查询失败：{type(e).__name__}: {e}")
        return
    sched = d.get("feeSchedule") or {}
    cross = d.get("userCrossRate")
    try:
        real_bps = float(cross) * 1e4
    except (TypeError, ValueError):
        real_bps = float(sched.get("cross", 0)) * 1e4
    disc = (d.get("activeReferralDiscount") or 0, d.get("activeStakingDiscount") or 0)
    record(OK, "Entropy 腿实际 taker 费率",
           f"{real_bps:.3f} bps  (userCrossRate={cross}, "
           f"referral折扣={disc[0]}, staking折扣={disc[1]})")
    # 往返净利 = (upper + lower) - 2 × 少算的手续费
    roundtrip = None
    if upper is not None and lower is not None:
        roundtrip = float(upper) + float(lower)

    if cfg_entropy_fee is not None:
        delta = float(cfg_entropy_fee) - real_bps
        if abs(delta) < 0.05:
            record(OK, "config entropy.taker_fee_bps",
                   f"{cfg_entropy_fee} —— 与实际一致")
        else:
            under = -delta                      # >0 表示 config 少填了
            # 少填 = 放行亏损单（FAIL）；多填 = 只是少开仓（WARN，保守无妨）
            record(FAIL if under > 0 else WARN,
                   "config entropy.taker_fee_bps",
                   f"填 {cfg_entropy_fee}，实际 {real_bps:.3f}，"
                   f"差 {delta:+.3f} bps")
            if under > 0 and roundtrip is not None:
                lost = 2 * under
                net = roundtrip - lost
                verdict = ("结构性亏损，每往返净亏 "
                           f"{abs(net):.2f} bps") if net <= 0 else \
                          f"往返净利被吃掉 {lost:.2f} bps，只剩 {net:.2f} bps"
                print(f"         ↳ 往返少算 {lost:.2f} bps 成本；"
                      f"名义 upper+lower={roundtrip:.2f} bps → {verdict}")
            elif under < 0:
                print("         ↳ config 填得比实际高，只会少开仓（偏保守），"
                      "不会亏钱，但会错过成交")

    if rh_taker_raw is not None and cfg_hedge_fee is not None:
        try:
            rh_rate = float(rh_taker_raw)
            rh_bps = rh_rate * 1e4 if abs(rh_rate) <= 1 else rh_rate
        except (TypeError, ValueError):
            rh_bps = None
        if rh_bps is not None:
            if abs(rh_bps - float(cfg_hedge_fee)) < 0.05:
                record(OK, "config hedge.taker_fee_bps",
                       f"{cfg_hedge_fee} —— 与场所值一致 ({rh_bps:.3f} bps)")
            else:
                record(FAIL, "config hedge.taker_fee_bps",
                       f"填 {cfg_hedge_fee}，场所实际 {rh_bps:.3f} bps")


# ------------------------------------------------------------- 3. Lighter RH

async def check_lighter_rh(symbol: str) -> dict:
    hr("3. Lighter Robinhood 链 / hedge leg")
    out: dict = {}
    async with aiohttp.ClientSession() as s:
        try:
            async with s.get(RH_API + "/api/v1/orderBooks",
                             timeout=aiohttp.ClientTimeout(total=REST_TIMEOUT)) as r:
                r.raise_for_status()
                data = await r.json()
        except Exception as e:
            record(FAIL, "RH REST 可达性", f"{type(e).__name__}: {e}")
            return out
        books = data.get("order_books") or []
        record(OK, "RH REST 可达", f"共 {len(books)} 个市场")
        hit = None
        for ob in books:
            if ob.get("symbol") == symbol:
                hit = ob
                break
        if hit is None:
            listed = [o.get("symbol") for o in books]
            record(FAIL, f"{symbol} 挂牌",
                   f"未找到。RH 链共 {len(listed)} 个市场，前 20 个：{listed[:20]}")
            return out
        status = hit.get("status")
        record(OK if status == "active" else FAIL, f"{symbol} 状态", f"{status}")
        out["market_id"] = int(hit["market_id"])
        out["px_dec"] = int(hit.get("supported_price_decimals", 2))
        out["sz_dec"] = int(hit.get("supported_size_decimals", 4))
        out["min_base"] = float(hit.get("min_base_amount", 0))
        out["min_quote"] = float(hit.get("min_quote_amount", 0))
        out["taker_fee"] = hit.get("taker_fee")
        record(OK, "market_id", f"{out['market_id']}  (chain_id={RH_CHAIN_ID})")
        record(OK, "精度", f"price={out['px_dec']}  size={out['sz_dec']}")
        record(OK, "最小下单量",
               f"min_base={out['min_base']}  min_quote={out['min_quote']}")
        record(OK, "场所 taker 费率",
               f"{out['taker_fee']}  (config 里 hedge.taker_fee_bps 应与之相符)")
    return out


# ------------------------------------------------------------- 4. ws 延迟

async def _hl_latency(coin: str) -> float | None:
    t0 = time.perf_counter()
    try:
        async with ws_connect(HL_WS, max_size=2**23, open_timeout=10) as ws:
            await ws.send(json.dumps({
                "method": "subscribe",
                "subscription": {"type": "l2Book", "coin": coin, "fast": True}}))
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("channel") == "l2Book":
                    return (time.perf_counter() - t0) * 1000.0
    except Exception:
        return None
    return None


async def _rh_latency(market_id: int) -> float | None:
    try:
        async with ws_connect(RH_WS, max_size=2**23, open_timeout=10) as ws:
            t0 = time.perf_counter()
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") == "connected":
                    await ws.send(json.dumps({
                        "type": "subscribe",
                        "channel": f"order_book/{market_id}"}))
                    t0 = time.perf_counter()
                elif msg.get("type") == "ping":
                    await ws.send(json.dumps({"type": "pong"}))
                elif msg.get("type") == "subscribed/order_book":
                    return (time.perf_counter() - t0) * 1000.0
    except Exception:
        return None
    return None


async def measure_latency(coin: str, market_id: int | None) -> None:
    hr("5. ws 链路延迟（3 次中位数，毫秒）")
    if coin:
        xs = [v for v in [await _hl_latency(coin) for _ in range(LATENCY_RUNS)]
              if v is not None]
        if xs:
            med = statistics.median(xs)
            lvl = OK if med < 300 else WARN if med < 800 else FAIL
            record(lvl, "Hyperliquid ws → 首帧",
                   f"median={med:.0f}ms  (min={min(xs):.0f} max={max(xs):.0f})")
        else:
            record(FAIL, "Hyperliquid ws → 首帧", "3 次均未收到 l2Book 快照")
    if market_id is not None:
        xs = [v for v in [await _rh_latency(market_id)
                          for _ in range(LATENCY_RUNS)] if v is not None]
        if xs:
            med = statistics.median(xs)
            lvl = OK if med < 300 else WARN if med < 800 else FAIL
            record(lvl, "Lighter-RH ws → 首帧",
                   f"median={med:.0f}ms  (min={min(xs):.0f} max={max(xs):.0f})")
        else:
            record(FAIL, "Lighter-RH ws → 首帧", "3 次均未收到 order_book 快照")
    print("         ↳ 两边延迟差越大，双腿成交的时间错配越严重，"
          "实际滑点越高。差异 > 300ms 建议换 VPS 区域。")


# ------------------------------------------------------- 5. 实时溢价对照 midline

async def check_premium(coin: str, market_id: int | None, symbol: str,
                        midline: float | None, upper: float | None,
                        lower: float | None) -> None:
    hr("6. 实时溢价 vs 配置的 midline")
    if not coin or market_id is None:
        record(WARN, "溢价对照", "前序检查未通过，跳过")
        return
    for attempt in range(3):
        try:
            if await _premium_once(coin, market_id, symbol,
                                   midline, upper, lower):
                return
        except Exception as e:
            if attempt == 2:
                record(WARN, "溢价对照", f"{attempt + 1} 次尝试均失败："
                                         f"{type(e).__name__}: {e}")
                return
            await asyncio.sleep(1.5)


async def _premium_once(coin: str, market_id: int, symbol: str,
                        midline, upper, lower) -> bool:
    e_mid, r_mid = None, None
    async with ws_connect(HL_WS, max_size=2**23, open_timeout=10) as ws:
        await ws.send(json.dumps({
            "method": "subscribe",
            "subscription": {"type": "l2Book", "coin": coin, "fast": True}}))
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("channel") == "l2Book":
                lv = (msg.get("data") or {}).get("levels") or [[], []]
                if lv[0] and lv[1]:
                    e_mid = (float(lv[0][0]["px"]) + float(lv[1][0]["px"])) / 2
                break
    async with ws_connect(RH_WS, max_size=2**23, open_timeout=10) as ws:
        async for raw in ws:
            msg = json.loads(raw)
            if msg.get("type") == "connected":
                await ws.send(json.dumps({
                    "type": "subscribe",
                    "channel": f"order_book/{market_id}"}))
            elif msg.get("type") == "ping":
                await ws.send(json.dumps({"type": "pong"}))
            elif msg.get("type") == "subscribed/order_book":
                ob = msg.get("order_book") or {}
                bids = ob.get("bids") or []
                asks = ob.get("asks") or []
                if bids and asks:
                    bp = float(bids[0].get("price"))
                    ap = float(asks[0].get("price"))
                    r_mid = (bp + ap) / 2
                break
    if not e_mid or not r_mid:
        record(WARN, "溢价对照", "未能取到双边盘口")
        return False
    prem = (e_mid / r_mid - 1.0) * 1e4
    record(OK, f"{symbol} 双边 mid", f"Entropy={e_mid}  RH={r_mid}")
    record(OK, "当前溢价", f"{prem:+.2f} bps")
    if midline is not None:
        dist = prem - midline
        record(OK, "距 midline", f"{dist:+.2f} bps")
        if upper is not None and dist >= upper:
            print(f"         ↳ 当前已在卖 Entropy 区间（>= +{upper}），"
                  f"上线即可能立刻开仓")
        elif lower is not None and dist <= -lower:
            print(f"         ↳ 当前已在买 Entropy 区间（<= -{lower}），"
                  f"上线即可能立刻开仓")
        else:
            print("         ↳ 当前在带宽内，上线后应先静默采集")
        if abs(dist) > 15:
            record(WARN, "midline 偏离",
                   f"实测溢价与配置中枢相差 {abs(dist):.1f} bps，"
                   f"建议重跑 tools/analyze.py 校准")
    return True


# ---------------------------------------------------------------------- main

def read_config(config_file: str) -> dict:
    """读出预检需要的 config 片段：thresholds + 两个场所的 taker 费率。"""
    empty = {"midline": None, "upper": None, "lower": None,
             "entropy_fee": None, "hedge_fee": None}
    try:
        import yaml
        with open(config_file) as fh:
            raw = yaml.safe_load(fh) or {}
    except Exception:
        return empty
    t = raw.get("thresholds") or {}
    return {
        "midline": t.get("midline_bps"),
        "upper": t.get("upper_bps"),
        "lower": t.get("lower_bps"),
        "entropy_fee": (raw.get("entropy") or {}).get("taker_fee_bps"),
        "hedge_fee": (raw.get("hedge") or {}).get("taker_fee_bps"),
    }


async def amain(args) -> int:
    print("=" * 78)
    print(f"entropy-arb 预检：Entropy(io) × Lighter-RH    品种 {args.symbol}")
    print("=" * 78)
    check_env(args.env_file, args.hedge)
    coin, _sz, _mid = await check_hl(args.symbol, args.dex)
    rh = await check_lighter_rh(args.symbol)
    cfg = read_config(args.config)
    await check_fees(cfg, rh.get("taker_fee"))
    await measure_latency(coin, rh.get("market_id"))
    await check_premium(coin, rh.get("market_id"), args.symbol,
                        cfg["midline"], cfg["upper"], cfg["lower"])

    hr("结论 / summary")
    n_fail = sum(1 for lv, _, _ in _results if lv == FAIL)
    n_warn = sum(1 for lv, _, _ in _results if lv == WARN)
    print(f"  FAIL {n_fail}    WARN {n_warn}    OK "
          f"{sum(1 for lv, _, _ in _results if lv == OK)}")
    if n_fail:
        print("  → 有阻塞项，不要开仓。修完上面的 FAIL 再跑一次。")
        return 1
    if n_warn:
        print("  → 无阻塞项，但有 WARN 需要你判断后再开仓。")
    else:
        print("  → 全部通过，可以进入 --record-only 采集阶段。")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Entropy × Lighter-RH 上线预检")
    p.add_argument("--symbol", default="SNDK")
    p.add_argument("--hedge", default="lighter-rh")
    p.add_argument("--dex", default="io")
    p.add_argument("--config", default="config.yaml")
    p.add_argument("--env-file", default=".env")
    args = p.parse_args()
    sys.exit(asyncio.run(amain(args)))


if __name__ == "__main__":
    main()
