#!/usr/bin/env python3
# 从 VPS 拉回 minutes.csv 到本地（密码登录，不依赖 sshpass）。
import os, sys, paramiko

HOST, PORT, USER = "43.133.205.136", 22, "ubuntu"
REMOTE = "/home/ubuntu/entropy-rh/logs/minutes.csv"
LOCAL = sys.argv[1] if len(sys.argv) > 1 else "/tmp/minutes-vps.csv"

PASS = os.environ.get("SSHPASS")
if not PASS:
    sys.exit("未设置 SSHPASS")

c = paramiko.SSHClient()
c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
c.connect(HOST, port=PORT, username=USER, password=PASS,
          timeout=20, look_for_keys=False, allow_agent=False)
sftp = c.open_sftp()
sftp.get(REMOTE, LOCAL)
sftp.close(); c.close()
print(f">> 已拉取: {LOCAL}")
