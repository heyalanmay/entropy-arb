#!/usr/bin/env python3
# =============================================================================
# 沙箱/本地一键部署采集到 VPS（密码登录版，不依赖 sshpass）
#
# 用法：
#   SSHPASS='<你的 ubuntu 密码>' \
#   /Users/ylh/.workbuddy/binaries/python/envs/default/bin/python \
#       /Users/ylh/WorkBuddy/2026-09-02-23-29-43/deploy/entropy-rh/deploy_vps.py
#
# 做了什么：
#   1. paramiko 密码登录 VPS
#   2. 探测 python3-venv，缺则 sudo 安装（处理 NOPASSWD / 密码两种）
#   3. SFTP 上传最新 fork 压缩包 + 安装脚本
#   4. 远程执行 vps-setup-remote.sh 72h（实时回显，含连通/落盘自检）
#
# 密码只从环境变量 SSHPASS 读取，不写入任何文件、不打印、不进日志。
# =============================================================================
import os
import sys
import time
import paramiko

HOST = "43.133.205.136"
PORT = 22
USER = "ubuntu"
HOME = "/home/ubuntu"          # 远端 ubuntu 家目录（非登录 shell 下 $HOME 可能为空，显式给定）
DUR = "72h"
PIP_INDEX = "https://pypi.tuna.tsinghua.edu.cn/simple"   # 国内 VPS 装依赖走清华镜像
TGZ = "/tmp/entropy-rh.tgz"
SETUP = "/Users/ylh/WorkBuddy/2026-09-02-23-29-43/deploy/entropy-rh/vps-setup-remote.sh"

PASS = os.environ.get("SSHPASS")
if not PASS:
    sys.exit("错误：未设置 SSHPASS 环境变量（SSHPASS='密码' python deploy_vps.py）")


def connect():
    c = paramiko.SSHClient()
    c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f">> 连接 {USER}@{HOST}:{PORT} ...")
    c.connect(HOST, port=PORT, username=USER, password=PASS,
              timeout=20, look_for_keys=False, allow_agent=False)
    print(">> 已登录")
    return c


def run_capture(client, cmd, timeout=60):
    """执行并捕获输出，返回 (rc, text)。用于探测类短命令。"""
    _, o, e = client.exec_command(cmd, timeout=timeout)
    out = o.read().decode("utf-8", "replace")
    err = e.read().decode("utf-8", "replace")
    rc = o.channel.recv_exit_status()
    return rc, out + err


def run_stream(client, cmd, pty=False, pwd=None, timeout=420):
    """执行并实时回显，用于长命令（如安装脚本）。返回退出码。"""
    stdin, stdout, _ = client.exec_command(cmd, get_pty=pty, timeout=timeout)
    if pwd is not None and pty:
        stdin.write(pwd + "\n")
        stdin.flush()
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
            time.sleep(0.1)
            continue
        sys.stdout.write(r.decode("utf-8", "replace"))
        sys.stdout.flush()
        if time.time() - start > timeout:
            print(f"\n!! 读取超时 {timeout}s，停止")
            break
    try:
        while stdout.channel.recv_ready():
            r = stdout.channel.recv(4096)
            if not r:
                break
            sys.stdout.write(r.decode("utf-8", "replace"))
    except Exception:
        pass
    return stdout.channel.recv_exit_status()


def ensure_venv(client):
    # 注意：`python3 -m venv --help` 即使缺 ensurepip 也会返回 0（假阳性）。
    # 正确探测：ensurepip 模块可导入，才说明 venv 能自举 pip。
    print(">> 探测 python3-venv / ensurepip ...")
    rc, txt = run_capture(client, "python3 -c 'import ensurepip' 2>/dev/null && echo HAVE_VENV || echo NO_VENV")
    if "HAVE_VENV" in txt:
        print(">> python3-venv 已就绪")
        return
    print(">> 缺少 python3-venv，尝试安装 ...")
    _, txt2 = run_capture(client, "sudo -n true >/dev/null 2>&1 && echo NOPASSWD || echo NEEDPWD")
    nopass = "NOPASSWD" in txt2
    # 用具体版本包名（Ubuntu 24.04 是 python3.12-venv），metapackage 兜底
    cmd = "sudo apt-get update -qq && sudo apt-get install -y python3.12-venv python3.venv python3-pip"
    if nopass:
        print("   （sudo 无需密码）")
        run_stream(client, cmd, pty=False, timeout=300)
    else:
        print("   （sudo 需密码，已通过 pty 传入）")
        run_stream(client, cmd, pty=True, pwd=PASS, timeout=300)
    rc, txt3 = run_capture(client, "python3 -c 'import ensurepip' 2>/dev/null && echo HAVE_VENV || echo STILL_MISSING")
    if "STILL_MISSING" in txt3:
        print("X 安装失败。请手动登录 VPS 执行：")
        print("   sudo apt-get install -y python3.12-venv python3-pip")
        sys.exit(1)
    print(">> python3-venv 安装完成")


def main():
    client = connect()
    try:
        ensure_venv(client)

        print(">> SFTP 上传代码 + 安装脚本 ...")
        sftp = client.open_sftp()
        sftp.put(TGZ, "/tmp/entropy-rh.tgz")
        sftp.put(SETUP, "/tmp/vps-setup-remote.sh")
        sftp.close()
        print(">> 上传完成")

        print(">> 远程安装（约 2~3 分钟，含连通/落盘自检）...")
        remote_cmd = (
            f"PIP_INDEX={PIP_INDEX} HOME={HOME} "
            f"bash /tmp/vps-setup-remote.sh {DUR}"
        )
        rc = run_stream(client, remote_cmd, pty=False, timeout=420)
        print(f"\n>> 远程脚本退出码: {rc}")

        if rc == 0:
            print("==============================================")
            print(" 部署命令已正常退出。看门狗 + 采集已在后台运行。")
            print(" 取回数据（在你 Mac 终端）：")
            print("   scp ubuntu@43.133.205.136:~/entropy-rh/logs/minutes.csv ./minutes-vps.csv")
            print(" 查看进度：")
            print("   ssh ubuntu@43.133.205.136 'tail -f ~/entropy-rh/logs/record.log'")
            print("==============================================")
        else:
            print(f"X 部署脚本返回非零（{rc}）。上面应有报错，按需处理或把输出贴回给我。")
    finally:
        client.close()


if __name__ == "__main__":
    main()
