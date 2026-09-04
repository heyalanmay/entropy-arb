#!/usr/bin/env python3
"""多品种溢价波动探针 —— 在决定采哪个品种之前，先横向比一比。

背景：Entropy(io dex) 只有 7 个品种，RH 有 74 个，交集只有 3 个。
而盈亏取决于「溢价摆动幅度」是否够大，不同品种差别可能很大。
采 72 小时之前先花 3 分钟比一下，能避免把时间浪费在最差的品种上。

口径与 analyze-peaks.py 一致：
    回本所需摆动 ≈ (Entropy 价差 + RH 价差) + 手续费×2
所以这里**同时报波动和回本线**，直接给比值。

用法（在 entropy-arb 仓库目录下）：
    .venv/bin/python ../deploy/entropy-rh/probe-symbols.py            # 默认 180 秒
    .venv/bin/python ../deploy/entropy-rh/probe-symbols.py 300
    FEES=2.25 .venv/bin/python ...      # 若改走 maker，把单次手续费调低再比

注意：只在集合竞价/盘中才有意义，休市时段所有品种都会很平。
"""
from __future__ import annotations

import asyncio
import json
import os
import statistics as st
import sys
import time

# websockets v14+ 会读环境/系统代理；直连才能和 REST 同链路
os.environ.setdefault("no_proxy", "*")
os.environ.setdefault("NO_PROXY", "*")
for k in ("http_proxy", "https_proxy", "all_proxy",
          "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
    os.environ.pop(k, None)

from websockets.asyncio.client import connect  # noqa: E402

sys.path.insert(0, os.getcwd())
from entropy_arb.book import OrderBook  # noqa: E402

HL_WS = "wss://api.hyperliquid.xyz/ws"
RH_WS = "wss://api.rh.lighter.xyz/stream"
RH_API = "https://api.rh.lighter.xyz"

# Entropy(io dex) 的品种 -> RH 上的名字（注意：RH 用全名，不是缩写）
PAIRS = [
    ("SNDK", "SNDK"),
    ("ANTH", "ANTHROPIC"),
    ("OAI", "OPENAI"),
]

FEES_ONEWAY = float(os.environ.get("FEES", "4.5"))   # 单次开或平的双腿手续费
DURATION = float(sys.argv[1]) if len(sys.argv) > 1 else 180.0


async def rh_markets() -> dict:
    import aiohttp
    async with aiohttp.ClientSession() as s:
        async with s.get(f"{RH_API}/api/v1/orderBooks",
                         timeout=aiohttp.ClientTimeout(total=20)) as r:
            data = await r.json()
    out = {}
    for m in data.get("order_books", []):
        if m.get("status") == "active":
            out[m["symbol"].upper()] = m
    return out


class Sym:
    def __init__(self, hl_coin: str, rh_name: str, rh_id: int):
        self.hl_coin = hl_coin
        self.rh_name = rh_name
        self.rh_id = rh_id
        self.e = OrderBook()
        self.h = OrderBook()
        self.samples: list[tuple[float, float, float, float]] = []  # t, prem, sp_e, sp_h


async def feed_hl(s: Sym, stop: asyncio.Event):
    while not stop.is_set():
        try:
            async with connect(HL_WS, max_size=2**23, open_timeout=10,
                               ping_interval=15, ping_timeout=15) as ws:
                await ws.send(json.dumps({
                    "method": "subscribe",
                    "subscription": {"type": "l2Book", "coin": s.hl_coin}}))
                async for raw in ws:
                    if stop.is_set():
                        return
                    msg = json.loads(raw)
                    if msg.get("channel") != "l2Book":
                        continue
                    d = msg.get("data", {})
                    if d.get("coin") not in (s.hl_coin, s.hl_coin.split(":", 1)[-1]):
                        continue
                    s.e.apply_hl(d["levels"])
        except asyncio.CancelledError:
            return
        except Exception as e:
            if stop.is_set():
                return
            print(f"  [{s.hl_coin}] HL 重连: {type(e).__name__}: {e}", flush=True)
            await asyncio.sleep(2)


async def feed_rh(s: Sym, stop: asyncio.Event):
    while not stop.is_set():
        try:
            async with connect(RH_WS, max_size=2**23, open_timeout=10,
                               ping_interval=15, ping_timeout=15) as ws:
                await ws.send(json.dumps({
                    "type": "subscribe",
                    "channel": f"order_book/{s.rh_id}"}))
                async for raw in ws:
                    if stop.is_set():
                        return
                    msg = json.loads(raw)
                    ob = msg.get("order_book")
                    if not ob:
                        continue
                    s.h.apply_lighter(ob, snapshot=msg.get("type") == "subscribed"
                                      or "nonce" in ob and not s.h.ready)
        except asyncio.CancelledError:
            return
        except Exception as e:
            if stop.is_set():
                return
            print(f"  [{s.rh_name}] RH 重连: {type(e).__name__}: {e}", flush=True)
            await asyncio.sleep(2)


def side(bids, asks):
    b, a = bids[0][0], asks[0][0]
    return b, a


async def main() -> None:
    print("查询 RH 市场…", flush=True)
    mkts = await rh_markets()

    syms: list[Sym] = []
    for hl_name, rh_name in PAIRS:
        m = mkts.get(rh_name.upper())
        if not m:
            print(f"  ⚠️  RH 上没有 {rh_name}，跳过")
            continue
        syms.append(Sym(f"io:{hl_name}", rh_name, int(m["market_id"])))
        print(f"  ✅ {hl_name:5} (io:{hl_name})  ↔  RH {rh_name} (market_id={m['market_id']})")

    if not syms:
        print("没有可用品种")
        return

    stop = asyncio.Event()
    tasks = []
    for s in syms:
        tasks.append(asyncio.create_task(feed_hl(s, stop)))
        tasks.append(asyncio.create_task(feed_rh(s, stop)))

    print(f"\n采样 {DURATION:.0f} 秒…", flush=True)
    t0 = time.time()
    try:
        while time.time() - t0 < DURATION:
            await asyncio.sleep(1.0)
            now = time.time()
            for s in syms:
                eb, ea = s.e.best_bid(), s.e.best_ask()
                hb, ha = s.h.best_bid(), s.h.best_ask()
                if None in (eb, ea, hb, ha):
                    continue
                e_mid, h_mid = (eb + ea) / 2, (hb + ha) / 2
                prem = (e_mid / h_mid - 1) * 1e4
                sp_e = (ea / eb - 1) * 1e4
                sp_h = (ha / hb - 1) * 1e4
                s.samples.append((now, prem, sp_e, sp_h))
    finally:
        stop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    fees_rt = FEES_ONEWAY * 2
    print("\n" + "=" * 78)
    print(f" 品种横向对比   采样 {DURATION:.0f}s   单次手续费 {FEES_ONEWAY} bps"
          f"（往返 {fees_rt:.1f}）")
    print("=" * 78)
    print(f" {'品种':<6}{'样本':>5}{'溢价均值':>10}{'标准差':>9}{'区间':>16}"
          f"{'E价差':>8}{'RH价差':>8}{'回本线':>8}{'最大摆动':>10}{'比值':>7}")
    print(" " + "-" * 76)

    rows = []
    for s in syms:
        if len(s.samples) < 10:
            print(f" {s.hl_coin:<8}{len(s.samples):>5}  样本太少，跳过")
            continue
        ts = [x[0] for x in s.samples]
        pr = [x[1] for x in s.samples]
        sp_e = st.median([x[2] for x in s.samples])
        sp_h = st.median([x[3] for x in s.samples])
        hurdle = sp_e + sp_h + fees_rt

        # 最大有利摆动：任意时刻出发，30 秒内溢价能达到的最大有利位移
        best = 0.0
        step = max(1, len(pr) // 400)
        for i in range(0, len(pr), step):
            p0 = pr[i]
            t_start = ts[i]
            win = [p for t, p in zip(ts[i:], pr[i:]) if t - t_start <= 30]
            if not win:
                continue
            best = max(best, p0 - min(win), max(win) - p0)

        mean = st.fmean(pr)
        sd = st.pstdev(pr)
        lo, hi = min(pr), max(pr)
        ratio = best / hurdle if hurdle else 0
        rows.append((ratio, s.hl_coin))
        print(f" {s.hl_coin.split(':')[-1]:<6}{len(pr):>5}{mean:>+10.2f}{sd:>9.2f}"
              f"{f'{lo:+.1f}~{hi:+.1f}':>16}{sp_e:>8.2f}{sp_h:>8.2f}"
              f"{hurdle:>8.1f}{best:>10.2f}{ratio:>7.2f}")

    print(" " + "-" * 76)
    print(" 比值 = 30 秒内最大有利摆动 ÷ 回本线；≥1 才勉强够本，>1.5 才有做头。")
    print(" 注意：现在是休市还是盘中，对结果影响巨大 —— 请对照时间看。")
    if rows:
        rows.sort(reverse=True)
        print(f"\n 排序: {' > '.join(f'{n}({r:.2f})' for r, n in rows)}")
    print("=" * 78)


if __name__ == "__main__":
    asyncio.run(main())
