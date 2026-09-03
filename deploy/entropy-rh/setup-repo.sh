#!/usr/bin/env bash
# =============================================================================
# setup-repo.sh — 搭好 entropy-arb 的代码仓库结构（fork + 双分支 + pin commit）
# =============================================================================
#
# 为什么用 fork 而不是 copy 后改：
#   1. 上游会修接口适配问题（HL / zkLighter ws 协议会变），copy 之后拿不到
#   2. 自己的改动可以用 git diff vendor/upstream 一眼审计 —— 排查亏损时价值极大
#   3. 出问题能一键回滚到上一个验证过的 commit
#
# 搭出来的结构：
#   vendor/upstream    100% 同步上游，永不手动修改（用来 diff「上游改了什么」）
#   main               生产分支 = upstream + 你自己的 patch
#
# 用法：
#   # 方式 A：你已经在 GitHub 上 fork 了（推荐）
#   bash setup-repo.sh --fork-url git@github.com:YOURNAME/entropy-arb.git
#
#   # 方式 B：不想 fork，本地初始化，之后自己 push 到任意私有仓库
#   bash setup-repo.sh --no-fork
#
#   # 指定目录（默认 ./entropy-arb）
#   bash setup-repo.sh --fork-url ... --dir ~/entropy-arb
#
#   # 目标目录已存在且想重来
#   bash setup-repo.sh --fork-url ... --force
# =============================================================================

set -euo pipefail

# 已验证过的上游 commit —— 生产只跑这个，不跑分支最新代码
PIN_COMMIT="aa0391471f6bf72f78c45801fb8117b7bf7e8c89"
UPSTREAM_URL="https://github.com/your-quantguy/entropy-arb.git"

FORK_URL=""
NO_FORK=0
TARGET_DIR="./entropy-arb"
FORCE=0

# ------------------------------------------------------------------ 参数解析

usage() {
    sed -n '3,30p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --fork-url) FORK_URL="${2:?--fork-url 需要一个参数}"; shift 2 ;;
        --no-fork)  NO_FORK=1; shift ;;
        --dir)      TARGET_DIR="${2:?--dir 需要一个参数}"; shift 2 ;;
        --force)    FORCE=1; shift ;;
        -h|--help)  usage ;;
        *)          echo "未知参数: $1（用 --help 看用法）" >&2; exit 1 ;;
    esac
done

if [[ $NO_FORK -eq 0 && -z "$FORK_URL" ]]; then
    echo ""
    echo "需要指定 --fork-url，或者用 --no-fork。"
    echo ""
    echo "  A. 推荐做法：先到 https://github.com/your-quantguy/entropy-arb 点右上角 Fork，"
    echo "     然后："
    echo "       bash setup-repo.sh --fork-url git@github.com:YOURNAME/entropy-arb.git"
    echo ""
    echo "  B. 不想 fork（比如要放私有仓库）："
    echo "       bash setup-repo.sh --no-fork"
    echo ""
    exit 1
fi

# ------------------------------------------------------------------ 前置检查

command -v git >/dev/null 2>&1 || { echo "错误：没找到 git" >&2; exit 1; }

TARGET_DIR="${TARGET_DIR/#\~/$HOME}"

if [[ -e "$TARGET_DIR" ]]; then
    if [[ $FORCE -eq 1 ]]; then
        echo "== --force：移除已存在的 $TARGET_DIR"
        rm -rf "$TARGET_DIR"
    else
        echo "错误：$TARGET_DIR 已存在。" >&2
        echo "      加 --force 覆盖，或用 --dir 换个目录。" >&2
        exit 1
    fi
fi

echo "=============================================="
echo " entropy-arb 仓库初始化"
echo "=============================================="
echo " 目标目录   : $TARGET_DIR"
echo " 上游       : $UPSTREAM_URL"
echo " 固定 commit: $PIN_COMMIT"
if [[ $NO_FORK -eq 0 ]]; then
    echo " 你的 fork  : $FORK_URL"
else
    echo " 模式       : 本地初始化（--no-fork）"
fi
echo ""

# ------------------------------------------------------- 克隆 / 初始化仓库

if [[ $NO_FORK -eq 0 ]]; then
    echo "== [1/6] 克隆你的 fork …"
    if ! git clone "$FORK_URL" "$TARGET_DIR" 2>/dev/null; then
        echo ""
        echo "克隆失败。常见原因："
        echo "  - 用了 SSH 地址但本机没配 GitHub SSH key"
        echo "    → 改用 HTTPS：--fork-url https://github.com/YOURNAME/entropy-arb.git"
        echo "  - fork 还没创建"
        echo "    → 先到 https://github.com/your-quantguy/entropy-arb 点 Fork"
        exit 1
    fi
else
    echo "== [1/6] 从上游克隆（之后你可以自己 push 到任意私有仓库）…"
    git clone "$UPSTREAM_URL" "$TARGET_DIR"
fi

cd "$TARGET_DIR"

# ------------------------------------------------------------ 配置 remote

echo "== [2/6] 配置 upstream remote …"
if git remote get-url upstream >/dev/null 2>&1; then
    git remote set-url upstream "$UPSTREAM_URL"
else
    git remote add upstream "$UPSTREAM_URL"
fi
git remote -v | sed 's/^/     /'

echo ""
echo "== [3/6] 拉取上游全部历史 …"
git fetch upstream --tags
# 浅克隆（--depth）拿不到历史 commit，unshallow 一下
if [[ -f "$(git rev-parse --git-dir)/shallow" ]]; then
    echo "     检测到浅克隆，正在补全历史 …"
    git fetch upstream --unshallow --tags || true
fi

# pin 的 commit 必须在本地存在，否则后面建分支会裸报错
if ! git cat-file -e "${PIN_COMMIT}^{commit}" 2>/dev/null; then
    echo ""
    echo "错误：本地拿不到 commit $PIN_COMMIT" >&2
    echo "      〈可能是网络问题或上游已改写历史〉" >&2
    echo "      手动修复：cd $TARGET_DIR && git fetch upstream --unshallow" >&2
    exit 1
fi

# ------------------------------------------------- 建立 vendor/upstream 分支

echo "== [4/6] 建立 vendor/upstream 分支（100% 同步上游，永不手动修改）…"
if git show-ref --verify --quiet "refs/heads/vendor/upstream"; then
    git branch -f vendor/upstream "$PIN_COMMIT"
else
    git branch vendor/upstream "$PIN_COMMIT"
fi

# ------------------------------------------------------- 建立 main 生产分支

echo "== [5/6] 建立 main 生产分支（= upstream + 你自己的 patch）…"
if git show-ref --verify --quiet "refs/heads/main"; then
    echo "     main 已存在，保持不变（当前 HEAD: $(git rev-parse --short main)）"
else
    git branch main "$PIN_COMMIT"
fi
git checkout main 2>/dev/null || git checkout -b main "$PIN_COMMIT"

# ------------------------------------------------------------ 记录 pin 信息

echo "== [6/6] 写入 pin 信息 …"
mkdir -p deploy/entropy-rh
cat > deploy/entropy-rh/PINNED-COMMIT.txt <<EOF
# 生产环境固定的上游 commit —— 不要用分支最新代码跑实盘
pinned_commit = $PIN_COMMIT
pinned_date   = $(date -u '+%Y-%m-%d %H:%M:%S UTC')
upstream      = $UPSTREAM_URL

# 上游有更新时，按这个流程走（不要直接 git pull）：
#   git fetch upstream
#   git diff vendor/upstream..upstream/main            # 看上游改了什么
#   git branch -f vendor/upstream upstream/main        # 认可后再更新基准
#   git checkout main && git merge vendor/upstream     # 合进生产分支
#   重跑测试 + preflight.py，全绿后才上机
EOF

# 确认 .env 不会被提交
if [[ -f .env ]]; then
    chmod 600 .env
    echo "     已把现有 .env 权限收紧为 600"
fi
if ! grep -qxF ".env" .gitignore 2>/dev/null; then
    echo ".env" >> .gitignore
    echo "     已在 .gitignore 补上 .env"
fi

# ---------------------------------------------------------------- 完成输出

echo ""
echo "=============================================="
echo " 完成"
echo "=============================================="
echo ""
echo " 当前分支     : $(git rev-parse --abbrev-ref HEAD)"
echo " 当前 commit  : $(git rev-parse HEAD)"
echo " vendor/upstream: $(git rev-parse --short vendor/upstream)"
echo ""
echo " 分支约定："
echo "   vendor/upstream  — 只同步上游，你永远不手动改它"
echo "   main             — 生产分支，你自己的 patch 放这里"
echo ""
echo " 随时审计自己的改动："
echo "   git diff vendor/upstream                 # 我改了什么"
echo "   git diff vendor/upstream upstream/main   # 上游又改了什么"
echo ""

if [[ $NO_FORK -eq 1 ]]; then
    echo " 下一步：把代码推到你自己的私有仓库"
    echo "   git remote set-url origin <你的私有仓库地址>"
    echo "   git push -u origin main"
    echo "   git push -u origin vendor/upstream"
    echo ""
fi

echo " 下一步（在服务器上）："
echo "   cd $TARGET_DIR"
echo "   python3 -m venv .venv && source .venv/bin/activate"
echo "   pip install -r requirements.txt"
echo "   cp deploy/entropy-rh/config.rh.yaml config.yaml"
echo "   python3 deploy/entropy-rh/preflight.py --symbol SNDK --config config.rh.yaml"
echo ""
