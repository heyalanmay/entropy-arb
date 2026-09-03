#!/usr/bin/env bash
# =============================================================================
# 新服务器一键初始化 —— 在【新服务器】上以 root 或 sudo 用户执行
#
#   sudo bash bootstrap-server.sh --user arbuser --repo git@github.com:YOU/entropy-arb.git
#   sudo bash bootstrap-server.sh --dry-run          # 只看会做什么，不改任何东西
#
# 做七件事：
#   1. 系统更新 + 装基础包
#   2. 建非 root 运行用户（缩小密钥泄露时的爆炸半径）
#   3. SSH 加固：禁密码、禁 root 登录  ← 有强制前置检查，防把自己锁门外
#   4. ufw 防火墙：只开 SSH
#   5. 建目录 / 时区 / 日志轮转
#   6. 拉仓库 + 建 venv + 装依赖
#   7. 输出后续步骤
#
# 幂等：重复执行安全。已做过的步骤会跳过。
# =============================================================================
set -euo pipefail

# ------------------------------------------------------------------- 参数
RUN_USER=""
REPO_URL=""
DRY_RUN=0
SKIP_HARDEN=0
FORK_PIN="aa03914"          # 验证过的上游 commit

while [[ $# -gt 0 ]]; do
    case "$1" in
        --user)        RUN_USER="${2:?--user 需要一个值}"; shift 2 ;;
        --repo)        REPO_URL="${2:?--repo 需要一个值}"; shift 2 ;;
        --dry-run)     DRY_RUN=1; shift ;;
        --skip-harden) SKIP_HARDEN=1; shift ;;
        -h|--help)     sed -n '2,20p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "未知参数: $1（用 --help 看用法）"; exit 1 ;;
    esac
done

# ------------------------------------------------------------------- 颜色与打印
if [[ -t 1 ]]; then
    C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YEL=$'\033[33m'
    C_BLU=$'\033[34m'; C_RST=$'\033[0m'
else
    C_RED=""; C_GRN=""; C_YEL=""; C_BLU=""; C_RST=""
fi

hr()  { printf '%s\n' "${C_BLU}──────────────────────────────────────────────────────${C_RST}"; }
step(){ printf '%s\n' "${C_BLU}== $*${C_RST}"; }
ok()  { printf '  %s[完成]%s %s\n' "$C_GRN" "$C_RST" "$*"; }
skip(){ printf '  %s[跳过]%s %s\n' "$C_YEL" "$C_RST" "$*"; }
warn(){ printf '  %s[注意]%s %s\n' "$C_YEL" "$C_RST" "$*"; }
die() { printf '%s[错误]%s %s\n' "$C_RED" "$C_RST" "$*" >&2; exit 1; }
act() {   # act "描述" "命令..."  —— dry-run 时只打印
    local desc="$1"; shift
    if [[ $DRY_RUN -eq 1 ]]; then
        printf '  %s[将执行]%s %s\n      %s\n' "$C_YEL" "$C_RST" "$desc" "$*"
    else
        printf '  %s[执行]%s %s\n' "$C_GRN" "$C_RST" "$desc"
        "$@" >/dev/null 2>&1 || warn "命令返回非零，请人工确认：$*"
    fi
}

# 以目标用户身份运行。只有「当前是 root 且目标不是 root」时才 sudo，
# 否则直接执行 —— 否则以目标用户自己运行时 sudo 会卡在密码输入。
as_user() {
    if [[ $EUID -eq 0 && -n "${RUN_USER:-}" && "${RUN_USER}" != "root" ]]; then
        sudo -u "$RUN_USER" "$@"
    else
        "$@"
    fi
}

# ------------------------------------------------------------------- 环境检查
[[ $EUID -eq 0 ]] || command -v sudo >/dev/null \
    || die "需要 root 或 sudo 权限运行"

if [[ -f /etc/os-release ]]; then
    . /etc/os-release
    OS_NAME="${NAME:-unknown} ${VERSION_ID:-}"
else
    OS_NAME="unknown"
fi

hr
printf '%s新服务器初始化%s  %s\n' "$C_BLU" "$C_RST" "$(date '+%F %T')"
printf '  系统: %s\n' "$OS_NAME"
printf '  用户: %s\n' "${RUN_USER:-（未指定，跳过建用户）}"
printf '  仓库: %s\n' "${REPO_URL:-（未指定，跳过拉仓库）}"
[[ $DRY_RUN -eq 1 ]] && printf '  %s模式: DRY-RUN（不会改动任何东西）%s\n' "$C_YEL" "$C_RST"
hr

# =================================================================== 1. 系统包
step "[1/7] 系统更新与基础包"
if [[ -r /var/lib/apt/lists/lock ]]; then
    act "更新包索引"              apt-get update -qq
    act "升级已装包"              env DEBIAN_FRONTEND=noninteractive apt-get upgrade -y -qq
    act "安装 python3/venv/git 等" env DEBIAN_FRONTEND=noninteractive \
        apt-get install -y -qq python3 python3-venv python3-pip git curl \
        ufw jq htop build-essential
    ok "系统包就绪"
else
    skip "非 Debian 系（$OS_NAME），跳过包管理，请自行确认 python3/venv/git/ufw 已装"
fi

# =================================================================== 2. 运行用户
step "[2/7] 运行用户"
if [[ -z "$RUN_USER" ]]; then
    skip "未指定 --user，沿用当前用户：$(id -un)"
    RUN_USER="$(id -un)"
elif id "$RUN_USER" >/dev/null 2>&1; then
    ok "用户 $RUN_USER 已存在"
else
    act "创建用户 $RUN_USER"      adduser --disabled-password --gecos "" "$RUN_USER"
    ok "已创建 $RUN_USER（无密码，只能 SSH key 登录）"
fi

# getent 在个别精简镜像里可能没有，用 python3 兜底
HOME_DIR=""
if command -v getent >/dev/null 2>&1; then
    HOME_DIR="$(getent passwd "$RUN_USER" 2>/dev/null | cut -d: -f6)"
elif command -v python3 >/dev/null 2>&1; then
    HOME_DIR="$(python3 -c "import pwd;print(pwd.getpwnam('$RUN_USER').pw_dir)" 2>/dev/null || true)"
fi
[[ -n "$HOME_DIR" && -d "$HOME_DIR" ]] || HOME_DIR="/home/$RUN_USER"
printf '      家目录: %s\n' "$HOME_DIR"

APP_DIR="$HOME_DIR/entropy-arb"

# ---- SSH key 检查（在禁用密码登录之前，必须先确认 key 已就位）--------------
step "[3/7] SSH 加固"
AUTH_KEYS="$HOME_DIR/.ssh/authorized_keys"
HAS_KEY=0
[[ -s "$AUTH_KEYS" ]] && HAS_KEY=1

if [[ $HAS_KEY -eq 0 ]]; then
    warn "在 $AUTH_KEYS 里没找到 SSH 公钥"
    warn "此时禁用密码登录 = 把自己锁在服务器外面，无法恢复"
    warn "→ 跳过 SSH 加固。请先在本机执行：ssh-copy-id $RUN_USER@$(hostname -I 2>/dev/null | awk '{print $1}')"
    warn "→ 然后重跑本脚本"
    SKIP_HARDEN=1
elif [[ $SKIP_HARDEN -eq 1 ]]; then
    skip "已指定 --skip-harden"
else
    if [[ ! -f /etc/ssh/sshd_config ]]; then
        skip "没找到 /etc/ssh/sshd_config"
    elif [[ $DRY_RUN -eq 1 ]]; then
        printf '  %s[将修改]%s /etc/ssh/sshd_config（先备份，再禁密码+禁 root）\n' "$C_YEL" "$C_RST"
    else
        cp /etc/ssh/sshd_config "/etc/ssh/sshd_config.bak.$(date +%Y%m%d_%H%M%S)"
        # 幂等：直接改，不改就注释掉原行再追加
        act "禁用密码登录" sed -i \
            's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
        act "禁用 root 登录" sed -i \
            's/^#\?PermitRootLogin.*/PermitRootLogin no/' /etc/ssh/sshd_config
        grep -qE '^PasswordAuthentication no' /etc/ssh/sshd_config \
            || echo 'PasswordAuthentication no' >> /etc/ssh/sshd_config
        grep -qE '^PermitRootLogin no' /etc/ssh/sshd_config \
            || echo 'PermitRootLogin no' >> /etc/ssh/sshd_config
        # sshd 服务名在 Debian 系是 ssh、RHEL 系是 sshd，用 bash -c 包住 ||
        act "校验 sshd 配置"      bash -c 'sshd -t || /usr/sbin/sshd -t'
        act "重启 sshd"           bash -c 'systemctl restart ssh 2>/dev/null || systemctl restart sshd 2>/dev/null || service ssh restart'
        ok "SSH 加固：密码登录已禁、root 登录已禁（配置已备份）"
    fi
fi

# =================================================================== 4. 防火墙
step "[4/7] 防火墙"
if command -v ufw >/dev/null; then
    act "默认拒绝所有入站"        ufw default deny incoming
    act "放行 SSH"               ufw allow OpenSSH
    act "启用 ufw"               bash -c 'echo y | ufw enable'
    ok "ufw 已启用（只开 SSH）"
    warn "⚠️ 确认 SSH 端口已放行后再断开当前连接；建议另开一个终端验证能重连"
else
    skip "ufw 不可用"
fi

# =================================================================== 5. 目录
step "[5/7] 目录与基础设置"
act "设置时区 UTC"               timedatectl set-timezone UTC
for d in "$APP_DIR" "$APP_DIR/logs" "$APP_DIR/deploy"; do
    act "建目录 $d"              mkdir -p "$d"
done
act "归属给 $RUN_USER"           chown -R "$RUN_USER":"$RUN_USER" "$HOME_DIR/entropy-arb"

# 日志轮转：minutes.csv 长期跑会涨
if [[ ! -d /etc/logrotate.d ]]; then
    skip "/etc/logrotate.d 不存在"
elif [[ $DRY_RUN -eq 1 ]]; then
    printf '  %s[将写入]%s /etc/logrotate.d/entropy-arb（保留 14 天）\n' "$C_YEL" "$C_RST"
else
    cat > /etc/logrotate.d/entropy-arb <<EOF
$APP_DIR/logs/*.log {
    daily
    rotate 14
    compress
    missingok
    notifempty
    copytruncate
    su $RUN_USER $RUN_USER
}
EOF
    ok "已配置日志轮转（logs/*.log，保留 14 天）"
fi

# =================================================================== 6. 仓库
step "[6/7] 代码仓库"
if [[ -z "$REPO_URL" ]]; then
    skip "未指定 --repo，跳过。之后手动：git clone <地址> $APP_DIR"
elif [[ -d "$APP_DIR/.git" ]]; then
    ok "仓库已存在于 $APP_DIR，跳过克隆"
else
    if [[ $DRY_RUN -eq 1 ]]; then
        printf '  %s[将执行]%s 克隆仓库 → %s\n' "$C_YEL" "$C_RST" "$APP_DIR"
    elif as_user git clone "$REPO_URL" "$APP_DIR" >/dev/null 2>&1; then
        ok "已克隆到 $APP_DIR"
    else
        die "克隆失败：$REPO_URL
      常见原因：① fork 地址写错 ② 服务器没配 GitHub SSH key（私有仓库）
      排查：ssh -T git@github.com   或改用 https 地址"
    fi
fi

if [[ ! -d "$APP_DIR/.git" ]]; then
    skip "仓库不存在，跳过 venv 与依赖"
elif [[ ! -f "$APP_DIR/requirements.txt" ]]; then
    warn "$APP_DIR 里没有 requirements.txt —— 确认克隆的是正确的仓库"
else
    if [[ $DRY_RUN -eq 1 ]]; then
        printf '  %s[将执行]%s 创建 venv 并安装 requirements.txt\n' "$C_YEL" "$C_RST"
    else
        as_user python3 -m venv "$APP_DIR/.venv" \
            || die "venv 创建失败。Debian/Ubuntu 先装：apt-get install -y python3-venv"
        ok "venv 已建"

        as_user "$APP_DIR/.venv/bin/pip" install -q --upgrade pip \
            || warn "pip 升级失败（不致命，继续）"
        if as_user "$APP_DIR/.venv/bin/pip" install -q -r "$APP_DIR/requirements.txt"; then
            ok "依赖安装完成"
        else
            die "依赖安装失败。看完整报错：
      sudo -u $RUN_USER $APP_DIR/.venv/bin/pip install -r $APP_DIR/requirements.txt"
        fi

        # 固定到验证过的 commit
        CURRENT="$(cd "$APP_DIR" && git rev-parse --short HEAD 2>/dev/null || echo '?')"
        if [[ "$CURRENT" == "${FORK_PIN:0:7}" ]]; then
            ok "已固定在验证过的 commit: $CURRENT"
        else
            warn "当前 HEAD=$CURRENT，与验证过的 $FORK_PIN 不同"
            warn "确认过 diff 后再上实盘；要固定就跑："
            warn "    cd $APP_DIR && git checkout $FORK_PIN"
        fi
    fi
fi

# =================================================================== 7. 收尾
step "[7/7] 环境自检"
if [[ -x "$APP_DIR/.venv/bin/python" ]]; then
    PYV="$("$APP_DIR/.venv/bin/python" --version 2>&1)"
    ok "Python：$PYV"
fi
if [[ -f /proc/1/cmdline ]]; then
    ok "/proc 可用 → guard.py 的进程检测走最稳的那条路"
fi
command -v crontab >/dev/null && ok "crontab 可用 → guard 可挂定时任务" \
                              || warn "无 crontab，guard 只能用 --watch 常驻模式"

hr
printf '%s初始化完成。%s 接下来：\n\n' "$C_GRN" "$C_RST"
cat <<EOF
  1) 把本地的部署包传上来（在你【本机】执行）：
       scp -r deploy/entropy-rh $RUN_USER@<服务器IP>:~/entropy-arb/deploy/

  2) 登上去装配置（采集阶段不需要密钥）：
       ssh $RUN_USER@<服务器IP>
       cd ~/entropy-arb && source .venv/bin/activate
       bash deploy/entropy-rh/install.sh

  3) 跑预检（重点看延迟 + 手续费对账）：
       python3 deploy/entropy-rh/preflight.py --symbol SNDK

  4) 采集 24~72 小时，不下单：
       bash deploy/entropy-rh/run.sh record

  5) 出阈值 → 挂 guard → 才开实盘。详见 DEPLOY.md / PREP.md
EOF
hr

if [[ $DRY_RUN -eq 1 ]]; then
    printf '%s这是 DRY-RUN，以上改动均未真正执行。%s\n' "$C_YEL" "$C_RST"
fi
