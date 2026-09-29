#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端测试：对 mock Clash API 跑一轮真实采样，再出报告。

完全隔离：使用独立端口 + 独立数据库（_test/_tmp/），
不会触碰 data/traffic.db 里的真实采样数据，因此在采样器运行期间也可安全执行。

覆盖点：
  1. 控制接口读取（含环境变量覆盖）
  2. 差分采样与落库
  3. 中途关闭的连接是否被正确处理
  4. 归因覆盖率统计
  5. 报告渲染（终端 + HTML）

用法： python _test/test_e2e.py [采样秒数]
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
TMP = os.path.join(HERE, "_tmp")
PY = sys.executable
DURATION = int(sys.argv[1]) if len(sys.argv) > 1 else 34
INTERVAL = 3


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


PORT = free_port()
SECRET = "test-secret-not-a-real-credential"
TDB = os.path.join(TMP, "traffic.db")

shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP, exist_ok=True)

ENV = dict(os.environ,
           CLASH_MON_PORT=str(PORT),
           CLASH_MON_SECRET=SECRET,
           CLASH_MON_DB=TDB,
           CLASH_MON_REPORT=os.path.join(TMP, "report"),
           CLASH_MON_INTERVAL=str(INTERVAL))

print("=" * 74)
print("  端到端测试 | 端口 %d | 采样 %d 秒 | 间隔 %d 秒" % (PORT, DURATION, INTERVAL))
print("  隔离数据库：%s" % TDB)
print("=" * 74)

mock = subprocess.Popen([PY, os.path.join(HERE, "mock_clash.py")], env=ENV,
                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, encoding="utf-8", errors="replace")
time.sleep(2.0)

sampler = subprocess.Popen([PY, "monitor.py", "run"], cwd=ROOT, env=ENV,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           text=True, encoding="utf-8", errors="replace")
time.sleep(DURATION)
sampler.terminate()
try:
    s_out, _ = sampler.communicate(timeout=15)
except Exception:
    sampler.kill()
    s_out, _ = sampler.communicate()

print("\n--- 采样器输出 ---")
print((s_out or "").strip() or "(无输出)")


def run(args):
    return subprocess.run([PY, "monitor.py"] + args, cwd=ROOT, env=ENV,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                          text=True, encoding="utf-8", errors="replace").stdout


print("\n--- 状态检查 ---")
print(run(["status"]).strip())

print("\n--- 报告（近 1 小时）---")
print(run(["report", "--no-open"]).strip())

mock.terminate()
try:
    mock.communicate(timeout=10)
except Exception:
    mock.kill()

# ------------------------------------------------------------------ 断言
conn = sqlite3.connect(TDB)
n_ev = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
n_sp = conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
tot = conn.execute("SELECT COALESCE(SUM(up),0) FROM events").fetchone()[0]
uniq_host = conn.execute("SELECT COUNT(DISTINCT host) FROM events").fetchone()[0]
uniq_proc = conn.execute("SELECT COUNT(DISTINCT process) FROM events").fetchone()[0]
has_c4 = conn.execute(
    "SELECT COUNT(*) FROM events WHERE host='up.aliyuncs.com'").fetchone()[0]
n_up = conn.execute("SELECT COUNT(*) FROM events WHERE up>0").fetchone()[0]
conn.close()

html_dir = os.path.join(TMP, "report")
htmls = ([f for f in os.listdir(html_dir) if f.endswith(".html")]
         if os.path.isdir(html_dir) else [])
html_ok = False
if htmls:
    body = open(os.path.join(html_dir, htmls[0]), encoding="utf-8").read()
    html_ok = ("__" not in body.replace("__T", "")) and 'class="kpi"' in body \
        and "pan.quark.cn" in body

print("\n--- 断言 ---")
checks = [
    ("采样点数 > 5", n_sp > 5, n_sp),
    ("事件行数 > 20", n_ev > 20, n_ev),
    ("归因上传量 > 1 MB", tot > 1024 * 1024, "%.2f MB" % (tot / 1048576)),
    ("识别出 >=4 个域名", uniq_host >= 4, uniq_host),
    ("识别出 >=3 个进程", uniq_proc >= 3, uniq_proc),
    ("捕获到中途关闭的连接", has_c4 > 0, has_c4),
    ("有上传增量的事件", n_up > 0, n_up),
    ("HTML 报告渲染完整", html_ok, htmls[0] if htmls else "未生成"),
]
ok = True
for name, passed, val in checks:
    print("  [%s] %-24s -> %s" % ("PASS" if passed else "FAIL", name, val))
    ok = ok and passed

print("\n结果：%s" % ("全部通过" if ok else "存在失败项"))
sys.exit(0 if ok else 1)
