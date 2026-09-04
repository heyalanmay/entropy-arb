#!/usr/bin/env python3
# =============================================================================
# 一条命令：从 VPS 拉回 minutes.csv + 看门狗/进程健康检查，再跑 analyze-peaks。
#
# 用法：
#   SSHPASS='<VPS ubuntu 密码>' \
#   /Users/ylh/.workbuddy/binaries/python/envs/default/bin/python \
#       /Users/ylh/WorkBuddy/2026-09-02-23-29-43/deploy/entropy-rh/vps_analyze.py [--notional 30]
#
# 输出：① 进程/看门狗健康 ② CSV 行数+最后写入时间 ③ analyze-peaks 完整报告
# 数据不足 1 小时时，analyze 会自行标注"仅供方向参考"，不会编造结论。
# =============================================================================
import os, sys, subprocess, paramiko

HOST, PORT, USER = "43.133.205.136", 22, "ubuntu"
PASS = os.environ.get("SSHPASS")
if not PASS:
    sys.exit("未设置 SSHPASS 环境变量")

HERE = "/Users/ylh/WorkBuddy/2026-09-02-23-29-43/deploy/entropy-rh"
CSV_REMOTE = "/home/ubuntu/entropy-rh/logs/minutes.csv"
CSV_LOCAL = "/tmp/minutes-vps.csv"
NOTIONAL = "30"
for i, a in enumerate(sys.argv[1:]):
    if a == "--notional" and i + 1 < len(sys.argv[1:]):
        NOTIONAL = sys.argv[1:][i + 1]

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, port=PORT, username=USER, password=PASS,
          timeout=20, look_for_keys=False, allow_agent=False)
sftp = c.open_sftp()

# ---- 健康检查 ----
_, o, e = c.exec_command(
    "P=$(cat /home/ubuntu/entropy-rh/logs/record.pid 2>/dev/null); "
    "if [ -n \"$P\" ] && kill -0 $P 2>/dev/null; then echo ALIVE $P; else echo REC_DEAD; fi; "
    "W=$(cat /home/ubuntu/entropy-rh/logs/watch.pid 2>/dev/null); "
    "if [ -n \"$W\" ] && kill -0 $W 2>/dev/null; then echo WATCH_ALIVE $W; else echo WATCH_DEAD; fi; "
    "echo ROWS=$(wc -l < /home/ubuntu/entropy-rh/logs/minutes.csv 2>/dev/null || echo 0); "
    "echo NOW=$(date -u +%s); "
    "echo LASTWRITE=$(stat -c %Y /home/ubuntu/entropy-rh/logs/minutes.csv 2>/dev/null || echo 0)",
    timeout=30)
st = o.read().decode() + e.read().decode()
print("==================== 采集机健康 ====================")
for line in st.strip().splitlines():
    if line.startswith("LASTWRITE="):
        age = int(dict(x.split("=", 1) for x in st.strip().split() if "=" in x)["NOW"]) - int(line.split("=", 1)[1])
        print(f"  最后写入: {age} 秒前" + ("" if age < 120 else "  ⚠️ 超过 2 分钟，可能卡死"))
    else:
        print("  " + line)
sys.stdout.flush()

# ---- 拉 CSV ----
try:
    sftp.get(CSV_REMOTE, CSV_LOCAL)
    print(f"  >> 已拉回 {CSV_LOCAL}")
except IOError:
    print("X 拉取 CSV 失败（文件不存在？采集未启动）")
    sftp.close(); c.close(); sys.exit(1)
sftp.close(); c.close()

# ---- 跑分析 ----
print("==================== 数据分析 ====================")
rc = subprocess.run([
    "/Users/ylh/WorkBuddy/2026-09-02-23-29-43/entropy-arb/.venv/bin/python",
    f"{HERE}/analyze-peaks.py", CSV_LOCAL, "--notional", NOTIONAL,
])
sys.exit(rc.returncode)
