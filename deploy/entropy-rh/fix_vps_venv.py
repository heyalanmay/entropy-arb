#!/usr/bin/env python3
# 一次性修复：在 VPS 上装 python3.12-venv + 清掉残缺 .venv，再重跑部署前准备。
import os, sys, time, paramiko

HOST, PORT, USER = "43.133.205.136", 22, "ubuntu"
PASS = os.environ.get("SSHPASS")
if not PASS:
    sys.exit("未设置 SSHPASS")

def connect():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f">> 连接 {USER}@{HOST} ...")
    c.connect(HOST, port=PORT, username=USER, password=PASS,
              timeout=20, look_for_keys=False, allow_agent=False)
    print(">> 已登录")
    return c

def stream(client, cmd, pty=False, pwd=None, timeout=420):
    stdin, stdout, _ = client.exec_command(cmd, get_pty=pty, timeout=timeout)
    if pwd is not None and pty:
        stdin.write(pwd + "\n"); stdin.flush()
    start = time.time()
    while True:
        if stdout.channel.exit_status_ready():
            break
        try:
            r = stdout.channel.recv(4096)
        except Exception:
            break
        if not r:
            if stdout.channel.exit_status_ready():
                break
            time.sleep(0.1); continue
        sys.stdout.write(r.decode("utf-8", "replace")); sys.stdout.flush()
        if time.time() - start > timeout:
            print("\n!! 超时"); break
    return stdout.channel.recv_exit_status()

def capture(client, cmd):
    _, o, e = client.exec_command(cmd, timeout=60)
    out = o.read().decode("utf-8", "replace") + e.read().decode("utf-8", "replace")
    return o.channel.recv_exit_status(), out

c = connect()
try:
    # 探测 sudo 是否需要密码
    _, t = capture(c, "sudo -n true >/dev/null 2>&1 && echo NOPASSWD || echo NEEDPWD")
    nopass = "NOPASSWD" in t
    print(">> sudo 模式:", "无需密码" if nopass else "需要密码")

    print(">> 安装 python3.12-venv python3-pip ...")
    cmd = "sudo apt-get update -qq && sudo apt-get install -y python3.12-venv python3-pip"
    rc = stream(c, cmd, pty=not nopass, pwd=None if nopass else PASS, timeout=360)
    print(f">> apt 安装退出码: {rc}")

    print(">> 清掉残缺的 .venv（重跑部署会重建）...")
    stream(c, "rm -rf /home/ubuntu/entropy-rh/.venv", pty=False)

    print(">> 验证 ensurepip 是否可用 ...")
    _, v = capture(c, "python3 -c 'import ensurepip; print(\"ensurepip OK\")' 2>&1")
    print(v.strip())
    if "ensurepip OK" not in v:
        print("X ensurepip 仍不可用，需手动处理"); sys.exit(1)
    print(">> 修复完成，可以重跑 deploy_vps.py")
finally:
    c.close()
