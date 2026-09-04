#!/usr/bin/env python3
# 一键体检：确认采集进程活着、CSV 在增长、看门狗正常。
import os, sys, time, paramiko

HOST, PORT, USER = "43.133.205.136", 22, "ubuntu"
PASS = os.environ.get("SSHPASS")
if not PASS:
    sys.exit("未设置 SSHPASS")

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, port=PORT, username=USER, password=PASS,
          timeout=20, look_for_keys=False, allow_agent=False)
sftp = c.open_sftp()

def get(remote, local):
    sftp.get(remote, local)

# 1) 进程是否活着（按 PID 文件）
_, o, e = c.exec_command(
    "P=$(cat /home/ubuntu/entropy-rh/logs/record.pid 2>/dev/null); "
    "if [ -n \"$P\" ] && kill -0 $P 2>/dev/null; then echo ALIVE $P; else echo DEAD; fi; "
    "W=$(cat /home/ubuntu/entropy-rh/logs/watch.pid 2>/dev/null); "
    "if [ -n \"$W\" ] && kill -0 $W 2>/dev/null; then echo WATCH_ALIVE $W; else echo WATCH_DEAD; fi; "
    "echo ROWS=$(wc -l < /home/ubuntu/entropy-rh/logs/minutes.csv 2>/dev/null || echo 0); "
    "echo NOW=$(date -u +%s); "
    "echo LASTWRITE=$(stat -c %Y /home/ubuntu/entropy-rh/logs/minutes.csv 2>/dev/null || echo 0)",
    timeout=30)
out = o.read().decode() + e.read().decode()
print(">> 进程状态:")
print(out.strip())

# 2) 拉回 CSV + 看门狗日志（看门狗只在重启时写 watch.log，健康时可能不存在）
def safe_get(remote, local):
    try:
        sftp.get(remote, local); return True
    except IOError:
        return False

safe_get("/home/ubuntu/entropy-rh/logs/minutes.csv", "/tmp/minutes-vps.csv")
safe_get("/home/ubuntu/entropy-rh/logs/watch.log", "/tmp/watch-vps.log")
safe_get("/home/ubuntu/entropy-rh/logs/record.log", "/tmp/record-vps.log")

# 3) 看门狗日志尾部
print(">> 看门狗日志 (watch.log) 尾部:")
wl = open("/tmp/watch-vps.log").read().strip()
print(wl[-800:] if wl else "(空 —— 进程一直健康，没重启过，正常)")

# 4) record.log 最新两行
print(">> record.log 最新两行:")
rl = open("/tmp/record-vps.log").read().strip().splitlines()
print("\n".join(rl[-2:]) if rl else "(空)")

sftp.close(); c.close()
print("\n>> 已拉回: /tmp/minutes-vps.csv /tmp/watch-vps.log /tmp/record-vps.log")
