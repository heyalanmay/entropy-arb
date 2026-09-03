# 你现在要做什么 —— 手把手操作手册

> 最后更新：2026-09-03
> 适用目标：Entropy(Hyperliquid io dex) × Lighter-Robinhood 链 双平台套利
>
> **怎么用这份文档**：从上往下做，每一步都有「做 → 验证」两半。
> **验证没过就别往下走**，否则后面所有排错都会变成猜谜。

---

## 先搞清楚：你现在卡在哪

| 步骤 | 做什么 | 依赖什么 | 能不能现在做 | 耗时 |
|---|---|---|---|---|
| **1** | fork 代码到你自己的 GitHub | 只有 GitHub 账号 | ✅ **现在就能做** | 10 分钟 |
| **2** | 开 Hyperliquid 独立账户 + 拿 key | 一个 EVM 钱包 | ✅ **现在就能做** | 30 分钟 |
| **3** | 开 Lighter RH 子账户 + 拿 key | 同一个 EVM 钱包 | ✅ **现在就能做** | 30 分钟 |
| 4 | 服务器初始化 | **服务器 IP（你还没给）** | ⏸ 等 | 15 分钟 |
| 5 | 上传代码 + 预检 | 步骤 1~4 | ⏸ 等 | 20 分钟 |
| 6 | 只采集不下单 24~72 小时 | 步骤 5 | ⏸ 等 | 1~3 天 |
| 7 | 算阈值、填 config | 步骤 6 的数据 | ⏸ 等 | 20 分钟 |
| 8 | 挂看门狗 + 小仓实盘 | 步骤 7 | ⏸ 等 | — |

**结论：今天白天你能推进的是第 1、2、3 步，全部做完。**
第 2、3 步是整个流程的**关键路径**——它要充值、要上链确认，最慢，而且**不依赖服务器**。
服务器到了也要等密钥，所以先把账户搞定，别空等。

---

# 第 1 步：把代码 fork 到你自己的 GitHub

## 为什么要先做这个

服务器的初始化脚本要填一个仓库地址，而这个地址**现在就得定下来**，否则第 4 步卡住。
用 fork 而不是 copy，是为了以后能 `git diff` 一眼看清「我改了什么」——排查亏损时这个值千金。

## 1.1 在网页上点 Fork

1. 打开 <https://github.com/your-quantguy/entropy-arb>
2. 右上角点 **Fork** → 保持默认设置 → **Create fork**
3. 几秒后你会进入自己的仓库，地址栏应该是：
   ```
   https://github.com/你的用户名/entropy-arb
   ```
   把「你的用户名」记下来，后面反复要用。

> 你现在这台机器的 git 配置是 `heyalanmay / heyalanmay@gmail.com`，
> 大概率 GitHub 用户名就是 `heyalanmay`，但**请务必以网页地址栏显示的为准**。

## 1.2 在本机终端跑一条命令

打开终端，复制粘贴（**把 `你的用户名` 换成实际的**）：

```bash
cd /Users/ylh/WorkBuddy/2026-09-02-23-29-43

bash deploy/entropy-rh/setup-repo.sh \
    --fork-url https://github.com/你的用户名/entropy-arb.git \
    --dir ./entropy-arb-fork \
    --force
```

> 用了 HTTPS 地址（`https://` 而不是 `git@`），因为你本机已经有 HTTPS 的 GitHub 凭据，
> 换成 SSH 反而可能因为没配 key 而失败。
> 放在 `entropy-arb-fork/` 是为了不覆盖你现有的 `entropy-arb/`（那份是我用来分析代码的）。

## 1.3 验证成功

命令跑完后，终端最后应该打印：

```
==============================================
 完成
==============================================
 当前分支     : main
 当前 commit  : aa0391471f6bf72f78c45801fb8117b7bf7e8c89
 vendor/upstream: aa03914
```

**最关键的一行是 `当前 commit` 必须以 `aa03914` 开头。**
如果不是，说明没 pin 到验证过的版本，不要继续。

再手动确认一次：

```bash
cd /Users/ylh/WorkBuddy/2026-09-02-23-29-43/entropy-arb-fork
git log --oneline -1        # 应显示 aa03914
git branch                  # 应显示 main 和 vendor/upstream 两行
cat deploy/entropy-rh/PINNED-COMMIT.txt
```

## 1.4 顺手做一件事：把部署文件提交进仓库

```bash
cd /Users/ylh/WorkBuddy/2026-09-02-23-29-43/entropy-arb-fork
mkdir -p deploy/entropy-rh
cp /Users/ylh/WorkBuddy/2026-09-02-23-29-43/deploy/entropy-rh/* deploy/entropy-rh/
git add deploy/entropy-rh/
git commit -m "add RH deployment kit: config, preflight, guard, bootstrap"
git push origin main
```

这样服务器上 `git clone` 下来就自带全部部署脚本，不用再手工 scp。

## ❌ 失败怎么办

| 报错 | 原因 | 解决 |
|---|---|---|
| `Repository not found` | 用户名写错，或 fork 还没建好 | 回浏览器确认地址栏 |
| `could not read Username` | GitHub 要你登录 | 用 GitHub 的 **Personal Access Token** 当密码，或改走 SSH |
| commit 不是 `aa03914` | 上游改了历史 | **停下来告诉我**，别自己改 |

---

# 第 2 步：开 Hyperliquid 独立账户（Entropy 这一腿）

## 2.1 先理解一件事：为什么必须开独立账户

Hyperliquid 的 API 钱包叫 **agent wallet**，它有个反直觉的特性：

> **agent 钱包自己余额是 0，它下的是你主账户的钱。**

所以 `config.yaml` 里的 `max_position_usd: 600` 只是策略层的**软限制**——
参数填错、代码有 bug、私钥泄露，都挡不住。你主账户里有多少 USDC，理论敞口就有多大。

**开一个全新的独立账户，只放套利要用的钱，这是唯一真正有效的硬隔离。**

## 2.2 开新钱包（推荐 MetaMask 或 Rabby）

1. 装钱包插件，新建一个**全新的**钱包（或者用钱包里的「添加账户」功能新建一个账户）
2. **把助记词抄在纸上**——这个账户以后就是你的套利专用账户
3. 记下这个钱包的 `0x...` 地址，这是你的 **`HL_ACCOUNT_ADDRESS`**

⚠️ 不要用你存放主要资金的那个钱包。

## 2.3 充值

打开 <https://app.hyperliquid.xyz>，连上新钱包 → 点 **Enable Trading** 签名激活 → **Deposit**。

需要准备：
- Arbitrum 上的 **USDC**（建议 **$300**，对应后面 config 里的两腿各 $600 cap 中的一份）
- Arbitrum 上一点 **ETH** 当 gas（几美元就够）

> 首次充值会顺带在链上创建你的 HL 账户。最低 5 USDC。

## 2.4 ⚠️ 关键：把钱转到 io dex 的清算所

**这一步最容易漏，漏了会「明明有钱但下不了单」。**

Entropy 跑在 Hyperliquid 的 `io` dex 上，而 **io dex 有自己独立的清算所**，
和你主 perps 账户的钱是**两笔账**。我实测确认过：

```bash
# 主 perps 账户
curl -s -X POST https://api.hyperliquid.xyz/info -H "Content-Type: application/json" \
  -d '{"type":"clearinghouseState","user":"0x你的地址"}'

# io dex 清算所  ← 套利实际用的是这个
curl -s -X POST https://api.hyperliquid.xyz/info -H "Content-Type: application/json" \
  -d '{"type":"clearinghouseState","user":"0x你的地址","dex":"io"}'
```

在 HL 网页界面上把 USDC 从主账户转到 io dex（找 **Portfolio / Balances** 页面里的
dex 切换或 Transfer 入口；io dex 是纯逐仓 `onlyIsolated`，所以必须先把保证金放进去）。

**验证**：上面第二条命令返回的 `marginSummary.accountValue` 必须 **> 0**。
如果还是 `0.0`，说明钱没转进去，回去找界面上的转账入口。

## 2.5 创建 agent 钱包，拿到私钥

1. 打开 <https://app.hyperliquid.xyz/API>
2. 点 **Create API Wallet**（或 Generate agent key）
3. 界面会显示一串私钥 → **这就是 `HL_PRIVATE_KEY`**
4. 立刻复制保存，关掉窗口后**再也看不到**

> ⚠️ 填的是 **agent 的私钥**，不是你主钱包的私钥。主钱包私钥永远不要给任何程序。

## 2.6 顺手查一下你的真实费率（省钱，且防亏钱）

```bash
curl -s -X POST https://api.hyperliquid.xyz/info -H "Content-Type: application/json" \
  -d '{"type":"userFees","user":"0x你的地址"}' | python3 -m json.tool
```

你会看到类似：

```json
{
  "dailyUserVlm": [...],
  "feeSchedule": { "cross": "0.00045", "add": "0.00015", ... },
  "userCrossRate": "0.00045",
  "userAddRate": "0.00015"
}
```

**`userCrossRate` × 10000 = 你的实际 taker 费率（bps）。**

- `0.00045` → **4.5 bps**（config 里现在是这个值，不用改）
- `0.000405` → 4.05 bps（有 staking 折扣，改 config 更划算）
- 别的数 → **告诉我，我帮你改 config**

> 这个数字极其重要：引擎把它叠加到入场门槛上。
> **填少了不是「少赚」，是把净利为负的单子当成正收益放行。**

## 2.7 一键验证（推荐，别手打那两条长命令）

开完账户、转完钱之后，用这个脚本一次查完所有东西。

**在你自己 Mac 的终端里跑**（不用连服务器，纯只读、不下单、不需要私钥）：

```bash
bash /Users/ylh/WorkBuddy/2026-09-02-23-29-43/deploy/entropy-rh/check-hl.sh 0x你的HL地址
```

地址在哪看：登录 <https://app.hyperliquid.xyz> 后，右上角账户名旁边那串 `0x` 开头的就是。

它会告诉你三件事，并且**直接说哪里没准备好**：

```
[1] 主 perps 账户（普通合约用的钱）
    账户总值       : $300.00
    持仓名义       : $0.00

[2] io dex 清算所  ← Entropy 实际用的是这个
    账户总值       : $300.00
    持仓名义       : $0.00

    OK 已注资 $300.00

[3] taker 费率（全策略最致命的一个数）
    你的实际费率   : 4.50 bps
    config 填的值  : 4.5 bps

    OK 一致，config 不用改
==============================================
 第 2 步完成
==============================================
```

如果 io dex 是 0，它会直接告诉你去哪转账；
如果费率比 config 填的高，它会算出差值并让你改 `taker_fee_bps`。

> 手打 curl 命令也行（2.4、2.6 节里有），但脚本不会打错字。

## 2.8 验证成功 —— 你现在应该有这两样

```
HL_ACCOUNT_ADDRESS = 0x...   （主钱包地址，新开的那个）
HL_PRIVATE_KEY     = 0x...   （agent 的私钥，不是主钱包私钥）
```

加上确认：io dex 清算所 `accountValue > 0`，且已记下 `userCrossRate`。

**先不要填进 .env，等第 3 步一起填。**

---

# 第 3 步：开 Lighter Robinhood 链账户（对冲腿）

## 3.1 三个必须知道的坑

| 坑 | 说明 |
|---|---|
| **① 主网 ≠ RH 链** | 这是**两套完全独立的账户和密钥**。主网的 key 连 RH 链会报 `API key check failed`。你必须在 RH 链上重新注册一套。 |
| **② 计价是 USDG 不是 USDC** | RH 链用 Robinhood 自己的稳定币 **USDG**。你充进去的 USDC 会显示成 USDG，**这是正常的**，不是充错了。 |
| **③ API key index 要用 4~254** | Lighter 官方**保留 0~3** 给自家网页端，占用会让你在网页上被踢下线。 |

## 3.2 注册并充值

1. 打开 <https://robinhoodchain.lighter.xyz>（这是仓库 README 里给的入口）
2. 用**第 2 步那个新钱包**连接（保持两侧资金隔离一致）
3. 签名激活账户
4. 充值 USDC → 到账后显示为 **USDG**
   - 支持从 Arbitrum / Base / Avalanche 通过 CCTP 跨链转入，最低 5 USDC
   - 建议充 **$300**，与 HL 侧对称

## 3.3 创建子账户（不要直接用主账户）

在账户管理界面创建 **sub-account**，后续所有操作都用这个子账户。
理由：API key 能提现（虽然只能提到创建它的 L1 地址），用子账户把敞口隔离开。

## 3.4 生成 API key

1. **先选中刚建的子账户**（这步漏了会生成到主账户上）
2. 进 API Keys 页面 → **Generate API Key**
3. **index 选一个 4~254 之间的数**，比如 `4`
4. 界面显示私钥 → **立刻保存，关掉就再也看不到**（Lighter 不显示第二次）

## 3.5 拿到 account_index（这一步最容易蒙）

子账户的编号不是界面上显眼的数字，用这条命令查：

```bash
curl -sS --get "https://api.rh.lighter.xyz/api/v1/accountsByL1Address" \
     --data-urlencode "l1_address=0x你的钱包地址" | python3 -m json.tool
```

返回里会有 `sub_accounts` 列表，找到你刚建的子账户，记下它的 **index**。
（我实测过：这个接口从国内直连 200，延迟约 0.34 秒，可用性没问题。）

## 3.6 验证 key 真的注册成功了

```bash
curl -sS --get "https://api.rh.lighter.xyz/api/v1/apikeys" \
     --data-urlencode "account_index=上一步查到的index" \
     --data-urlencode "api_key_index=你选的4" | python3 -m json.tool
```

**返回里必须有 `"code": 200`。** 不是 200 就说明 key 没绑到这个子账户上，回去重做 3.4。

> 这两条命令都是纯只读，不会下单、不会动你的钱。放心跑。

## 3.7 顺手确认 SNDK 市场参数

```bash
curl -s -m 20 "https://api.rh.lighter.xyz/api/v1/orderBookDetails" \
  | python3 -c "
import json,sys
d=json.load(sys.stdin)
for m in d['order_book_details']:
    if m['symbol']=='SNDK':
        print('market_id  :', m['market_id'])
        print('taker_fee  :', m['taker_fee'])
        print('min_base   :', m['min_base_amount'])
        print('min_quote  :', m['min_quote_amount'])
        print('status     :', m['status'])
"
```

对照预期：`market_id = 32`、`taker_fee = 0.0000`、`min_base = 0.01`、`status = active`。

⚠️ `market_id` 如果变了（我上次测是 32），**告诉我**，config 和代码里要同步改。

## 3.8 验证成功 —— 现在应该有这三样

```
LIGHTER_ACCOUNT_INDEX     = 子账户编号（数字）
LIGHTER_API_KEY_INDEX     = 4（或你选的 4~254 之间的数）
LIGHTER_API_PRIVATE_KEY   = 生成的私钥
```

---

# 第 4 步：服务器（你在等的那件事）

## 4.1 服务器到手后，先给我这两个信息

```
IP 地址   : 
用户名    : 通常是 root（新机器第一次只能 root 登）
```

我会据此给你定制好的完整命令，你复制粘贴就行。

## 4.2 服务器选型建议（如果还没下单）

| 项 | 建议 | 理由 |
|---|---|---|
| 系统 | **Ubuntu 22.04 或 24.04 LTS** | 有 `/proc`，看门狗的进程检测走这条路最稳 |
| CPU | 2 核 | 1 核够跑，2 核留余量 |
| 内存 | 2 GB | asyncio + 盘口缓存，1G 偏紧 |
| 磁盘 | ≥ 20 GB | `logs/minutes.csv` 会一直涨 |
| 区域 | 东京 / 新加坡 / 硅谷 优先 | **两边 ws 延迟差 > 300ms 就换区域**，这个用 preflight 第 5 段实测 |

## 4.3 初始化（服务器就绪后跑）

先把本机公钥送上去（你已经有 `~/.ssh/entropy_arb.pub`）：

```bash
ssh-copy-id -i ~/.ssh/entropy_arb.pub root@<服务器IP>
```

然后**在服务器上**：

```bash
# 先干跑一次，看它会做什么（零副作用）
sudo bash bootstrap-server.sh --dry-run --user arbuser

# 确认没问题再真跑
sudo bash bootstrap-server.sh \
    --user arbuser \
    --repo https://github.com/你的用户名/entropy-arb.git
```

它做 7 件事：装系统包 → 建非 root 用户 → SSH 加固 → ufw 防火墙 →
建目录/时区/日志轮转 → 克隆仓库+建 venv+装依赖 → 环境自检。幂等，重复跑安全。

> ⚠️ 脚本内置防呆：禁用密码登录前会先检查 `authorized_keys` 里有没有公钥，
> 没有就**自动跳过 SSH 加固**——否则你会把自己永久锁在门外。

**验证**：**另开一个终端**用新用户登进去，确认能登再断当前连接。

```bash
ssh arbuser@<服务器IP>
```

---

# 第 5 步：上传密钥 + 预检

## 5.1 填 .env 并传上去

在本机把密钥填进模板（**不要提交到 git**）：

```bash
cd /Users/ylh/WorkBuddy/2026-09-02-23-29-43/deploy/entropy-rh
cp env.rh.template ~/entropy-rh.env
# 用编辑器填入 5 个值
open -e ~/entropy-rh.env
```

传上去：

```bash
scp ~/entropy-rh.env arbuser@<服务器IP>:~/entropy-arb/.env
```

在服务器上收紧权限：

```bash
ssh arbuser@<服务器IP> 'chmod 600 ~/entropy-arb/.env && ls -l ~/entropy-arb/.env'
# 必须看到 -rw-------
```

## 5.2 跑预检

```bash
ssh arbuser@<服务器IP>
cd ~/entropy-arb
source .venv/bin/activate
cp deploy/entropy-rh/config.rh.yaml config.yaml
python3 deploy/entropy-rh/preflight.py --symbol SNDK --config config.rh.yaml
```

预检跑 6 段：环境变量 → HL 连通/费率 → RH 连通 → **费率对账** → ws 延迟 → 溢价对照。

**重点看三处**：

1. **费率对账段**：会拿你账户的真实 `userCrossRate` 和 config 里的 `4.5` 比对，不一致会报警
2. **ws 延迟段**：HL 和 RH 的延迟**差距 > 300ms** 就考虑换服务器区域
3. 最后一行必须是 `→ 全部通过，可以进入 --record-only 采集阶段。`

---

# 第 6 步：只采集，不下单（24~72 小时，零风险）

```bash
cd ~/entropy-arb && source .venv/bin/activate
nohup python3 main.py --record-only --symbol SNDK --hedge lighter-rh \
      > logs/record.out 2>&1 &
echo $! > logs/record.pid
```

**这个模式不需要密钥、不下任何单**，只记录两个平台的盘口和溢价到 `logs/minutes.csv`。

⚠️ **必须跑满 24 小时以上，最好 72 小时**，要同时覆盖：
- 美股盘中（北京时间 21:30–04:00）—— 波动大
- 盘后时段 —— 波动小

只采几小时得出的阈值是废的。我本地实测过 3 分钟的数据，溢价在 -5.8 ~ -6.8 bps 之间晃，
但那是**某个时刻的快照，不是你的 midline**。

## 为什么这一步不能跳过

我先说清楚现在的处境，免得你有错觉：

| 项 | 数值 |
|---|---|
| 一次往返的成本 | **≈ 6.2 bps**（Entropy taker 4.5 + 价差摩擦 1.65） |
| 实测溢价波动幅度 | std 约 **0.5~1.0 bps** |

**成本比波动大一个数量级。** 这意味着调阈值调不出利润——
真正的答案只能从数据里来：要么溢价偶尔会有远超常态的尖峰（那就有肉），
要么这个组合在当前费率下根本不成立（那就换品种或降费率）。

采集数据就是回答这个问题的唯一办法。

---

# 第 7 步：算阈值

```bash
cd ~/entropy-arb && source .venv/bin/activate
python3 tools/analyze.py --fees-bps 4.5
```

把输出的 `midline_bps / upper_bps / lower_bps` 填回 `config.yaml`。

## ⚠️ 填之前必须满足的安全约束

```
midline + upper >= 1.0      且      lower - midline >= 1.0
```

**违反会稳定亏钱。** 原因是我读代码时发现的引擎特性：

- 卖 Entropy 方向的有效门槛 = `midline + upper`
- 买 Entropy 方向的有效门槛 = `lower - midline`
- 双腿是**同时成交、当场锁平**的，所以**每笔净利(bps) 就精确等于这个门槛**

推论：**门槛为负 = 每笔必亏。**

仓库原来的默认值 `midline=-3.7 / upper=2.0` 算出来是 **-1.7 bps**——
一旦触发就稳定送钱。我已经把 `upper_bps` 改成 `11.4`，两个方向门槛都是 **+7.7 bps**，
保证不亏。**别把它调回 2.0。**

---

# 第 8 步：挂看门狗 + 小仓实盘

## 8.1 先挂 guard（它和 bot 完全独立）

`guard.py` 跑在 bot 进程**之外**，只用交易所的**公开只读接口**（不需要任何私钥），
所以它「连单都下不了」，也因此不可能被 bot 的 bug 影响。我之前验证过零耦合。

```bash
# 人工核对一次，看输出对不对
python3 deploy/entropy-rh/guard.py --dump

# 挂 cron，每 2 分钟检查
crontab -e
# 加这一行：
*/2 * * * * cd ~/entropy-arb && .venv/bin/python3 deploy/entropy-rh/guard.py \
    --check --enforce --expect-running >> logs/guard.log 2>&1
```

红线（写在 `.env` 里）：

```bash
GUARD_MAX_NET_USD=150        # 净敞口上限 —— 最该盯的一项
GUARD_MAX_LEG_USD=800        # 单腿名义上限
GUARD_MAX_DAY_LOSS_USD=40    # 日亏红线
GUARD_MIN_EQUITY_USD=200     # 权益下限
```

> **`GUARD_MAX_NET_USD` 最有价值**——它直接抓「一腿成交、另一腿没跟上」这个最真实的亏损场景。
>
> 触发红线时 guard 会**停掉 bot、停止继续开仓，但不会自动平仓**。
> 自动平仓需要持有私钥，会摧毁它「连单都下不了」的安全优势。**平仓由你决定。**

## 8.2 小仓起步

```bash
python3 main.py --symbol SNDK --hedge lighter-rh
```

起步参数：`max_order_notional_usd: 50`（单笔 $50）、`max_position_usd: 600`。

跑稳一周，确认：
- 没有异常单边持仓
- guard 日志里没有 CRIT
- `logs/trades.csv` 里的实际成交价与你预期一致

再考虑提规模。

---

# 附录：常见报错对照表

| 报错 | 根因 | 解决 |
|---|---|---|
| `API key check failed` | 用了 Lighter **主网**的 key 连 RH 链 | 回 RH 链重新注册一套（第 3 步） |
| Lighter 拒单 / `order size too small` | 单笔名义 < `min_base 0.01` 枚 ≈ $15.4 | config 里 `min_order_notional_usd` 保持 20；币价大涨后要重算 |
| HL 显示有钱但下不了单 | 钱在主 perps，没转到 **io dex 清算所** | 第 2.4 步 + 验证命令 |
| 每笔都亏一点点 | 门槛为负（`midline+upper < 0`） | 第 7 步的安全约束 |
| 一直不触发 | 门槛设太高（现在 +7.7 bps 就是这样） | **正常现象**，等采集数据算真实阈值 |
| 网页端老被踢下线 | API key index 用了 0~3 | 换 4~254 |
| guard 报 CRIT: net exposure | 一腿成交另一腿没跟上 | 看 `logs/trades.csv`，人工平掉单边 |

---

# 一句话总结

> **今天：fork 代码（第 1 步）→ 开 HL 独立账户（第 2 步）→ 开 Lighter RH 子账户（第 3 步）。**
> **明天起：等服务器 IP 给我，剩下的我来给你定制命令。**
> **账户没搞定之前，服务器到了也只能干等着。**
