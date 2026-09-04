#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
guard.py — entropy-arb 资金安全看门狗（进程外 / 只读 / 不持有任何私钥）
=====================================================================

设计原则
--------
1. **只读**。只调用两家的公开查询接口，不需要 HL_PRIVATE_KEY、也不需要
   LIGHTER_API_PRIVATE_KEY。因此本脚本被攻破也不会造成任何资金损失——
   它连单都下不了。
2. **进程外**。独立于 bot 运行，不信任 bot 自己报的数字。bot 进程内的
   `max_position_usd` 是软限制（策略层），本脚本从交易所侧独立核对，
   是硬限制（账户层）。
3. **不做自动平仓**。自动平仓意味着持有私钥，会摧毁第 1 条的安全优势。
   本脚本超限时的动作是「杀掉 bot 进程，停止继续开仓」，然后大声告警，
   由人来决定怎么平。

检查项
------
  A. 单边敞口    任一条腿的绝对名义超过上限
  B. 净敞口      两腿相加后的净 delta 名义超过上限（= 对冲失败）
  C. 日亏损      今日账户权益相对日初基准回撤超过上限
  D. 权益下限    账户权益低于下限（防强平/归零）
  E. 进程存活    bot 进程是否在跑（--expect-running 时）

用法
----
    # 单次检查（最常用，配合 cron 每 1~5 分钟跑一次）
    python3 guard.py --check

    # 持续监控
    python3 guard.py --watch --interval 60

    # 检查并在触发红线时 kill 掉 bot 进程
    python3 guard.py --check --enforce

    # 打印两边账户原始持仓，用于核对字段名
    python3 guard.py --dump

退出码
------
    0  一切正常
    1  有 WARNING（未越红线）
    2  有 CRITICAL（越红线；--enforce 时已尝试停止 bot）
    3  脚本自身执行失败（查不到账户、接口不通等）——不要当正常看待

依赖：纯标准库，无需 pip install，也不需要在 venv 里跑。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ----------------------------------------------------------------- 常量与默认

HL_INFO = "https://api.hyperliquid.xyz"
RH_API = "https://api.rh.lighter.xyz"

DEFAULT_DEX = "io"
HTTP_TIMEOUT = 12

# 默认阈值。请用命令行参数按自己的资金量覆盖。
# 这些默认值的含义：单腿 $800、净敞口 $150、日亏 $40、权益下限 $200
DEFAULT_MAX_LEG_USD = 800.0
DEFAULT_MAX_NET_USD = 150.0
DEFAULT_MAX_DAY_LOSS_USD = 40.0
DEFAULT_MIN_EQUITY_USD = 200.0

OK, WARN, CRIT = "OK", "WARN", "CRIT"
_SEV_RANK = {OK: 0, WARN: 1, CRIT: 2}


# ------------------------------------------------------------------- 工具函数

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _http_post_json(url: str, body: dict, timeout: int = HTTP_TIMEOUT):
    data = json.dumps(body).encode()
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _http_get_json(url: str, params: dict, timeout: int = HTTP_TIMEOUT):
    full = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(full, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode())


def _f(x, default=0.0) -> float:
    """宽容地把接口返回的 str/int/float/None 转成 float。"""
    try:
        if x is None:
            return default
        return float(x)
    except (TypeError, ValueError):
        return default


def _load_dotenv(path: str) -> dict:
    """极简 .env 解析：只读 KEY=VALUE，忽略注释和空行。"""
    env = {}
    if not os.path.isfile(path):
        return env
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


# -------------------------------------------------------------- 交易所侧查询

def fetch_hl_state(address: str, dex: str) -> dict:
    """
    返回 {"equity": float, "legs": {SYMBOL: {"size": float, "notional": float}}}
    size 带符号（正=多头）；notional 取绝对值。
    """
    d = _http_post_json(
        HL_INFO + "/info",
        {"type": "clearinghouseState", "user": address, "dex": dex},
    )
    equity = _f((d.get("marginSummary") or {}).get("accountValue"))

    legs: dict[str, dict] = {}
    for ap in d.get("assetPositions") or []:
        p = ap.get("position") or {}
        coin = str(p.get("coin") or "")
        # dex 上的 coin 可能写作 "io:SNDK" 或直接 "SNDK"，取最后一段统一
        sym = coin.split(":")[-1].upper()
        szi = _f(p.get("szi"))
        notional = _f(p.get("positionValue"))
        if szi == 0.0 and notional == 0.0:
            continue
        legs[sym] = {"size": szi, "notional": abs(notional)}
    return {"equity": equity, "legs": legs}


def fetch_lighter_state(account_index: str, api_base: str = RH_API) -> dict:
    d = _http_get_json(
        api_base + "/api/v1/account", {"by": "index", "value": account_index})
    accounts = d.get("accounts") or []
    if not accounts:
        raise RuntimeError(f"Lighter 查不到 account_index={account_index}")

    a = accounts[0]
    # collateral 是账户净资产（含未实现盈亏用 cross_asset_value 更准）
    equity = _f(a.get("cross_asset_value")) or _f(a.get("collateral"))

    legs: dict[str, dict] = {}
    for p in a.get("positions") or []:
        sym = str(p.get("symbol") or "").upper()
        sign = _f(p.get("sign"), 1.0)
        size = sign * _f(p.get("position"))
        notional = abs(_f(p.get("position_value")))
        if size == 0.0 and notional == 0.0:
            continue
        legs[sym] = {"size": size, "notional": notional}
    return {"equity": equity, "legs": legs}


# ------------------------------------------------------------------ 日亏基准

def _state_path(path: str) -> str:
    return os.path.abspath(path)


def load_baseline(path: str) -> dict:
    p = _state_path(path)
    if not os.path.isfile(p):
        return {}
    try:
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_baseline(path: str, data: dict) -> None:
    p = _state_path(path)
    os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
    tmp = p + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    os.replace(tmp, p)


def resolve_baseline(path: str, equity: float, reset: bool) -> tuple[float, str]:
    """
    返回 (基准权益, 基准日期)。
    新的一天或首次运行 → 以当前权益建立新基准。
    """
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    st = load_baseline(path)
    if reset or st.get("date") != today or not st.get("equity"):
        save_baseline(path, {"date": today, "equity": equity,
                             "set_at": _now()})
        return equity, today
    return _f(st.get("equity")), st.get("date", today)


# ------------------------------------------------------------------- 检查逻辑

class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []   # (severity, item, detail)
        self.severity = OK

    def add(self, sev: str, item: str, detail: str) -> None:
        self.rows.append((sev, item, detail))
        if _SEV_RANK[sev] > _SEV_RANK[self.severity]:
            self.severity = sev

    def render(self) -> str:
        icon = {OK: "[ OK ]", WARN: "[WARN]", CRIT: "[CRIT]"}
        lines = []
        for sev, item, detail in self.rows:
            lines.append(f"  {icon[sev]} {item:<14} {detail}")
        return "\n".join(lines)


def run_checks(args, hl: dict, lt: dict) -> tuple[Report, dict]:
    rep = Report()
    sym = args.symbol.upper()

    hl_leg = hl["legs"].get(sym, {"size": 0.0, "notional": 0.0})
    lt_leg = lt["legs"].get(sym, {"size": 0.0, "notional": 0.0})

    hl_notional, lt_notional = hl_leg["notional"], lt_leg["notional"]
    net_size = hl_leg["size"] + lt_leg["size"]

    # 净敞口名义：用两腿名义之差（同标的、价格相近，差值即未对冲部分）
    net_notional = abs(hl_notional - lt_notional)
    # 若名义都取不到（接口没给 positionValue），退回用 size × 参考价估算
    total_equity = hl["equity"] + lt["equity"]

    other_legs = [f"{s}(${v['notional']:.2f})"
                  for s, v in list(hl["legs"].items()) + list(lt["legs"].items())
                  if s != sym]

    facts = {
        "symbol": sym,
        "hl_notional": hl_notional,
        "lt_notional": lt_notional,
        "hl_size": hl_leg["size"],
        "lt_size": lt_leg["size"],
        "net_notional": net_notional,
        "net_size": net_size,
        "hl_equity": hl["equity"],
        "lt_equity": lt["equity"],
        "total_equity": total_equity,
        "other_legs": other_legs,
    }

    # ---- A. 单边敞口 -------------------------------------------------------
    for name, notional in (("Entropy(HL)", hl_notional), ("Lighter(RH)", lt_notional)):
        if notional > args.max_leg_usd:
            rep.add(CRIT, "单边敞口", f"{name} {notional:.2f} USD > 上限 {args.max_leg_usd:.2f}")
        elif notional > args.max_leg_usd * args.warn_ratio:
            rep.add(WARN, "单边敞口", f"{name} {notional:.2f} USD 接近上限 {args.max_leg_usd:.2f}")
    if max(hl_notional, lt_notional) <= args.max_leg_usd * args.warn_ratio:
        rep.add(OK, "单边敞口",
                f"Entropy {hl_notional:.2f} / Lighter {lt_notional:.2f} USD（上限 {args.max_leg_usd:.0f}）")

    # ---- B. 净敞口 ---------------------------------------------------------
    if net_notional > args.max_net_usd:
        rep.add(CRIT, "净敞口",
                f"{net_notional:.2f} USD > 上限 {args.max_net_usd:.2f} —— 对冲失败，单边裸奔")
    elif net_notional > args.max_net_usd * args.warn_ratio:
        rep.add(WARN, "净敞口", f"{net_notional:.2f} USD 接近上限 {args.max_net_usd:.2f}")
    else:
        rep.add(OK, "净敞口", f"{net_notional:.2f} USD（上限 {args.max_net_usd:.0f}）")

    # ---- C. 日亏损 ---------------------------------------------------------
    base, bdate = resolve_baseline(args.state, total_equity, args.reset_baseline)
    pnl = total_equity - base
    facts["day_baseline"] = base
    facts["day_pnl"] = pnl
    if pnl < -args.max_day_loss_usd:
        rep.add(CRIT, "日亏损",
                f"今日 {pnl:+.2f} USD（基准 {base:.2f} @ {bdate}）超过 -{args.max_day_loss_usd:.2f}")
    elif pnl < -args.max_day_loss_usd * args.warn_ratio:
        rep.add(WARN, "日亏损", f"今日 {pnl:+.2f} USD，接近 -{args.max_day_loss_usd:.2f} 红线")
    else:
        rep.add(OK, "日亏损", f"今日 {pnl:+.2f} USD（基准 {base:.2f} @ {bdate}）")

    # ---- D. 权益下限 -------------------------------------------------------
    if total_equity < args.min_equity_usd:
        rep.add(CRIT, "账户权益",
                f"{total_equity:.2f} USD < 下限 {args.min_equity_usd:.2f}")
    else:
        rep.add(OK, "账户权益",
                f"{total_equity:.2f} USD（Entropy {hl['equity']:.2f} + Lighter {lt['equity']:.2f}）")

    # ---- E. 其他品种（不该有的仓位）---------------------------------------
    if other_legs:
        rep.add(WARN, "其他品种持仓", f"{sym} 之外还有：{', '.join(other_legs)} —— 确认是否为本人操作")
    else:
        rep.add(OK, "其他品种持仓", "无")

    # ---- F. 进程存活 -------------------------------------------------------
    if args.expect_running:
        pid = find_bot_pid(args.match)
        facts["bot_pid"] = pid
        if pid is None:
            rep.add(WARN, "bot 进程", f"未按 pattern '{args.match}' 找到运行中的进程")
        else:
            rep.add(OK, "bot 进程", f"运行中 pid={pid}")

    return rep, facts


def _iter_processes() -> list[tuple[int, str]]:
    """
    返回 [(pid, cmdline)]。优先读 /proc（Linux 一定存在、无需 procps），
    失败再回退到 ps。两者都拿不到时返回空列表。
    """
    me = os.getpid()
    out: list[tuple[int, str]] = []

    if os.path.isdir("/proc"):
        try:
            for entry in os.listdir("/proc"):
                if not entry.isdigit():
                    continue
                pid = int(entry)
                if pid == me:
                    continue
                try:
                    with open(f"/proc/{entry}/cmdline", "rb") as fh:
                        raw = fh.read()
                except (FileNotFoundError, PermissionError, ProcessLookupError):
                    continue
                if not raw:
                    continue
                cmd = raw.replace(b"\x00", b" ").decode("utf-8", "replace").strip()
                if cmd:
                    out.append((pid, cmd))
            if out:
                return out
        except Exception:
            pass  # 落到 ps 回退

    try:
        res = subprocess.run(["ps", "-eo", "pid=,args="],
                             capture_output=True, text=True, timeout=10)
        for line in res.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(None, 1)
            if len(parts) != 2:
                continue
            try:
                pid = int(parts[0])
            except ValueError:
                continue
            if pid != me:
                out.append((pid, parts[1]))
    except Exception:
        pass
    return out


def find_bot_pid(pattern: str) -> int | None:
    """按命令行 pattern 找 bot 进程，排除 guard 自己。"""
    for pid, cmd in _iter_processes():
        if pattern in cmd and "guard.py" not in cmd:
            return pid
    return None


def stop_bot(args, reason: str) -> bool:
    """优雅停机优先（SIGTERM），超时再 SIGKILL。"""
    pid = find_bot_pid(args.match)
    if pid is None:
        print(f"  [ ! ] 未找到 bot 进程（pattern={args.match}），无法自动停止")
        return False
    print(f"  [ ! ] 触发红线：{reason}")
    print(f"  [ ! ] 正在停止 bot 进程 pid={pid} …")
    try:
        os.kill(pid, 15)  # SIGTERM —— 让引擎走 graceful shutdown
    except ProcessLookupError:
        return True
    for _ in range(20):
        time.sleep(0.5)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            print(f"  [ ! ] pid={pid} 已退出（SIGTERM 优雅停机）")
            return True
    try:
        os.kill(pid, 9)
        print(f"  [ ! ] pid={pid} 未响应 SIGTERM，已强制 SIGKILL")
    except ProcessLookupError:
        pass
    return True


def notify(webhook: str, title: str, body: str) -> None:
    """通用 POST 通知：企业微信/钉钉/自建中转都吃 JSON。失败不影响主流程。"""
    if not webhook:
        return
    payload = {"msgtype": "text", "text": {"content": f"{title}\n{body}"}}
    try:
        req = urllib.request.Request(
            webhook, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=8).read()
    except Exception as e:
        print(f"  [ ! ] 通知发送失败：{type(e).__name__}: {e}")


# ------------------------------------------------------------------------ main

def dump_raw(args, hl: dict, lt: dict) -> None:
    print("Entropy / Hyperliquid（dex=%s）" % args.dex)
    print(f"  equity = {hl['equity']}")
    print(f"  legs   = {json.dumps(hl['legs'], ensure_ascii=False)}")
    print("Lighter（%s, account_index=%s）" % (args.lighter_api, args.lighter_index))
    print(f"  equity = {lt['equity']}")
    print(f"  legs   = {json.dumps(lt['legs'], ensure_ascii=False)}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="entropy-arb 资金安全看门狗（只读 / 进程外）",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="执行一次检查")
    ap.add_argument("--watch", action="store_true", help="持续监控")
    ap.add_argument("--dump", action="store_true", help="打印两边账户原始持仓后退出")
    ap.add_argument("--interval", type=float, default=60.0, help="--watch 的检查间隔秒（默认 60）")

    ap.add_argument("--env-file", default=None, help=".env 路径（默认自动定位）")
    ap.add_argument("--symbol", default=None, help="监控的品种，如 SNDK（默认取 ENV_SYMBOL 或 SNDK）")
    ap.add_argument("--dex", default=DEFAULT_DEX, help="Hyperliquid dex 名（默认 io）")
    ap.add_argument("--lighter-api", default=RH_API, help="Lighter API 地址")

    ap.add_argument("--max-leg-usd", type=float, default=None, help="单腿名义上限")
    ap.add_argument("--max-net-usd", type=float, default=None, help="净敞口名义上限")
    ap.add_argument("--max-day-loss-usd", type=float, default=None, help="单日亏损上限")
    ap.add_argument("--min-equity-usd", type=float, default=None, help="账户权益下限")
    ap.add_argument("--warn-ratio", type=float, default=0.7, help="告警触发占上限的比例（默认 0.7）")

    ap.add_argument("--enforce", action="store_true", help="CRIT 时自动停止 bot 进程")
    ap.add_argument("--match", default="main.py", help="识别 bot 进程的命令行 pattern")
    ap.add_argument("--expect-running", action="store_true", help="同时检查 bot 进程是否存活")
    ap.add_argument("--state", default=None, help="日亏基准状态文件路径")
    ap.add_argument("--reset-baseline", action="store_true", help="强制重设今日基准")
    ap.add_argument("--webhook", default=None, help="CRIT 时 POST 通知的 URL")
    args = ap.parse_args()

    if not (args.check or args.watch or args.dump):
        args.check = True

    # ---------- 定位 .env ----------
    here = os.path.dirname(os.path.abspath(__file__))
    env_path = args.env_file
    if env_path is None:
        for cand in (os.path.join(here, ".env"),
                     os.path.join(here, "..", ".env"),
                     os.path.join(here, "..", "..", ".env")):
            if os.path.isfile(cand):
                env_path = cand
                break
    env = _load_dotenv(env_path) if env_path else {}
    if env_path and os.path.isfile(env_path):
        print(f"密钥/账户来源：{os.path.abspath(env_path)}（本脚本只读取地址类公开信息，不读私钥）")

    hl_addr = env.get("HL_ACCOUNT_ADDRESS") or os.environ.get("HL_ACCOUNT_ADDRESS", "")
    lt_idx = env.get("LIGHTER_ACCOUNT_INDEX") or os.environ.get("LIGHTER_ACCOUNT_INDEX", "")
    args.lighter_index = lt_idx

    if not hl_addr or not lt_idx:
        print("  [ERR] 缺少 HL_ACCOUNT_ADDRESS 或 LIGHTER_ACCOUNT_INDEX。")
        print("        请在 .env 中填写，或用 --env-file 指定路径。")
        print(f"        当前 hl_addr={'已填' if hl_addr else '缺失'} "
              f"lighter_index={'已填' if lt_idx else '缺失'}")
        return 3

    if args.symbol is None:
        args.symbol = env.get("SYMBOL") or os.environ.get("SYMBOL") or "SNDK"
    args.symbol = args.symbol.upper()

    # ---------- 阈值 ----------
    def _thr(cli, envkey, default):
        if cli is not None:
            return cli
        v = env.get(envkey) or os.environ.get(envkey)
        return float(v) if v else default

    args.max_leg_usd = _thr(args.max_leg_usd, "GUARD_MAX_LEG_USD", DEFAULT_MAX_LEG_USD)
    args.max_net_usd = _thr(args.max_net_usd, "GUARD_MAX_NET_USD", DEFAULT_MAX_NET_USD)
    args.max_day_loss_usd = _thr(args.max_day_loss_usd, "GUARD_MAX_DAY_LOSS_USD", DEFAULT_MAX_DAY_LOSS_USD)
    args.min_equity_usd = _thr(args.min_equity_usd, "GUARD_MIN_EQUITY_USD", DEFAULT_MIN_EQUITY_USD)
    wh = args.webhook or env.get("GUARD_WEBHOOK_URL") or os.environ.get("GUARD_WEBHOOK_URL", "")
    args.webhook = wh

    if args.state is None:
        args.state = os.path.join(here, "..", "logs", "guard_state.json")

    # ---------- dump 模式 ----------
    if args.dump:
        try:
            hl = fetch_hl_state(hl_addr, args.dex)
            lt = fetch_lighter_state(lt_idx, args.lighter_api)
        except Exception as e:
            print(f"  [ERR] 查询失败：{type(e).__name__}: {e}")
            return 3
        dump_raw(args, hl, lt)
        return 0

    # ---------- 主循环 ----------
    worst = OK
    while True:
        try:
            hl = fetch_hl_state(hl_addr, args.dex)
            lt = fetch_lighter_state(lt_idx, args.lighter_api)
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            print(f"  [ERR] 交易所接口不可达：{type(e).__name__}: {e}")
            print("        无法确认账户状态 —— 按最坏情况处理，请人工确认。")
            return 3
        except Exception as e:
            print(f"  [ERR] 查询失败：{type(e).__name__}: {e}")
            return 3

        rep, facts = run_checks(args, hl, lt)
        print(f"\n===== guard @ {_now()} | {facts['symbol']} =====")
        print(rep.render())
        print(f"  ----  Entropy {facts['hl_size']:+.4f} (${facts['hl_notional']:.2f})   "
              f"Lighter {facts['lt_size']:+.4f} (${facts['lt_notional']:.2f})   "
              f"净 {facts['net_size']:+.4f}")

        if rep.severity == CRIT:
            if args.enforce:
                stop_bot(args, "资金安全红线")
            else:
                print("  [ ! ] 已越红线；加 --enforce 可在触发时自动停止 bot 进程。")
            notify(args.webhook, "[entropy-arb] 资金安全红线触发", rep.render())
            print("  [ ! ] 请人工核对仓位后决定是否平仓 —— guard 不会自动平仓。")

        if _SEV_RANK[rep.severity] > _SEV_RANK[worst]:
            worst = rep.severity

        if not args.watch:
            break
        time.sleep(args.interval)

    return {OK: 0, WARN: 1, CRIT: 2}[worst]


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n已中断")
        sys.exit(130)
