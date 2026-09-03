# Entropy × Lighter-RH 上线手册（SNDK）

一步一步来，别跳步。整套流程是 **采集 → 算阈值 → 小仓实盘**，没有模拟盘，
所以每一步都是为了让你在花真钱之前把坑踩完。

> **服务器还没准备好？** 先看 [PREP.md](PREP.md) —— 那里是「上机前要备齐什么」的
> 完整清单（账户隔离、代码源、guard 红线阈值），大部分在本地就能做完。
> 本文档是「服务器好了之后怎么跑」。

---

## 0a. 上机前必读：你的钱到底暴露在哪里

这条比后面所有参数都重要，是我查了两家密钥权限边界后得出的结论，和直觉相反：

| 场所 | 凭证 | 能提现吗 | 真实风险 |
|---|---|---|---|
| Hyperliquid (Entropy) | agent wallet 私钥 | **不能**（协议级禁止） | ⚠️ **agent 花的是你主账户的余额** |
| Lighter RH | API key | 能，但**只能提到创建该账户的 L1 地址** | 该账户余额是敞口 |

**HL 的 agent wallet 自身余额为 0，它下单消耗的是主账户的钱。**

所以 `max_position_usd: 600` 只是策略层的软限制——挡不住参数填错、挡不住 bug、
挡不住密钥泄露后被恶意下单。**你主账户里有多少 USDC，理论敞口就有多大。**

结论：**风险不是「钱被转走」（两家都有协议级保护），而是「被亏掉」。
唯一真正有效的硬隔离是——给这套策略开独立的、只放小额资金的账户。**

- HL：开全新钱包（比子账户更彻底），或符合门槛的话用子账户
- Lighter RH：用子账户，API key 的 `index` 用 ≥ 2（0/1 被网页端占用）

---

## 0b. 先记住三个已经查实的数

这三条是 2026-09-02 用 `preflight.py` 打真实接口量出来的，直接决定你怎么填配置：

| 项 | 实测值 | 影响 |
|---|---|---|
| Entropy(io) taker 手续费 | **4.5 bps**（基础档，实测某账户 4.05） | 不是 0，也不是 2.5。填错会让策略结构性亏损 |
| io dex 的 `feeRecipient` | `null` | 当前不收 builder 附加费，只有 HL 基础费 |
| Lighter-RH 的 SNDK `min_base` | **0.01 枚**（≈ $15.4 @ $1540） | 最小名义必须 ≥ $20，否则 Lighter 拒单 |

其余实测参数：

- **Entropy**：`io:SNDK`，szDecimals=4，maxLeverage=10，`onlyIsolated=True`，funding 乘数 0.5，OI 上限 $10M
- **Lighter-RH**：SNDK active，`market_id=32`，chain_id=466324，价格精度 2 / 数量精度 4，
  `min_quote=10`，**taker 费 0 bps**（已与 `hedge.taker_fee_bps: 0.0` 对齐）

---

## 0c. ⚠️ 最重要的一节：门槛参数填错会「稳定亏钱」（2026-09-03 实测）

这一节是本地实跑 + 逐档验证代码得出的，**改参数前必须读完**。

### 结论：每笔净利(bps) 就精确等于「有效门槛」

引擎双腿是**同时成交、当场锁平**的，所以一次往返的净利不是 `upper + lower`，
而是各自方向的**有效门槛**（`engine.py::_eff_threshold`）：

| 方向 | 有效门槛 | 公式 |
|---|---|---|
| 卖 Entropy + 买 RH | `midline + upper` | — |
| 买 Entropy + 卖 RH | `lower - midline` | — |

用真实代码 `plan_arb` 逐档验证过（合成盘口，卡在门槛上）：

| threshold | 成交名义 | exp_edge | 净利(bps) |
|---|---|---|---|
| -1.7 | $49.98 | -$0.0085 | **-1.70** ❌ |
| 0.0 | $49.98 | $0.0000 | 0.00 |
| 2.0 | $49.98 | $0.0100 | +2.00 ✅ |
| 7.7 | $49.98 | $0.0385 | +7.70 ✅ |

**推论：`threshold` 为负 = 每笔必亏。**

### 你原来的参数自相矛盾

`midline: -3.7` + `upper: 2.0` + `lower: 4.0`：

- 卖 Entropy 方向门槛 = `-3.7 + 2.0` = **-1.7 bps → 成交即亏**
- 买 Entropy 方向门槛 = `4.0 - (-3.7)` = **+7.7 bps → 成交才赚**

也就是说：**溢价一冲高、程序去卖 Entropy，那一单就是稳定亏钱的。**
它现在没亏，只是因为溢价从没冲到 -1.7 那么高（实测在 -3.4 ~ -7.2 之间）。

### 改参数必须满足的安全约束

```
midline + upper >= 1.0      且      lower - midline >= 1.0
想两方向对称：  upper = lower - 2 * midline
```

> `tools/analyze.py` 用 `max(..., 1.0)` 兜底，永远不会推荐负门槛——**工具是安全的，
> 危险的是手填**。所以阈值请务必用它算，不要凭感觉填。

`config.rh.yaml` 里已把 `upper_bps` 从 2.0 改成 **11.4**，使两个方向门槛都变成 +7.7 bps
（保证不亏）。但这组值**几乎不会触发**——这是诚实的现状，见下。

### 更硬的问题：现在这个价差可能根本不够手续费

2026-09-03 实采 3 分钟（北京时间 09:46，即**美股盘后**），用 `analyze.py --fees-bps 4.5`：

```
   band bps |  SELL entropy |  BUY entropy
        1.0 |      0    0.0 |      0    0.0     ← 连 1 bps 净利都触发不了
        2.0 |      0    0.0 |      0    0.0
      ... 全部为 0
```

原因（同一次实测的快照）：

| 项 | 数值 |
|---|---|
| Entropy 价差 | 1.95 bps |
| RH 价差 | 1.36 bps |
| 两边半价差合计（硬损耗） | **≈ 1.65 bps** |
| Entropy taker 费 | **4.5 bps** |
| 合计成本 | **≈ 6.2 bps** |
| 实测溢价波动（std） | 约 **0.5 ~ 1.0 bps** |

**成本 6.2 bps vs 波动 1 bps —— 差一个数量级。**

⚠️ **但别急着下结论**：这 3 分钟是美股盘后，是这套策略最差的时段。
SNDK 是股票 perp，美股盘中（北京时间 21:30–04:00）波动和成交量会显著放大。
**必须采满 24~72 小时、覆盖美股盘中，再判断这个组合到底能不能做。**

如果采集后仍然是 0 触发，说明问题不在参数而在**费率**：4.5 bps taker 对这种
1 bps 级价差就是太贵了。那时的选择是降费率（HL staking）或换品种/换场所，
而不是继续调阈值——**调阈值调不出利润，只会调出亏损。**

---

## 1. 准备代码仓库（先在你本地做）

**用 fork，不要 copy 后改。** 原因：上游会修接口适配问题；fork 能让你随时
`git diff` 审计「我到底改了什么」；出问题能回滚。

先到 https://github.com/your-quantguy/entropy-arb 点右上角 Fork，然后：

```bash
cd /Users/ylh/WorkBuddy/2026-09-02-23-29-43
bash deploy/entropy-rh/setup-repo.sh --fork-url git@github.com:YOURNAME/entropy-arb.git --dir ~/entropy-arb
```

它会搭出两个分支：`vendor/upstream`（只同步上游，永不手动改）和 `main`（你的生产分支），
并把生产固定在已验证的 commit `aa03914`。

不想 fork 就用 `bash deploy/entropy-rh/setup-repo.sh --no-fork`，之后自己 push 到私有仓库。

---

## 2. 新服务器初始化 + 传文件

### 2.1 先在服务器上跑初始化脚本

把 `bootstrap-server.sh` 传上去（或者服务器能直接 wget 到你的仓库）：

```bash
# 在本机
scp deploy/entropy-rh/bootstrap-server.sh root@<新服务器IP>:/tmp/

# 在服务器上
sudo bash /tmp/bootstrap-server.sh --user arbuser \
    --repo git@github.com:YOURNAME/entropy-arb.git
```

一条命令做完：装系统包 → 建非 root 用户 → SSH 加固 → ufw 防火墙 →
建目录/时区/日志轮转 → 克隆仓库 + 建 venv + 装依赖 → 环境自检。

**建议先跑一次 `--dry-run` 看看它会做什么**（不会改动任何东西）：

```bash
sudo bash /tmp/bootstrap-server.sh --dry-run --user arbuser
```

> ⚠️ 脚本内置防呆：**没检测到 SSH 公钥时会拒绝禁用密码登录**，
> 否则你会把自己永久锁在门外。它会提示你先跑 `ssh-copy-id arbuser@<IP>`。
>
> 之后每条命令都要用 venv：`source ~/entropy-arb/.venv/bin/activate`

### 2.2 再传部署包（在本机执行）

```bash
scp -r deploy/entropy-rh arbuser@<新服务器IP>:~/entropy-arb/deploy/
```

**验证**：另开一个终端用新用户 SSH 登进去，确认能进再断开原来的连接。

---

## 3. 安装配置

```bash
cd ~/entropy-arb
bash deploy/entropy-rh/install.sh
```

脚本会**先把已有的 `config.yaml` 和 `.env` 备份成 `.bak.时间戳`** 再写入，不会裸覆盖。
`.env` 如果已存在，脚本只提示哪些键还缺，不帮你覆盖密钥。

看到最后那 5 行提示就说明装好了。

---

## 4. 填密钥

```bash
vi ~/entropy-arb/.env
```

需要这 5 个：

| 键 | 去哪拿 |
|---|---|
| `HL_PRIVATE_KEY` | https://app.hyperliquid.xyz/API 建 agent 钱包，填 **agent** 的私钥 |
| `HL_ACCOUNT_ADDRESS` | 你的 HL **主账户**地址（不是 agent 地址） |
| `LIGHTER_ACCOUNT_INDEX` | Robinhood 链账户 index |
| `LIGHTER_API_KEY_INDEX` | Robinhood 链 API key index |
| `LIGHTER_API_PRIVATE_KEY` | Robinhood 链 API key 私钥 |

**这里有个必踩的坑**：Lighter 主网和 Robinhood 链是**两套完全独立的账户和密钥**。
你在主网注册的 key 拿去连 `lighter-rh` 会报 `API key check failed`。
必须用 https://robinhoodchain.lighter.xyz 上注册的那一套。

填完收紧权限：`chmod 600 .env`

另外：Entropy 的 `io:SNDK` 是 **`onlyIsolated=True`**，资金要放进 isolated 保证金，
杠杆上限 10 倍。

---

## 5. 跑预检（关键一步，别跳过）

```bash
cd ~/entropy-arb
python3 deploy/entropy-rh/preflight.py --symbol SNDK
```

它会依次检查 6 件事，任何 **FAIL** 都要先修掉再往下走：

1. `.env` 五个键是否都填了
2. HL 上 `io` dex 和 `io:SNDK` 是否还在
3. Lighter-RH 上 SNDK 是否 active，并把 `market_id` / 精度 / 最小下单量 / 场所费率打出来
4. **taker 费率对账** —— 用你的 `HL_ACCOUNT_ADDRESS` 查真实费率，和 config 里填的值比对
5. 两条 ws 链路的真实延迟（3 次中位数）
6. 当前双边盘口 → 实时溢价 vs 你配置的 midline

第 4 步是这套脚本最值钱的地方。它会直接告诉你：

```
[FAIL] config entropy.taker_fee_bps  填 2.5，实际 4.050，差 -1.550 bps
       ↳ 往返少算 3.10 bps 成本；名义 upper+lower=6.00 bps → 只剩 2.90 bps
```

看到这个就把 `config.yaml` 里的 `entropy.taker_fee_bps` 改成脚本报的实际值，重跑一次。

**关于延迟**：预检会分别测 HL 和 RH 的首帧延迟。两边差得越多，双腿成交的时间错配越严重。
从国内直连测到的是 HL 约 400–850ms、RH 约 100ms，差得很离谱；VPS 上应该好很多。
上实盘前看一眼这个数字——**两边差异 > 300ms 建议换 VPS 区域**（这一步就是之前说的延迟优化）。

---

## 6. 采集数据（不下单，不用密钥）

```bash
bash deploy/entropy-rh/run.sh record
bash deploy/entropy-rh/run.sh status     # 看是不是活着
```

每分钟往 `logs/minutes.csv` 写一行。README 建议至少跑几小时，**跑满 24 小时更好**——
溢价有日内规律（SNDK 是股票 perp，美股盘中/盘后的 oracle 行为不一样）。

采集期间你可以先去睡觉。回来执行：

```bash
bash deploy/entropy-rh/run.sh stop
```

---

## 7. 算出阈值

```bash
python3 tools/analyze.py
```

它会打出溢价分布、各候选带宽的历史触发次数，并给一段可以直接粘进 `config.yaml` 的
`thresholds:` 块。

**把算出来的 midline 填回去**，别沿用我给的 -3.7——那是你之前某一批数据的快照。
README 说得很直白：midline 填错 = 持续亏钱。

---

## 8. 挂上资金看门狗（开实盘之前必须做）

`config.yaml` 里**没有日亏损熔断，也没有全局净敞口上限**——我逐项核过，全部风控只有
每场所的仓位上限、下单频率、滑点保护和连续错误停机，而且都是策略层的软限制。

`guard.py` 补的是**账户层的硬限制**：它跑在 bot 进程之外，直接问交易所拿持仓和权益，
不信任 bot 自己报的任何数字。

### 它为什么安全

- **只读**：只调两家公开查询接口，不需要任何私钥 —— 它连单都下不了，被攻破也无所谓
- **进程外**：独立于 bot，bot 崩了它照样能查
- **不做自动平仓**：自动平仓意味着持有私钥，会摧毁上面的安全优势。
  它只做「停止继续开仓 + 大声告警」，平仓由你决定

### 装

在 `.env` 里加红线（按你的账户金额调整）：

```bash
GUARD_MAX_LEG_USD=800        # 单腿名义上限（略高于 config 的 600，留滞后余量）
GUARD_MAX_NET_USD=150        # 净敞口上限 —— 超过说明对冲失败，最该盯的一项
GUARD_MAX_DAY_LOSS_USD=40    # 日亏红线，触发即停机
GUARD_MIN_EQUITY_USD=200     # 权益下限
GUARD_WEBHOOK_URL=           # 可选：企业微信/钉钉/Telegram 中转，CRIT 时推送
```

挂 cron（每 2 分钟一次，guard 崩了也不影响 bot）：

```bash
mkdir -p ~/entropy-arb/logs
(crontab -l 2>/dev/null; echo '*/2 * * * * cd ~/entropy-arb && python3 deploy/entropy-rh/guard.py --check --enforce --expect-running >> logs/guard.log 2>&1') | crontab -
```

### 先验一遍再信它

```bash
python3 deploy/entropy-rh/guard.py --dump      # 看它读到的持仓对不对
python3 deploy/entropy-rh/guard.py --check     # 看 5 项检查结果
```

`--dump` 会打印两边账户的原始持仓。**开实盘前先人工核对一遍金额**，确认它连的是你的
隔离账户而不是别的账户。

### 退出码

| 码 | 含义 |
|---|---|
| 0 | 正常 |
| 1 | 有 WARN |
| 2 | 越红线（加 `--enforce` 时已自动停掉 bot） |
| 3 | **脚本自身失败** —— 查不到账户状态，等于失去监控，要当异常看待 |

---

## 9. 小仓实盘

```bash
python3 -m pip install -r requirements-live.txt     # 或者 bash deploy/entropy-rh/install.sh --live
bash deploy/entropy-rh/run.sh live
```

启动前会要你手输 `yes` 确认。

**先用最小仓位验证链路**：把 `config.yaml` 里两边的 `max_position_usd` 暂时改成 100，
确认能正常开平仓、对冲、对账，再逐步加到 600。

---

## 10. 日常运维

```bash
bash deploy/entropy-rh/run.sh status    # 进程 + 最近成交 + 日志尾部
bash deploy/entropy-rh/run.sh logs      # 实时跟日志
bash deploy/entropy-rh/run.sh stop      # 优雅停止
```

**每天/每周要做的**：重跑 `tools/analyze.py`。溢价中枢会漂，midline 是这套策略唯一
会让你「在公允价值上买一整天」的失效点。

---

## 11. 已知风险（README 里的，加上这次实测的四条）

| 风险 | 说明 |
|---|---|
| **agent wallet 敞口** | ⚠️ 最容易被忽略。HL agent 花的是主账户余额，`max_position_usd` 只是软限制。**必须开独立小账户**（见 0a） |
| **config 无熔断** | 没有日亏损上限、没有净敞口上限。全部风控都是策略层软限制，**靠 `guard.py` 补硬限制**（见第 8 节） |
| **midline 失效** | 头号亏损来源。定期重测 |
| **手续费填低** | 这次实测抓到的：实际 4.5 bps。填低 = 放行亏损单 |
| **USDG 基差** | RH 链报价资产是 USDG，稳定币自身的波动是真实盈亏，midline 只能吸收「水平」不能吸收「移动」 |
| **资金费率** | 两个场所各算各的。实测 io:SNDK 的 funding 乘数是 0.5，不等于 RH 那边，carry 没有被建模，靠仓位上限兜着 |
| **薄盘口** | Entropy 深度可能很浅，`take_fraction=0.5` 和 $50 上限就是为此 |
| **单腿成交** | 一条腿成了另一条没成，引擎会自动对冲，但还是要盯 |
| **最小下单量** | 这次实测新增：SNDK 涨过 $2000 后，0.01 枚就不止 $20 了，`min_order_notional_usd` 要跟着上调 |

---

## 12. 出问题怎么查

| 现象 | 原因 / 处理 |
|---|---|
| guard 报 `CRIT 净敞口` | 一腿成交另一腿没跟上，bot 已被 guard 停掉。**手动**把大的一边减到与另一边相当 |
| guard 报 `CRIT 日亏损` | 当天别重启。先复盘 `logs/trades.csv` 找原因 |
| guard 退出码 3 | 失去监控（接口不通/查不到账户）。立即人工查账户，考虑先停 bot |
| guard 报 `其他品种持仓` | 账户里有非 SNDK 的仓位。确认是你本人的其他策略，还是异常下单 |
| 私钥疑似泄露 | **先吊销再排查**：HL 在主账户界面撤销 agent；Lighter 在界面 revoke key |
| `API key check failed` | Lighter key 注册在主网了。换 Robinhood 链的 key |
| `io:SNDK not found` | dex 改名或下架，跑预检确认 |
| 一直 `below_min_notional` | 币价涨了，上调 `min_order_notional_usd`（≈ min_base × 现价 × 1.3） |
| `RATE_LIMITED` 频发 | 调低 `hedge.max_orders_per_min`（Lighter 硬上限约 40） |
| 引擎 `halted` | 连续 3 次执行错误。看 `logs/engine.log` 尾部定位，平仓后重启 |
| 一直不开仓 | 大概率是手续费填对了之后门槛变宽（见下），或者溢价一直没走出带宽 |

---

## 附：手续费修正后，你的入场门槛变成多少

这是最需要你心里有数的一件事。引擎会把手续费叠加到门槛上，所以 `taker_fee_bps`
从 0 改到 4.5 之后，实际触发线整体外扩：

```
卖 Entropy（+买 RH）：溢价 >= midline + upper + 4.5 = -3.7 + 2.0 + 4.5 = +2.8 bps
买 Entropy（+卖 RH）：溢价 <= midline - lower - 4.5 = -3.7 - 4.0 - 4.5 = -12.2 bps
```

也就是说一次完整往返需要溢价走出 **15 bps** 的振幅，扣完成本净赚 `upper + lower = 6 bps`。
按 $50 的单笔，一个往返约 **$0.03**。

这意味着：**开仓频率会比你现在体感的低很多**。这是对的——之前如果填的是 0 或 2.5，
那些「成交」里有相当一部分实际是净亏的。
