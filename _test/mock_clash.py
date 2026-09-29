#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Mock Clash API —— 仅用于离线验证 monitor.py 的采样/差分/归因逻辑。

端口与 secret 均从 Clash 的 config.yaml 读取（与 monitor.py 同一来源），
因此仓库里不保存任何凭据；也可用环境变量 CLASH_MON_PORT / CLASH_MON_SECRET 覆盖。

Clash 未运行时该端口空闲，monitor.py 会直接连到这里，无需改任何配置。

模拟内容：
  * 一条长期上传大户（quark.exe → pan.quark.cn），用来验证「谁在上传」
  * 一条中途关闭的连接（c4，18 秒后消失），用来验证关闭连接的尾部丢失
  * 总量额外乘 1.05，用来验证归因覆盖率指标

用法： python _test/mock_clash.py
"""

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from monitor import read_clash_ctrl          # noqa: E402  复用同一套配置读取逻辑

_cfg_host, _cfg_port, _cfg_secret = read_clash_ctrl()
SECRET = os.environ.get("CLASH_MON_SECRET", _cfg_secret)
PORT = int(os.environ.get("CLASH_MON_PORT", _cfg_port or 50876))
T0 = None          # 从第一次 /connections 请求开始计时，方便反复测试

# (id, host, ip, port, process, processPath, chain, rule, up_Bps, down_Bps,
#  lifetime_s, emit_process)
#
# emit_process 刻意做成两种：
#   Clash for Windows 实测 metadata.process 为空、只有 processPath；
#   部分开源内核则直接给出 process。报告必须两者都能识别，
#   否则整列会显示「未识别」。
SPEC = [
    ("c1", "pan.quark.cn", "111.62.75.10", 443, "quark.exe",
     r"D:\Apps\Quark\quark.exe", "DIRECT", "DomainSuffix", 210_000, 12_000,
     None, False),
    ("c2", "ab.chatgpt.com", "104.18.32.20", 443, "ChatGPT.exe",
     r"C:\Users\me\AppData\Local\Programs\ChatGPT\ChatGPT.exe",
     "Proxies[HK]", "DomainSuffix", 42_000, 8_000, None, False),
    ("c3", "cdn.modelscope.cn", "47.98.1.5", 443, "chrome.exe",
     r"C:\Program Files\Google\Chrome\chrome.exe", "DIRECT", "GeoIP",
     6_000, 900_000, None, True),
    ("c4", "up.aliyuncs.com", "118.31.2.9", 443, "chrome.exe",
     r"C:\Program Files\Google\Chrome\chrome.exe", "DIRECT", "GeoIP",
     150_000, 3_000, 18, False),
    ("c5", "drive.quark.cn", "111.62.75.11", 443, "quark.exe",
     r"D:\Apps\Quark\quark.exe", "DIRECT", "DomainSuffix", 95_000, 20_000,
     None, False),
]


def snapshot():
    global T0
    if T0 is None:
        T0 = time.time()
    el = time.time() - T0
    conns, total_up, total_down = [], 0, 0
    for (cid, host, ip, port, proc, ppath, chain, rule, ur, dr, life,
         emit_proc) in SPEC:
        if life is not None and el > life:
            # 连接已关闭：其流量仍留在内核累计值里，但不再出现在 connections 中
            total_up += int(ur * life)
            total_down += int(dr * life)
            continue
        up, down = int(ur * el), int(dr * el)
        total_up += up
        total_down += down
        conns.append({
            "id": cid,
            "upload": up,
            "download": down,
            "start": time.strftime("%Y-%m-%dT%H:%M:%S+08:00", time.localtime(T0)),
            "metadata": {
                "network": "tcp", "type": "HTTP", "host": host,
                "destinationIP": ip, "destinationPort": str(port),
                "process": proc if emit_proc else "",
                "processPath": ppath,
                "rule": rule, "chains": [chain],
            },
        })
    return {
        "uploadTotal": int(total_up * 1.05),
        "downloadTotal": int(total_down * 1.02),
        "connections": conns,
        "memory": 0,
    }


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        if self.headers.get("Authorization", "") != "Bearer " + SECRET:
            body = b'{"message":"Unauthorized"}'
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.startswith("/version"):
            body = b'{"premium":true,"version":"2026.01.01-mock"}'
        elif self.path.startswith("/connections"):
            body = json.dumps(snapshot()).encode("utf-8")
        else:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    print("Mock Clash API 监听 127.0.0.1:%d" % PORT, flush=True)
    srv.serve_forever()
