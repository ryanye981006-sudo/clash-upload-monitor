#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""针对「Clash 重启换控制端口」的健壮性测试。

这是最容易导致采样器静默失效的真实场景：Clash for Windows 每次启动都会
分配一个随机 external-controller 端口，采样器若死守旧端口就会一路失败直至退出。

测试流程：
  1. 用临时 config.yaml 指向端口 A，启动 mock + 采样器，确认正常采样
  2. 杀掉 mock（并确认端口 A 真的释放），改 config.yaml 指向端口 B，在 B 起新 mock
  3. 采样器应自动重新发现端口并继续采样（而不是退出）

用法： python _test/test_port_change.py
"""

import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TMP = os.path.join(HERE, "_tmp_portchange")
PY = sys.executable
SECRET = "test-secret-not-a-real-credential"
INTERVAL = 3


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def port_open(port):
    s = socket.socket()
    s.settimeout(0.4)
    try:
        s.connect(("127.0.0.1", port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def write_config(path, port):
    with open(path, "w", encoding="utf-8") as f:
        f.write("mixed-port: 7890\n"
                "external-controller: 127.0.0.1:%d\n"
                "secret: %s\n" % (port, SECRET))


def start_mock(port):
    env = dict(ENV, CLASH_MON_PORT=str(port))
    return subprocess.Popen([PY, os.path.join(HERE, "mock_clash.py")], env=env,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, encoding="utf-8", errors="replace")


def kill_mock(p, port):
    """杀掉 mock，并循环确认端口已真正释放。"""
    if p and p.poll() is None:
        p.kill()
        try:
            p.communicate(timeout=10)
        except Exception:
            pass
    for _ in range(30):
        if not port_open(port):
            return True
        time.sleep(0.2)
    return False


shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP, exist_ok=True)
CFG = os.path.join(TMP, "config.yaml")
TDB = os.path.join(TMP, "traffic.db")

PORT_A, PORT_B = free_port(), free_port()
write_config(CFG, PORT_A)

ENV = dict(os.environ,
           CLASH_MON_CONFIG=CFG,
           CLASH_MON_SECRET=SECRET,
           CLASH_MON_DB=TDB,
           CLASH_MON_REPORT=os.path.join(TMP, "report"),
           CLASH_MON_INTERVAL=str(INTERVAL))
# 注意：不设置 CLASH_MON_PORT，让采样器只能靠 config.yaml 发现端口，
#       这样才能真正验证重连逻辑。

print("=" * 74)
print("  端口漂移测试 | 端口 A=%d → B=%d" % (PORT_A, PORT_B))
print("=" * 74)

mock = start_mock(PORT_A)
time.sleep(2.0)
print("  mock A 监听端口 A：%s" % port_open(PORT_A))

sampler = subprocess.Popen([PY, "monitor.py", "run"], cwd=ROOT, env=ENV,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, encoding="utf-8", errors="replace")

print("\n阶段 1：在端口 A 正常采样 12 秒 …")
time.sleep(12)
n_before = 0
if os.path.exists(TDB):
    c = sqlite3.connect(TDB)
    n_before = c.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    c.close()
print("  已采样 %d 个点" % n_before)

print("\n阶段 2：模拟 Clash 重启 …")
print("  杀掉 mock A 并在端口 A 释放后，再启动 mock B")
kill_mock(mock, PORT_A)
print("  端口 A 已释放：%s" % (not port_open(PORT_A)))
write_config(CFG, PORT_B)
mock = start_mock(PORT_B)
time.sleep(2.0)
print("  mock B 监听端口 B：%s" % port_open(PORT_B))
switched_at = time.time()

print("  等待 30 秒，观察采样器能否自愈 …")
time.sleep(30)

alive = sampler.poll() is None
if alive:
    sampler.terminate()
try:
    out, _ = sampler.communicate(timeout=15)
except Exception:
    sampler.kill()
    out, _ = sampler.communicate()
kill_mock(mock, PORT_B)

print("\n--- 采样器输出 ---")
print((out or "").strip() or "(无输出)")

c = sqlite3.connect(TDB)
n_after = c.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
n_post = c.execute("SELECT COUNT(*) FROM samples WHERE ts>=?",
                   (switched_at,)).fetchone()[0]
c.close()

print("\n--- 断言 ---")
checks = [
    ("采样器未退出（成功自愈）", alive, "存活" if alive else "已退出"),
    ("切换前已采样", n_before >= 2, n_before),
    ("切换后仍在采样", n_post >= 3, n_post),
    ("日志出现重连提示", "已重连" in (out or ""),
     "已重连" in (out or "")),
    ("检测到旧端口失败", ("读取失败" in (out or "")) or ("已重连" in (out or "")),
     "有失败/重连记录"),
    ("总采样点增长", n_after > n_before, "%d → %d" % (n_before, n_after)),
]
ok = True
for name, passed, val in checks:
    print("  [%s] %-22s -> %s" % ("PASS" if passed else "FAIL", name, val))
    ok = ok and passed
print("\n结果：%s" % ("全部通过" if ok else "存在失败项"))
sys.exit(0 if ok else 1)
