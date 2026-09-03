# entropy-arb 上线准备清单（Entropy × Lighter-RH）

> 目标：**新服务器还没好，先把所有能在本地做完的事做完。**
> 优先级：① 资金安全 ② 收益。
> 本清单按「先安全、后收益」排序，每一层都写明 **做什么 / 为什么 / 怎么验证**。

---

## 0. 先说三个必须知道的结论（决定了后面所有准备动作）

### 结论 1：钱不会被转走，但会被「亏掉」——防护重点是资金隔离，不是提现限制

我查了两家的密钥权限边界，结果和直觉相反：

| 场所 | 凭证类型 | 能提现吗 | 真实风险 |
|---|---|---|---|
| Hyperliquid (Entropy) | agent wallet 私钥 | **不能**（协议级禁止） | ⚠️ **agent 是用主账户的余额在交易** |
| Lighter RH | API key (Poseidon-Schnorr) | 能，但**只能提到创建该账户的 L1 地址**（协议级限制） | 该账户余额是敞口 |

**关键发现：HL 的 agent wallet 本身余额为 0，它下单消耗的是你主账户的钱。**

这意味着 `max_position_usd: 600` **只是策略层的软限制**——它挡不住参数填错、挡不住 bug、挡不住密钥泄露后被恶意下单。你的主账户里有多少 USDC，理论敞口就有多大。

**所以第一条、也是最重要的一条准备工作：给这套策略开独立的、只放小额资金的账户。** 这是唯一真正有效的硬隔离。

### 结论 2：config 里没有「日亏损熔断」，也没有「全局敞口上限」

我把 `config.example.yaml` 全部风控项过了一遍，实际只有：

| 有 | 没有 |
|---|---|
| `max_position_usd`（每场所、软限制） | ❌ 日/总亏损 kill switch |
| `max_orders_per_min` | ❌ 全局净敞口上限 |
| `max_order_notional_usd` | ❌ 单笔最大亏损 |
| `leg_slippage_bps` / `hedge_slippage_bps` | ❌ 连续亏损熔断 |
| `max_consecutive_errors` | ❌ 账户层（而非策略层）的对账 |

对策：**用进程外的 `guard.py` 补上**（见第 3 层）。它从交易所侧独立读持仓和权益，不信任 bot 自己报的数字。

### 结论 3：收益预期要现实——这是一分钱一分钱捡的策略

按实测费率（Entropy taker **4.5 bps**，Lighter-RH 0 bps）和当前参数（upper 2.0 + lower 4.0）：

- 一次完整往返**扣费后净赚 6 bps**
- 单笔 $50 → 单往返 **≈ $0.03**
- 若每天 10 次往返 → ≈ $0.3/天

$600 仓位下理论年化看着有十几个点，但**真实频率会远低于 10 次/天**——15 bps 的振幅要求不是随时都有。

> 收益来自「高频率 × 小价差」，不是「单次暴利」。想提收益只能靠：**提高 cap 和单笔规模**（风险同比例上升）、**加更多品种/更多对冲腿**（工作量大幅上升）、**降低费率**（staking 或换场所）。
>
> ⚠️ 在跑满 24~72 小时采集数据之前，任何收益估算都是纸面数字。

---

## 1. 第一层：账户与资金隔离（最重要，先做这个）

### 1.1 Hyperliquid / Entropy

- [ ] **新开一个独立的 HL 主账户**（不是你现在的常用账户），或者用子账户
  - 子账户要求：主账户终身交易量 ≥ $100,000（在 HL 界面确认你是否符合）
  - 不符合就**直接开全新钱包**——更彻底，推荐
- [ ] 该账户**只放套利资金**，建议首次 $300~600（对应 `max_position_usd: 600` 的两腿占用）
  - 两腿各需要一个 cap，实际占用 ≈ 2 × cap，留 20% 保证金余量
- [ ] 在 https://app.hyperliquid.xyz/API 创建 **agent wallet**，记下：
  - `HL_ACCOUNT_ADDRESS`（主账户地址）
  - `HL_PRIVATE_KEY`（**agent** 的私钥，不是主账户私钥）
- [ ] 确认主账户**其他资金已隔离** —— 既然用了独立账户，这条自动满足
- [ ] **给 `io` dex 的清算所单独注资**（HL 上每个 builder dex 有独立清算所，实测 `dex=io` 与主 perps 的 `accountValue` 是分开的）

### 1.2 Lighter Robinhood 链

- [ ] 在 Robinhood 链上创建**子账户**（sub-account），不要直接用主账户
- [ ] 为该子账户注册 API key，**`apiKeyIndex` 用 ≥ 2**（0/1 被前端界面占用，冲突会导致你网页端被踢下线）
- [ ] ⚠️ **主网和 RH 链是两套独立账户和 key**——主网的 key 连 RH 链会报 `API key check failed`
- [ ] 记下：`LIGHTER_ACCOUNT_INDEX`、`LIGHTER_API_KEY_INDEX`、`LIGHTER_API_PRIVATE_KEY`

### 1.3 密钥保管

- [ ] `.env` 权限设为 `600`，且**绝不能进 git**（确认 `.gitignore` 含 `.env`）
- [ ] 私钥只在服务器上存在一份，不在聊天、笔记、截图里留存
- [ ] 记录「如何吊销」：HL agent wallet 可随时在主账户界面撤销；Lighter key 可在界面 revoke

**验证方式**：`guard.py --dump` 能正确读到两个账户的权益和持仓，且金额与你预期一致。

---

## 2. 第二层：服务器（还没好，先把选型定下来）

### 2.1 区域选择（影响延迟，实测再定）

你的策略是秒级信号（`premium_persist_sec=0.3`），不是 HFT，所以延迟只影响 adverse selection，不影响能不能抢到。但仍值得测。

候选：东京 / 新加坡 / 硅谷 / 法兰克福 / 弗吉尼亚。

**服务器起来后第一件事**——在每台候选机器跑（这个脚本已有）：

```bash
python3 deploy/entropy-rh/preflight.py --symbol SNDK --config config.rh.yaml
```

看第 5 段「ws 链路延迟」。**两边延迟差 > 300ms 就换区域**。

> 参考数据：我这边国内直连实测 HL 首帧 400~850ms、RH 100~135ms，严重不对称。你的 VPS 上大概率好很多，但必须自己测。

### 2.2 规格

| 项 | 建议 | 说明 |
|---|---|---|
| CPU | 2 核 | 1 核够跑，2 核留余量给 dashboard 和 guard |
| 内存 | 2 GB | Python + asyncio + book 缓存，1G 偏紧 |
| 磁盘 | ≥ 20 GB | `logs/minutes.csv` 持续写，长期会涨 |
| 系统 | Ubuntu 22.04 / 24.04 LTS | 有 `/proc`，guard 的进程检测走这条路最稳 |

### 2.3 新服务器初始化（一条命令，别手敲）

服务器到手后，**在上面**（root 或 sudo）跑一条命令就够了：

```bash
sudo bash bootstrap-server.sh \
    --user arbuser \
    --repo git@github.com:YOURNAME/entropy-arb.git
```

它做 7 件事：装系统包 → 建非 root 用户 → SSH 加固 → ufw 防火墙 → 建目录/时区/日志轮转 → 克隆仓库+建 venv+装依赖 → 环境自检。幂等，重复跑安全。

先看会做什么（**强烈建议先跑一次**）：

```bash
sudo bash bootstrap-server.sh --dry-run --user arbuser
```

#### ⚠️ 内置了一个防呆保护

**禁用密码登录前，会先检查 `~/.ssh/authorized_keys` 里有没有公钥。**

没有公钥就禁密码 = 把自己永久锁在服务器外面，无法恢复。脚本检测到没 key 会**自动跳过 SSH 加固**并提示你先：

```bash
ssh-copy-id arbuser@<服务器IP>
```

之后重跑脚本即可。想强制跳过加固用 `--skip-harden`。

> 这个脚本已实测：dry-run 零副作用、SSH 加固两个分支（有 key / 无 key）行为正确、第 6 段克隆+venv+装依赖真跑通过（并在新 venv 里成功加载了 `config.rh.yaml`）。

- [ ] 初始化跑完，SSH 用新用户能登进去（**另开一个终端验证后再断当前连接**）
- [ ] 确认 `.env` 不在任何 git 仓库里（`git status` 不显示它）
- [ ] `chmod 600 ~/entropy-arb/.env`

---

## 3. 第三层：代码 —— 用 fork，不要 copy 后改

**结论：fork，不要 copy。**

理由不是洁癖，是三条实际收益：

1. **上游会更新**。这个仓库的接口适配（HL / zkLighter ws 协议）会随交易所变化而修。copy 之后你永远拿不到这些修复，除非手工比对。
2. **你的改动可审计**。做动态价差改造时要改 `engine.py`。用 fork 可以 `git diff vendor/upstream` 一眼看清「我到底改了什么」——这在排查亏损时价值极大。
3. **可回滚**。出问题能一键回到上一个验证过的 commit。

### 推荐的仓库结构

```
你自己的 GitHub: yourname/entropy-arb   (fork)
├── vendor/upstream    ← 100% 同步上游，永不手动修改
└── main               ← 生产分支 = upstream + 你自己的 patch
```

`vendor/upstream` 分支的存在意义：**随时能 diff 出「上游改了什么」和「我改了什么」**，两件事分开看。

### 一条命令搭好

```bash
bash deploy/entropy-rh/setup-repo.sh --fork-url git@github.com:YOURNAME/entropy-arb.git
```

不做 fork 也可以（比如想用私有仓库），脚本支持 `--no-fork` 模式：本地初始化 + 加 upstream remote，之后你自己 push 到任意私有仓库。

### 生产必须 pin commit

**永远不要用分支最新代码跑实盘。** 当前验证过的上游 commit：

```
aa0391471f6bf72f78c45801fb8117b7bf7e8c89   (2026-08-26)
```

部署时固定到这个 commit；上游有更新时，先在 `vendor/upstream` 上 fetch，diff 看过再决定要不要 merge 进 `main`，merge 完重跑一遍测试才上机。

---

## 4. 第四层：配置与参数（本地可以全部做完）

已经做好的：

- [x] `config.rh.yaml` —— 已通过仓库严格 schema 校验（实测 `load_config` 通过）
- [x] 费率修正为 **4.5 bps**（实测 `userCrossRate=0.00045`，不是 2.5）
- [x] `min_order_notional_usd: 20`（Lighter SNDK 的 `min_base=0.01` ≈ $15.4，填 10 会被拒单）

### 4.1 风控参数复核（按你的实际资金量调）

`config.rh.yaml` 里这几项**必须按你的隔离账户金额重新核**：

| 参数 | 当前值 | 怎么定 |
|---|---|---|
| `entropy.max_position_usd` | 600 | 两腿各一个 cap，账户资金 ≥ 2 × cap × 1.2 |
| `hedge.max_position_usd` | 600 | 同上 |
| `sizing.max_order_notional_usd` | 50 | 首次建议保持 50，稳定后再提 |
| `hedge.max_orders_per_min` | 30 | Lighter 上限约 40，别顶满 |
| `execution.max_consecutive_errors` | 3 | 保持 3，这是引擎的自我保护 |

### 4.2 为 guard 设定红线（按你的风险承受度）

写在 `.env` 里，guard 会自动读取：

```bash
GUARD_MAX_LEG_USD=800        # 单腿名义上限（略高于 config 的 600，留出滞后余量）
GUARD_MAX_NET_USD=150        # 净敞口上限 —— 超过说明对冲失败
GUARD_MAX_DAY_LOSS_USD=40    # 日亏红线，触发即停机
GUARD_MIN_EQUITY_USD=200     # 权益下限
GUARD_WEBHOOK_URL=           # 可选：企业微信/钉钉/Telegram 中转，CRIT 时推送
```

**`GUARD_MAX_NET_USD` 是最有价值的一项**——它直接抓「一腿成交、另一腿没跟上」这个最真实的亏损场景。

### 4.3 阈值先别急着定

`midline/upper/lower` **必须等采集完数据再定**。现在硬填一个数就开仓 = 主动送钱（README 自己写明：midline 填错 = 持续亏钱）。

流程：

```bash
# 第一步：只采集，不下单（无需密钥，零风险）
python3 main.py --record-only --symbol SNDK --hedge lighter-rh

# 跑满 24~72 小时（覆盖美股盘中和盘外两种 regime）

# 第二步：分析出阈值
python3 tools/analyze.py --fees-bps 4.5
```

> 我实测过当前溢价在 -5.8 ~ -6.8 bps，距旧的 midline -3.7 约 -2~-3 bps，在带宽内。这是**采样时刻的快照，不是你的 midline**，必须自己采满一天。

---

## 5. 第五层：监控与应急（本地写好，服务器上直接挂）

### 5.1 guard 的三种跑法

```bash
# A. cron 每 2 分钟检查一次（推荐，最省事，guard 崩了也不影响 bot）
*/2 * * * * cd ~/entropy-arb && python3 deploy/entropy-rh/guard.py \
    --check --enforce --expect-running >> logs/guard.log 2>&1

# B. 常驻监控
python3 deploy/entropy-rh/guard.py --watch --interval 60 --enforce

# C. 人工核对
python3 deploy/entropy-rh/guard.py --check
```

退出码：`0` 正常 / `1` 有告警 / `2` 越红线 / `3` 脚本自身失败（**3 要当作异常看待**，说明查不到账户状态，等于失去监控）。

### 5.2 为什么 guard 不做自动平仓

自动平仓需要 guard 持有私钥 → 一旦 guard 被攻破，它就成了新的攻击面，而它现在**连单都下不了**。

这是有意的设计取舍：

> guard 只做「**停止继续开仓**」+「**大声告警**」，平仓由人决定。

它的独立性正是价值所在——**不信任 bot 报的任何数字**，直接问交易所。

### 5.3 应急预案（先写好，出事不慌）

| 情况 | 动作 |
|---|---|
| guard 报 CRIT-净敞口 | bot 已停。**手动**把大的一边减到与另一边相当 |
| guard 报 CRIT-日亏损 | bot 已停。当天别重启，先复盘 `logs/trades.csv` |
| guard 退出码 3 | 失去监控。立即人工查账户，考虑先停 bot |
| 引擎 `halted` | 连续 3 次执行失败，引擎自停。看 `logs/engine.log` 找原因 |
| 私钥疑似泄露 | HL：主账户界面撤销 agent；Lighter：界面 revoke key。**先吊销再排查** |

---

## 6. 准备工作的执行顺序

```
【阶段 A】现在本地就能做 —— 不依赖服务器
├── [1] 开独立账户（HL 独立钱包 + Lighter 子账户，apiKeyIndex ≥ 2）  ← 最重要
├── [2] fork 仓库 + pin commit                                      ← bash setup-repo.sh
├── [3] 复核 config.rh.yaml 的 cap 与你的账户金额匹配
└── [4] 设定 guard 红线阈值，写进 .env

【阶段 B】新服务器到手 —— 大约 10 分钟
├── [5] sudo bash bootstrap-server.sh --dry-run --user arbuser      ← 先看
├── [6] sudo bash bootstrap-server.sh --user arbuser --repo <fork>  ← 再真跑
└── [7] 传部署包：scp -r deploy/entropy-rh arbuser@<IP>:~/entropy-arb/deploy/

【阶段 C】上机验证 —— 别急着开仓
├── [8] 跑 preflight.py —— 重点看延迟和费率对账
├── [9] --record-only 采集 24~72 小时                               ← 零风险，别跳过
├── [10] tools/analyze.py 出阈值，填回 config
├── [11] 挂上 guard 的 cron
└── [12] 最小仓 live，$50 单笔先跑 3 天看实际成交质量
```

---

## 附：本次新增/修改的文件

| 文件 | 作用 |
|---|---|
| `bootstrap-server.sh` | **新服务器一键初始化**（安全加固 + 依赖 + 仓库）— 已实测 |
| `guard.py` | **进程外资金安全看门狗**（只读、无私钥、可自动停机）— 已实跑验证 |
| `setup-repo.sh` | 一键搭好 fork + 双分支 + pin commit |
| `PREP.md` | 本文档 |
| `config.rh.yaml` | 费率与最小名义已修正，已通过 schema 校验 |
| `preflight.py` | 含费率对账、延迟实测 |
| `DEPLOY.md` | 12 节上线手册 |
| `install.sh` / `run.sh` | 装配置 / record-live-stop-status-logs |

> **免责声明**：本文档基于公开接口实测与代码分析，仅供参考，不构成投资建议。自动化交易软件以真实资金运行，存在本金全损风险。上机前请充分测试，仓位控制在可承受亏损范围内。
