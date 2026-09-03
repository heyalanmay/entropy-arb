#!/usr/bin/env python3
"""往返回测 + 尖峰分布分析 —— 回答「这个品种到底能不能赚」

为什么不用 tools/analyze.py：
  它用**分钟均值**（edge_mean）统计触发频率，而策略吃的是**瞬时尖峰**，
  于是对尖峰型机会系统性低估；而且它只看单边门槛是否被突破，
  不检验「开了之后能不能以更好的价格平回来」——真正决定赚赔的是往返。

口径（关键，别搞错）：
  recorder 记的 sell_edge / buy_edge 是**可执行价差**（bid/ask 算的），
  已经含了穿价差的滑点，所以**只需再叠手续费**，不要重复扣滑点。
    开仓 卖Entropy/买对冲   捕获 S = sell_edge
    平仓 买Entropy/卖对冲   捕获 B = buy_edge
    净利(bps) = S + B - 手续费(开+平)
  单腿手续费默认 HL taker 4.5 + RH 0 = 4.5 bps/次，往返 9 bps。

用法：
    .venv/bin/python analyze-peaks.py                        # 默认读 logs/minutes.csv
    .venv/bin/python analyze-peaks.py logs/minutes.csv --notional 30
    .venv/bin/python analyze-peaks.py --fees-bps 4.5 --max-hold 60 --edge both
"""
from __future__ import annotations

import argparse
import csv
import statistics as st
import sys
from datetime import datetime, timezone

BJ_OFFSET = 8 * 3600


# --------------------------------------------------------------------------- io
def load(path: str) -> list[dict]:
    with open(path, newline="") as fh:
        rows = list(csv.DictReader(fh))
    if not rows:
        sys.exit(f"{path} 里没有数据行（只有表头或空文件）")
    out = []
    for r in rows:
        try:
            out.append({
                "ts": int(r["minute_ts"]),
                "utc": r["time_utc"],
                "e_bid": float(r["entropy_bid"]), "e_ask": float(r["entropy_ask"]),
                "h_bid": float(r["hedge_bid"]), "h_ask": float(r["hedge_ask"]),
                "prem": float(r["premium_mean_bps"]),
                "prem_hi": float(r["premium_high_bps"]),
                "prem_lo": float(r["premium_low_bps"]),
                "s_mean": float(r["sell_edge_mean_bps"]),
                "s_max": float(r["sell_edge_max_bps"]),
                "b_mean": float(r["buy_edge_mean_bps"]),
                "b_max": float(r["buy_edge_max_bps"]),
                "n": int(r["samples"]),
            })
        except (KeyError, ValueError) as e:
            sys.exit(f"CSV 格式不认识（{e}）—— 是不是旧 schema？删掉重采。")
    out.sort(key=lambda r: r["ts"])
    return out


def pct(xs: list[float], p: float) -> float:
    if not xs:
        return float("nan")
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def fmt(x: float, w: int = 7, d: int = 2) -> str:
    return f"{x:>{w}.{d}f}" if x == x else " " * (w - 3) + "n/a"


# ---------------------------------------------------------------- round trip bt
def backtest(rows: list[dict], entry_bps: float, fees_rt: float,
             max_hold: int, use_max: bool, direction: str) -> dict:
    """一次进出算一笔；不重叠开仓（一笔平掉才开下一笔）。

    direction "sell_first": 先卖 Entropy（吃正溢价），用 S 开、B 平
    direction "buy_first" : 先买 Entropy（吃负溢价），用 B 开、S 平
    """
    ek, xk = ("s", "b") if direction == "sell_first" else ("b", "s")
    sfx = "max" if use_max else "mean"
    ekey, xkey = f"{ek}_{sfx}", f"{xk}_{sfx}"

    trades: list[dict] = []
    i, n = 0, len(rows)
    while i < n:
        if rows[i][ekey] < entry_bps:
            i += 1
            continue
        S = rows[i][ekey]
        need = fees_rt - S                    # 平仓腿至少要捕获这么多才不亏
        j, best_j, best_B = i + 1, None, -1e9
        while j < n and rows[j]["ts"] - rows[i]["ts"] <= max_hold * 60:
            B = rows[j][xkey]
            if B > best_B:
                best_B, best_j = B, j
            if B >= need:                     # 一旦够本立刻平（不贪）
                best_j, best_B = j, B
                break
            j += 1
        if best_j is None:                    # 数据尾部，开不完整
            break
        net = S + best_B - fees_rt
        trades.append({
            "open": rows[i]["utc"], "S": S, "B": best_B, "net": net,
            "hold": (rows[best_j]["ts"] - rows[i]["ts"]) // 60,
            "timeout": best_B < need,
        })
        i = best_j + 1                        # 平完才允许下一笔
    if not trades:
        return {"trades": 0}
    nets = [t["net"] for t in trades]
    return {
        "trades": len(trades),
        "wins": sum(1 for x in nets if x > 0),
        "net_sum": sum(nets),
        "net_mean": st.fmean(nets),
        "net_med": st.median(nets),
        "worst": min(nets),
        "best": max(nets),
        "hold_mean": st.fmean([t["hold"] for t in trades]),
        "timeouts": sum(1 for t in trades if t["timeout"]),
        "detail": trades,
    }


# ------------------------------------------------------------------------ main
def main() -> None:
    ap = argparse.ArgumentParser(description="往返回测 + 尖峰分布分析")
    ap.add_argument("csv", nargs="?", default="logs/minutes.csv")
    ap.add_argument("--fees-bps", type=float, default=4.5,
                    help="单次开/平的双腿总手续费 bps（HL taker 4.5 + RH 0，默认 4.5）")
    ap.add_argument("--notional", type=float, default=30.0, help="单笔名义 USD")
    ap.add_argument("--max-hold", type=int, default=60, help="最长持仓分钟数")
    ap.add_argument("--edge", choices=["max", "mean", "both"], default="both",
                    help="用分钟内峰值(乐观，假设你抓到那一秒)还是均值(悲观)")
    ap.add_argument("--top", type=int, default=8, help="打印前 N 笔明细")
    a = ap.parse_args()

    rows = load(a.csv)
    fees_rt = a.fees_bps * 2

    # ---- 数据概况 ----
    t0, t1 = rows[0]["ts"], rows[-1]["ts"]
    span_min = (t1 - t0) // 60 + 1
    cover = len(rows) / span_min * 100 if span_min else 0
    thin = sum(1 for r in rows if r["n"] < 30)
    print("=" * 74)
    print(" 数据概况")
    print("=" * 74)
    print(f" 文件      : {a.csv}")
    print(f" 区间      : {rows[0]['utc']} → {rows[-1]['utc']} (UTC)")
    print(f" 分钟行数  : {len(rows)}  /  跨度 {span_min} 分钟  →  覆盖率 {cover:.1f}%")
    print(f" 采样偏薄  : {thin} 行 samples<30（两腿有一边 STALE 过）")
    if len(rows) < 60:
        print(" !! 不足 1 小时，下面所有结论都只能当方向参考，别拿去改实盘配置")

    # ---- 价差（滑点来源，已含在 edge 里，这里只是让你知道贵在哪）----
    e_sp = [(r["e_ask"] / r["e_bid"] - 1) * 1e4 for r in rows]
    h_sp = [(r["h_ask"] / r["h_bid"] - 1) * 1e4 for r in rows]
    print(f"\n 盘口价差  : Entropy 中位 {st.median(e_sp):.2f} bps   "
          f"对冲腿 中位 {st.median(h_sp):.2f} bps"
          f"   （合计 {st.median(e_sp)+st.median(h_sp):.2f} bps，已含在 edge 内）")

    # ---- premium / edge 分布 ----
    prem = [r["prem"] for r in rows]
    print("\n" + "=" * 74)
    print(" 分布（bps）")
    print("=" * 74)
    print(f" premium   : mean {st.fmean(prem):+.2f}  median {st.median(prem):+.2f}  "
          f"std {st.pstdev(prem) if len(prem) > 1 else 0:.2f}  "
          f"[{min(r['prem_lo'] for r in rows):+.2f}, {max(r['prem_hi'] for r in rows):+.2f}]")
    hdr = f" {'序列':<22}" + "".join(f"{p:>8}" for p in
                                    ["p50", "p75", "p90", "p95", "p99", "max"])
    print("\n" + hdr)
    print(" " + "-" * 71)
    for label, key in [("sell_edge 分钟峰值", "s_max"), ("sell_edge 分钟均值", "s_mean"),
                       ("buy_edge  分钟峰值", "b_max"), ("buy_edge  分钟均值", "b_mean")]:
        xs = [r[key] for r in rows]
        print(f" {label:<20}" + "".join(fmt(v, 8) for v in
              [pct(xs, .5), pct(xs, .75), pct(xs, .9), pct(xs, .95), pct(xs, .99), max(xs)]))
    print(f"\n 往返成本线: {fees_rt:.1f} bps（= 手续费 {a.fees_bps}×2；滑点已在 edge 里）")
    print(" 单边参考线: 一笔要赚钱，开仓捕获 S + 平仓捕获 B 之和必须 > 这条线")

    # ---- 门槛扫描（单边命中率，回答「多久出现一次」）----
    print("\n" + "=" * 74)
    print(" 门槛命中率（分钟峰值口径 —— 乐观上界）")
    print("=" * 74)
    print(f" {'门槛':>6}  {'sell 命中':>10} {'次/天':>7}   {'buy 命中':>10} {'次/天':>7}")
    print(" " + "-" * 60)
    days = max(span_min / 1440, 1e-9)
    for g in [2, 4, 6, 8, 10, 12, 14, 16, 20]:
        cs = sum(1 for r in rows if r["s_max"] >= g)
        cb = sum(1 for r in rows if r["b_max"] >= g)
        print(f" {g:>5} b  {cs:>6} ({cs/len(rows)*100:4.1f}%) {cs/days:>7.1f}   "
              f"{cb:>6} ({cb/len(rows)*100:4.1f}%) {cb/days:>7.1f}")

    # ---- 往返回测 ----
    modes = [("分钟峰值(乐观)", True), ("分钟均值(悲观)", False)]
    if a.edge != "both":
        modes = [m for m in modes if m[1] == (a.edge == "max")]

    best = None
    for label, use_max in modes:
        print("\n" + "=" * 74)
        print(f" 往返回测 —— {label}   持仓上限 {a.max_hold} 分钟   单笔 ${a.notional:.0f}")
        print("=" * 74)
        print(f" {'方向':<10}{'开仓门槛':>9}{'笔数':>6}{'胜率':>7}{'净均值':>9}"
              f"{'净合计':>9}{'最差':>8}{'持仓均':>7}{'超时':>6}{'估日收益':>10}")
        print(" " + "-" * 71)
        for direction, dname in [("sell_first", "卖E先"), ("buy_first", "买E先")]:
            for g in [fees_rt / 2, 6, 8, 10, 12, 14]:
                r = backtest(rows, g, fees_rt, a.max_hold, use_max, direction)
                if not r["trades"]:
                    continue
                usd = r["net_sum"] / 1e4 * a.notional
                print(f" {dname:<10}{g:>8.1f}b{r['trades']:>6}"
                      f"{r['wins']/r['trades']*100:>6.0f}%{r['net_mean']:>+9.2f}"
                      f"{r['net_sum']:>+9.1f}{r['worst']:>+8.1f}"
                      f"{r['hold_mean']:>7.0f}{r['timeouts']:>6}"
                      f"{usd/days:>+9.2f}$")
                # 只有净合计为正才配得上"最优"；全负就是这品种不能做
                if use_max and r["net_sum"] > 0 and \
                        (best is None or r["net_sum"] > best[2]["net_sum"]):
                    best = (dname, g, r, direction)

    # ---- 明细 + 结论 ----
    if best:
        dname, g, r, direction = best
        print("\n" + "=" * 74)
        print(f" 最优组合明细（{dname} 门槛 {g:.1f} bps，乐观口径）前 {a.top} 笔")
        print("=" * 74)
        for t in r["detail"][:a.top]:
            flag = " TIMEOUT" if t["timeout"] else ""
            print(f" {t['open']}  开{t['S']:+7.2f}  平{t['B']:+7.2f}  "
                  f"净{t['net']:+7.2f}b  持{t['hold']:>3}分{flag}")

        prem_med = st.median(prem)
        hurdle = g - a.fees_bps
        print("\n" + "=" * 74)
        print(" 折算成 config.yaml（仅当上面净合计为正、且样本 >12 小时才可用）")
        print("=" * 74)
        print(" thresholds:")
        print(f"   midline_bps: {prem_med:.1f}")
        if direction == "sell_first":
            print(f"   upper_bps: {hurdle - prem_med:.1f}    # 卖 Entropy 触发线")
            print(f"   lower_bps: {hurdle + prem_med:.1f}    # 反向，先按对称给")
        else:
            print(f"   upper_bps: {hurdle - prem_med:.1f}    # 反向，先按对称给")
            print(f"   lower_bps: {hurdle + prem_med:.1f}    # 买 Entropy 触发线")
        print(f"\n 口径提醒：引擎在门槛之上再叠 {a.fees_bps} bps 手续费，"
              f"所以 midline+upper = 门槛 {g:.1f} - {a.fees_bps} = {hurdle:.1f}")
    else:
        print("\n" + "=" * 74)
        print(" 结论：在任何候选门槛下都没有一笔完整往返 —— 这个品种目前无利可图")
        print("=" * 74)
        print(f" 往返要 {fees_rt:.1f} bps，实测 sell 峰值上限 "
              f"{max(r['s_max'] for r in rows):.2f} / buy 峰值上限 "
              f"{max(r['b_max'] for r in rows):.2f}")
        print(" 出路：① 继续采满 24~72h 找盘中窗口 ② 换品种（ANTH/OAI）"
              " ③ 改代码走 maker 把 4.5 降到 1.5")


if __name__ == "__main__":
    main()
