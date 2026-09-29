#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Clash 上传流量采样器 + 归因分析
=================================================

背景
----
Clash 自身不保留历史流量明细：
  * /connections 只存「活跃」连接，连接一关闭明细立即丢失；
  * uploadTotal / downloadTotal 只有内核启动以来的累计值，重启清零；
  * 内核日志 (Data/logs/*.log) 只有目标/规则/出口，不含字节数。

结果就是：你能看到当前累计上传了多少，但无法事后回答「刚才是谁传的」。

本工具的做法
------------
定时轮询 /connections，对每条连接的 upload 字段做差分，
把增量按「连接 / 进程 / 域名 / 出口节点」落库到 SQLite。
跑一段时间后即可还原出任意时段的上传分布。

归因覆盖率 = 各连接增量之和 / uploadTotal 增量，用于衡量采样是否漏量。

用法
----
  python monitor.py menu      交互菜单（桌面启动器调用）
  python monitor.py run       前台采样，Ctrl+C 停止
  python monitor.py start     后台采样（无窗口）
  python monitor.py stop      停止后台采样
  python monitor.py status    查看运行状态
  python monitor.py report    生成上传分布报告
"""

import json
import os
import re
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

# --------------------------------------------------------------------------
# 基础路径
# --------------------------------------------------------------------------

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("CLASH_MON_DATA", os.path.join(BASE_DIR, "data"))
REPORT_DIR = os.environ.get("CLASH_MON_REPORT", os.path.join(BASE_DIR, "report"))
DB_PATH = os.environ.get("CLASH_MON_DB", os.path.join(DATA_DIR, "traffic.db"))
PID_PATH = os.path.join(DATA_DIR, "monitor.pid")
LOG_PATH = os.path.join(DATA_DIR, "monitor.log")

for _d in (DATA_DIR, REPORT_DIR):
    os.makedirs(_d, exist_ok=True)

SAMPLE_INTERVAL = int(os.environ.get("CLASH_MON_INTERVAL", "15"))   # 采样间隔（秒）
RETAIN_DAYS = int(os.environ.get("CLASH_MON_RETAIN_DAYS", "14"))    # 数据保留天数


def find_clash_config():
    """定位 Clash / mihomo 的 config.yaml。

    找不到时返回默认候选路径（后续会给出明确报错，提示用 CLASH_MON_CONFIG 指定）。
    """
    env = os.environ.get("CLASH_MON_CONFIG")
    home = os.path.expanduser("~")
    cands = []
    if env:
        cands.append(env)
    cands += [
        os.path.join(BASE_DIR, "config.yaml"),
        os.path.join(home, ".config", "clash", "config.yaml"),
        os.path.join(home, ".config", "mihomo", "config.yaml"),
        os.path.join(home, ".config", "clash-verge", "config.yaml"),
    ]
    # Clash for Windows 把 config.yaml 放在安装目录的 Data/ 下，
    # 安装位置因人而异，这里扫一批常见落点
    roots = [
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.environ.get("LOCALAPPDATA", os.path.join(home, "AppData", "Local")),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs"),
        "D:\\software", "D:\\Program Files", "C:\\software",
    ]
    for r in roots:
        if not r:
            continue
        for name in ("Clash for Windows", "Clash", "clash", "mihomo", "Clash Verge"):
            cands.append(os.path.join(r, name, "Data", "config.yaml"))
            cands.append(os.path.join(r, name, "config.yaml"))

    for c in cands:
        try:
            if c and os.path.isfile(c):
                return c
        except Exception:
            continue
    # 一个都没找到：返回最通用的默认位置，便于报错信息给出可操作的提示
    return env or os.path.join(home, ".config", "clash", "config.yaml")


CLASH_CONFIG = find_clash_config()

# Windows 下强制 stdout 用 UTF-8，配合 .bat 里的 chcp 65001
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass


# --------------------------------------------------------------------------
# 终端宽度（中文字符占两列，避免表格错位）
# --------------------------------------------------------------------------

def _w(s):
    """字符串显示宽度：CJK / 全角字符按 2 列计。"""
    w = 0
    for ch in s:
        o = ord(ch)
        if (0x1100 <= o <= 0x115F or 0x2E80 <= o <= 0xA4CF or
                0xAC00 <= o <= 0xD7A3 or 0xF900 <= o <= 0xFAFF or
                0xFE30 <= o <= 0xFE6F or 0xFF00 <= o <= 0xFF60 or
                0xFFE0 <= o <= 0xFFE6 or 0x20000 <= o <= 0x3FFFD):
            w += 2
        elif o == 0xFE0F or 0x1F300 <= o <= 0x1FAFF:      # emoji
            w += 2
        elif 0xFE00 <= o <= 0xFE0F:
            w += 0
        else:
            w += 1
    return w


def pad(s, width, align="left"):
    """按显示宽度补齐 / 截断。"""
    s = str(s)
    cur = _w(s)
    if cur > width:
        out, acc = "", 0
        for ch in s:
            cw = _w(ch)
            if acc + cw > width - 1:
                break
            out += ch
            acc += cw
        return out + "…" + " " * max(0, width - acc - 1)
    fill = " " * (width - cur)
    return fill + s if align == "right" else s + fill


def pretty_bytes(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024.0:
            return ("%.2f %s" % (n, unit)) if unit != "B" else ("%d B" % n)
        n /= 1024.0
    return "%.2f PB" % n


def fmt_ts(ts, pat="%Y-%m-%d %H:%M:%S"):
    return datetime.fromtimestamp(ts).strftime(pat)


def banner(title, ch="="):
    line = ch * 74
    print()
    print(line)
    print("  " + title)
    print(line)


# --------------------------------------------------------------------------
# Clash 控制接口发现
# --------------------------------------------------------------------------

def read_clash_ctrl():
    """从 Data/config.yaml 读 external-controller 与 secret。

    Clash for Windows 每次启动会用随机端口，因此绝不能写死。
    可用环境变量 CLASH_MON_HOST / CLASH_MON_PORT / CLASH_MON_SECRET 覆盖（离线测试用）。
    """
    host, port, secret = None, None, ""
    if os.path.exists(CLASH_CONFIG):
        try:
            with open(CLASH_CONFIG, encoding="utf-8", errors="ignore") as f:
                txt = f.read()
            m = re.search(r"^\s*external-controller:\s*['\"]?([^'\"\s]+)", txt, re.M)
            if m:
                val = m.group(1)
                if ":" in val:
                    host, _, p = val.rpartition(":")
                    host = host or "127.0.0.1"
                    port = int(p)
                else:
                    host, port = "127.0.0.1", int(val)
            s = re.search(r"^\s*secret:\s*['\"]?([^'\"\s]+)", txt, re.M)
            if s:
                secret = s.group(1)
        except Exception:
            pass

    host = os.environ.get("CLASH_MON_HOST") or host or "127.0.0.1"
    env_port = os.environ.get("CLASH_MON_PORT")
    if env_port:
        port = int(env_port)
    secret = os.environ.get("CLASH_MON_SECRET", secret)
    return host, port, secret


def probe_ctrl(host, port, secret, timeout=2.0):
    """探测某 host:port 是否是活的 Clash API。"""
    if not port:
        return False
    if not tcp_alive(host, port, timeout):
        return False
    try:
        code, _ = api_raw(host, port, secret, "/version", timeout)
        return code == 200
    except Exception:
        return False


def tcp_alive(host, port, timeout=1.5):
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except Exception:
        return False


def find_ctrl():
    """返回 (host, port, secret)，找不到则抛 ClashNotRunning。"""
    host, port, secret = read_clash_ctrl()
    if probe_ctrl(host, port, secret):
        return host, port, secret

    # 配置里的端口失效（Clash 重启换了端口 / 配置未刷新）→ 扫一遍常见端口
    for p in (9090, 9097, 9091, 50876, 6170):
        if probe_ctrl("127.0.0.1", p, secret):
            return "127.0.0.1", p, secret
    for p in scan_listening_ports():
        if probe_ctrl("127.0.0.1", p, secret):
            return "127.0.0.1", p, secret
    raise ClashNotRunning(
        "无法连接 Clash 控制接口。请依次检查：\n"
        "  1) Clash / mihomo 是否已启动；\n"
        "  2) 配置文件中是否设置了 external-controller（Clash for Windows\n"
        "     默认关闭外部控制，需在 Settings 里打开）；\n"
        "  3) 配置文件路径是否正确——当前使用的是：\n"
        "     %s\n"
        "     如不正确，请设置环境变量 CLASH_MON_CONFIG 指向真正的 config.yaml。"
        % CLASH_CONFIG
    )


def scan_listening_ports():
    """netstat 抓取本机监听端口（内核随机端口兜底）。"""
    ports = []
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                             capture_output=True, text=True, timeout=10,
                             encoding="utf-8", errors="ignore").stdout
        for line in out.splitlines():
            if "LISTENING" not in line:
                continue
            m = re.search(r"127\.0\.0\.1:(\d+)\s", line)
            if m:
                p = int(m.group(1))
                if p not in ports and p != 7890:
                    ports.append(p)
    except Exception:
        pass
    return ports[:40]


class ClashNotRunning(Exception):
    pass


# --------------------------------------------------------------------------
# HTTP 客户端（必须绕过本地代理，否则请求会被 7890 转发）
# --------------------------------------------------------------------------

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def api_raw(host, port, secret, path, timeout=8.0):
    url = "http://%s:%d%s" % (host, port, path)
    req = urllib.request.Request(url)
    if secret:
        req.add_header("Authorization", "Bearer " + secret)
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception:
        return 0, b""


def api_get(host, port, secret, path):
    code, body = api_raw(host, port, secret, path)
    if code != 200:
        raise ClashNotRunning("控制接口返回 HTTP %s（%s）" % (code, path))
    return json.loads(body.decode("utf-8", "replace"))


# --------------------------------------------------------------------------
# 数据库
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS samples (
    ts            REAL PRIMARY KEY,
    upload_total  INTEGER,
    download_total INTEGER,
    conn_count    INTEGER
);

CREATE TABLE IF NOT EXISTS events (
    ts        REAL,
    cid       TEXT,
    host      TEXT,
    ip        TEXT,
    port      INTEGER,
    process   TEXT,
    proc_path TEXT,
    chain     TEXT,
    rule      TEXT,
    network   TEXT,
    up        INTEGER,
    down      INTEGER,
    conn_start REAL,
    PRIMARY KEY (ts, cid)
);

CREATE INDEX IF NOT EXISTS idx_events_ts   ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_host ON events(host);
CREATE INDEX IF NOT EXISTS idx_events_proc ON events(process);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.execute("PRAGMA journal_mode=WAL")      # 采样进程写 / 报告进程读并发
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def db_init():
    with db_connect() as c:
        c.executescript(SCHEMA)


# --------------------------------------------------------------------------
# 采样
# --------------------------------------------------------------------------

def record_sample(conn, ts, data, state):
    """把一次 /connections 快照的增量写入库。

    state: {cid: last_up} 跨采样保持，用于差分。
    返回 (全局上传增量, 归因上传增量)
    """
    cur_up_total = int(data.get("uploadTotal") or 0)
    cur_down_total = int(data.get("downloadTotal") or 0)
    conns = data.get("connections") or []

    last_total = state.get("_total_up")
    global_delta = 0 if last_total is None else max(0, cur_up_total - last_total)
    state["_total_up"] = cur_up_total
    state["_total_down"] = cur_down_total

    rows = []
    seen = set()
    attributed = 0

    for c in conns:
        cid = c.get("id") or ""
        if not cid:
            continue
        seen.add(cid)
        m = c.get("metadata") or {}
        up_now = int(c.get("upload") or 0)
        down_now = int(c.get("download") or 0)
        prev = state.get(cid)
        if prev is None:
            d_up, d_down = up_now, down_now          # 新连接：首采全额计入
        else:
            d_up = max(0, up_now - prev[0])
            d_down = max(0, down_now - prev[1])
        state[cid] = (up_now, down_now)
        attributed += d_up

        if d_up <= 0 and d_down <= 0:
            continue

        chains = m.get("chains") or []
        rows.append((
            ts, cid,
            m.get("host") or "",
            m.get("destinationIP") or "",
            int(m.get("destinationPort") or 0),
            m.get("process") or "",
            m.get("processPath") or "",
            chains[-1] if chains else "",
            m.get("rule") or "",
            m.get("network") or "",
            d_up, d_down,
            _parse_iso(m.get("start")) or 0.0,
        ))

    # 清掉已消失的连接，避免 state 无限增长
    for cid in [k for k in state if not k.startswith("_") and k not in seen]:
        del state[cid]

    with conn:
        conn.execute(
            "INSERT OR REPLACE INTO samples(ts,upload_total,download_total,conn_count)"
            " VALUES(?,?,?,?)", (ts, cur_up_total, cur_down_total, len(conns)))
        if rows:
            conn.executemany(
                "INSERT OR REPLACE INTO events(ts,cid,host,ip,port,process,"
                "proc_path,chain,rule,network,up,down,conn_start)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return global_delta, attributed


def _parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def log(msg=""):
    """带 flush 的输出。

    后台运行时 stdout 被重定向到日志文件，此时是块缓冲；不显式 flush
    会导致日志长时间不落盘，出问题时看不到现场。
    """
    try:
        print(msg)
        sys.stdout.flush()
    except Exception:
        pass


def run_sampler(foreground=True):
    db_init()
    log("采样器启动，间隔 %d 秒。Ctrl+C 停止。" % SAMPLE_INTERVAL)
    host, port, secret = find_ctrl()
    log("Clash 控制接口：%s:%d" % (host, port))
    log("Clash 配置文件：%s" % CLASH_CONFIG)
    log("数据库：%s" % DB_PATH)
    log()

    conn = db_connect()
    state = {}
    last_beat = 0
    last_cleanup = 0
    errors = 0

    try:
        while True:
            t0 = time.time()
            try:
                data = api_get(host, port, secret, "/connections")
                errors = 0
            except Exception as e:
                errors += 1
                # Clash 重启后会换一个随机控制端口，此时必须重新发现，
                # 否则采样器会一直失败直至退出。
                if errors == 1 or errors % 5 == 0:
                    try:
                        nh, np_, ns = find_ctrl()
                        if (nh, np_) != (host, port):
                            log("[%s] 控制接口已变化：%s:%d → %s:%d，已重连"
                                % (fmt_ts(t0, "%H:%M:%S"), host, port, nh, np_))
                            host, port, secret = nh, np_, ns
                            errors = 0
                            continue
                    except ClashNotRunning:
                        pass
                log("[%s] 读取失败(%d)：%s" % (fmt_ts(t0, "%H:%M:%S"), errors, e))
                if errors >= 20:
                    log("连续失败 20 次，退出。请检查 Clash 是否仍在运行。")
                    break
                time.sleep(min(SAMPLE_INTERVAL, 5 * errors))
                continue

            g, a = record_sample(conn, t0, data, state)

            # 每小时清理一次过期数据
            if t0 - last_cleanup > 3600:
                last_cleanup = t0
                cutoff = t0 - RETAIN_DAYS * 86400
                try:
                    with conn:
                        conn.execute("DELETE FROM events WHERE ts < ?", (cutoff,))
                        conn.execute("DELETE FROM samples WHERE ts < ?", (cutoff,))
                except Exception:
                    pass

            if foreground and t0 - last_beat >= 60:
                last_beat = t0
                cov = (100.0 * a / g) if g else 0.0
                log("[%s] 采样正常 | 本次区间上传 %s | 归因覆盖 %.0f%% | 活跃连接 %d"
                    % (fmt_ts(t0, "%H:%M:%S"), pretty_bytes(g), cov,
                       len(data.get("connections") or [])))

            dt = time.time() - t0
            time.sleep(max(1.0, SAMPLE_INTERVAL - dt))
    except KeyboardInterrupt:
        log("\n已停止采样。")
    finally:
        try:
            conn.close()
        except Exception:
            pass


# --------------------------------------------------------------------------
# 进程管理
# --------------------------------------------------------------------------

def read_pid():
    try:
        with open(PID_PATH) as f:
            return int(f.read().strip())
    except Exception:
        return None


def pid_alive(pid):
    if not pid:
        return False
    try:
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/NH"],
                             capture_output=True, text=True, timeout=10,
                             encoding="utf-8", errors="ignore").stdout
        return str(pid) in out
    except Exception:
        return False


def start_background():
    pid = read_pid()
    if pid_alive(pid):
        print("采样器已在运行（PID %d）。" % pid)
        return
    creation = 0
    if sys.platform == "win32":
        DETACHED_PROCESS = 0x00000008
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        CREATE_NO_WINDOW = 0x08000000
        creation = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
    log = open(LOG_PATH, "a", encoding="utf-8")
    log.write("\n===== 启动于 %s =====\n" % fmt_ts(time.time()))
    log.flush()
    p = subprocess.Popen(
        [sys.executable, os.path.abspath(__file__), "run"],
        stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        creationflags=creation, close_fds=True,
        cwd=BASE_DIR,
    )
    with open(PID_PATH, "w") as f:
        f.write(str(p.pid))
    time.sleep(1.5)
    if pid_alive(p.pid):
        print("采样器已后台启动（PID %d）。" % p.pid)
        print("日志：%s" % LOG_PATH)
    else:
        print("启动失败，请查看日志：%s" % LOG_PATH)
        try:
            print(open(LOG_PATH, encoding="utf-8").read()[-800:])
        except Exception:
            pass


def stop_background():
    pid = read_pid()
    if not pid_alive(pid):
        print("采样器未在运行。")
        return
    try:
        subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                       capture_output=True, text=True, timeout=15)
        time.sleep(0.5)
        print("已停止采样器（PID %d）。" % pid)
    except Exception as e:
        print("停止失败：%s" % e)


def show_status():
    banner("采样器状态")
    pid = read_pid()
    running = pid_alive(pid)
    print("  运行状态 : %s" % ("● 运行中 (PID %d)" % pid if running else "○ 未运行"))
    print("  数据目录 : %s" % DATA_DIR)
    print("  数据库   : %s (%s)"
          % (DB_PATH, pretty_bytes(os.path.getsize(DB_PATH)) if os.path.exists(DB_PATH) else "尚未创建"))

    host, port, secret = read_clash_ctrl()
    ok = probe_ctrl(host, port, secret)
    print("  Clash    : %s" % ("● 已连接 %s:%d" % (host, port) if ok else "○ 未连接（%s:%d）" % (host, port)))

    if not os.path.exists(DB_PATH):
        return
    try:
        conn = db_connect()
        row = conn.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM samples").fetchone()
        n, t0, t1 = row
        if n:
            print("  采样点数 : %d" % n)
            print("  覆盖时段 : %s → %s" % (fmt_ts(t0), fmt_ts(t1)))
        else:
            print("  采样点数 : 0（尚未采集到数据）")
        conn.close()
    except Exception as e:
        print("  读取数据库失败：%s" % e)


# --------------------------------------------------------------------------
# 报告
# --------------------------------------------------------------------------

def _hours_arg():
    if len(sys.argv) > 2:
        try:
            return float(sys.argv[2])
        except ValueError:
            pass
    return 24.0


def gather(conn, since):
    out = {}

    row = conn.execute(
        "SELECT COUNT(*), MIN(ts), MAX(ts) FROM samples WHERE ts>=?", (since,)
    ).fetchone()
    out["n_samples"] = int(row[0] or 0)
    out["range"] = (row[1], row[2])
    have = row[1] is not None

    tot = conn.execute(
        "SELECT COALESCE(SUM(up),0), COALESCE(SUM(down),0) FROM events WHERE ts>=?",
        (since,)).fetchone()
    out["attr_up"], out["attr_down"] = int(tot[0]), int(tot[1])

    # 全局增量 = 区间首末两次采样累计值之差（含已关闭连接，是真实总量）
    out["global_up"] = out["global_down"] = 0
    if have:
        for col, key in (("upload_total", "global_up"), ("download_total", "global_down")):
            a = conn.execute("SELECT %s FROM samples WHERE ts>=? ORDER BY ts ASC LIMIT 1"
                             % col, (since,)).fetchone()
            b = conn.execute("SELECT %s FROM samples WHERE ts>=? ORDER BY ts DESC LIMIT 1"
                             % col, (since,)).fetchone()
            out[key] = max(0, int(b[0] or 0) - int(a[0] or 0))

    def top(sql, limit=25):
        return [(str(r[0]), int(r[1] or 0), int(r[2] or 0))
                for r in conn.execute(sql, (since, limit)).fetchall()]

    out["by_process"] = top(
        "SELECT COALESCE(NULLIF(process,''),'(未识别)') k,"
        " COALESCE(SUM(up),0), COALESCE(SUM(down),0)"
        " FROM events WHERE ts>=? GROUP BY k ORDER BY SUM(up) DESC LIMIT ?")
    out["by_host"] = top(
        "SELECT COALESCE(NULLIF(host,''),'IP:'||COALESCE(NULLIF(ip,''),'?')) k,"
        " COALESCE(SUM(up),0), COALESCE(SUM(down),0)"
        " FROM events WHERE ts>=? GROUP BY k ORDER BY SUM(up) DESC LIMIT ?")
    out["by_chain"] = top(
        "SELECT COALESCE(NULLIF(chain,''),'(直连/无)') k,"
        " COALESCE(SUM(up),0), COALESCE(SUM(down),0)"
        " FROM events WHERE ts>=? GROUP BY k ORDER BY SUM(up) DESC LIMIT ?")
    out["by_rule"] = top(
        "SELECT COALESCE(NULLIF(rule,''),'(无)') k,"
        " COALESCE(SUM(up),0), COALESCE(SUM(down),0)"
        " FROM events WHERE ts>=? GROUP BY k ORDER BY SUM(up) DESC LIMIT ?")

    out["by_hour"] = [
        (fmt_ts(int(r[0]), "%m-%d %H:00"), int(r[1] or 0), int(r[2] or 0))
        for r in conn.execute(
            "SELECT CAST(ts/3600 AS INTEGER)*3600 h, COALESCE(SUM(up),0), COALESCE(SUM(down),0)"
            " FROM events WHERE ts>=? GROUP BY h ORDER BY h", (since,)).fetchall()]

    out["top_conn"] = [
        (str(r[0] or ""), str(r[1] or ""), str(r[2] or ""), int(r[3] or 0),
         int(r[4] or 0), str(r[5] or ""), float(r[6] or 0))
        for r in conn.execute(
            "SELECT COALESCE(host,''), COALESCE(ip,''),"
            " COALESCE(NULLIF(process,''),'(未识别)'),"
            " COALESCE(SUM(up),0), COALESCE(SUM(down),0),"
            " COALESCE(NULLIF(chain,''),'(直连)'), MAX(ts)"
            " FROM events WHERE ts>=? GROUP BY cid"
            " ORDER BY SUM(up) DESC LIMIT 15", (since,)).fetchall()]
    return out


def _table(title, rows, total_up, limit=15):
    print()
    print("── %s %s" % (title, "─" * max(0, 60 - _w(title))))
    if not rows:
        print("   （无数据）")
        return
    print("   %-3s %-40s %11s %8s" % ("#", "名称", "上传", "占比"))
    for i, (name, up, _dn) in enumerate(rows[:limit], 1):
        pct = (100.0 * up / total_up) if total_up else 0.0
        print("   %-3d %-40s %11s %7.1f%%"
              % (i, pad(name, 40), pretty_bytes(up), pct))


def print_report(conn, since):
    d = gather(conn, since)
    t0, t1 = d["range"]
    banner("Clash 上传流量归因报告")
    if t0 is None:
        print("  该时段没有任何采样数据。")
        print("  请先启动采样器（菜单选 1），跑一段时间后再看报告。")
        return d

    span_h = (t1 - t0) / 3600.0
    print("  统计时段 : %s → %s  (%.2f 小时)" % (fmt_ts(t0), fmt_ts(t1), span_h))
    print("  采样点数 : %d" % d["n_samples"])
    print()
    print("  全局上传 : %s" % pretty_bytes(d["global_up"]))
    print("  归因上传 : %s" % pretty_bytes(d["attr_up"]))
    cov = (100.0 * d["attr_up"] / d["global_up"]) if d["global_up"] else 0.0
    print("  归因覆盖 : %.1f%%%s"
          % (cov, "" if 85 <= cov <= 115 else "   ← 偏差较大，见文末说明"))
    avg = d["global_up"] / max(span_h * 3600, 1) / 1024.0
    print("  平均上速 : %.1f KB/s" % avg)

    _table("上传来源 · 按进程", d["by_process"], d["attr_up"])
    _table("上传去向 · 按域名", d["by_host"], d["attr_up"])
    _table("上传出口 · 按节点", d["by_chain"], d["attr_up"])
    _table("命中的规则", d["by_rule"], d["attr_up"])

    print()
    print("── 单条连接上传榜（已按连接归并）%s" % ("─" * 34))
    print("   %9s  %-28s %-18s %-15s %s"
          % ("上传", "目标", "进程", "出口", "最后活跃"))
    for host, ip, proc, up, _dn, chain, last in d["top_conn"]:
        print("   %9s  %-28s %-18s %-15s %s"
              % (pretty_bytes(up), pad(host or ip, 28), pad(proc, 18),
                 pad(chain, 15), fmt_ts(last, "%H:%M:%S")))

    if d["by_hour"]:
        print()
        print("── 上传趋势（按小时） %s" % ("─" * 40))
        mx = max(x[1] for x in d["by_hour"]) or 1
        for label, up, _dn in d["by_hour"][-24:]:
            bar = "█" * max(1, int(38.0 * up / mx)) if up else ""
            print("   %-14s %11s  %s" % (label, pretty_bytes(up), bar))

    print()
    print("说明：归因覆盖 <100% 表示有部分上传来自采样区间内已关闭、")
    print("      来不及观测的连接；>100% 表示新连接把建立以来的累计量")
    print("      一次性计入了当前区间。拉长采样时长可收敛。")
    return d


def write_html(conn, since, d):
    t0, t1 = d["range"]
    if t0 is None:
        return None
    cov = (100.0 * d["attr_up"] / d["global_up"]) if d["global_up"] else 0.0
    span_h = (t1 - t0) / 3600.0

    def bars(rows, total, unit_col="上传"):
        if not rows:
            return '<p class="empty">无数据</p>'
        mx = max(r[1] for r in rows) or 1
        out = []
        for name, up, dn in rows[:20]:
            w = max(0.8, 100.0 * up / mx)
            pct = (100.0 * up / total) if total else 0
            esc = (name.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))
            out.append(
                '<div class="row"><div class="nm" title="%s">%s</div>'
                '<div class="tr"><div class="bar" style="width:%.2f%%"></div></div>'
                '<div class="vl">%s<span class="pc">%.1f%%</span></div></div>'
                % (esc, esc, w, pretty_bytes(up), pct))
        return "\n".join(out)

    hour_rows = [(h, u, dn) for h, u, dn in d["by_hour"]]

    def section(title, key):
        return ('<section><h2>%s</h2><div class="chart">%s</div></section>'
                % (title, bars(d.get(key) or [], d["attr_up"])))

    def esc(x):
        return (str(x).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))

    conn_rows = "\n".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td><td class='num'>%s</td>"
        "<td>%s</td><td class='ts'>%s</td></tr>"
        % (esc(h or ip), esc(ip), esc(p), pretty_bytes(up), esc(cha),
           fmt_ts(last, "%H:%M:%S"))
        for h, ip, p, up, _dn, cha, last in d["top_conn"])

    hour_mx = max([u for _h, u, _d in hour_rows] or [1]) or 1
    hour_bars = "\n".join(
        '<div class="hrow"><div class="hl">%s</div>'
        '<div class="hb" style="width:%.2f%%"></div>'
        '<div class="hv">%s</div></div>'
        % (h, max(0.4, 100.0 * u / hour_mx), pretty_bytes(u))
        for h, u, _dn in hour_rows[-48:])

    tpl = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Clash 上传流量归因报告</title>
<style>
  :root{
    --bg:#f6f7f9; --card:#ffffff; --ink:#1a1d21; --sub:#5c6470;
    --line:#e4e7ec; --accent:#2563eb; --up:#e5484d; --down:#0d9488;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--ink);
       font:14px/1.6 "Microsoft YaHei","PingFang SC","Helvetica Neue",Arial,sans-serif}
  .wrap{max-width:960px;margin:0 auto;padding:28px 20px 60px}
  h1{font-size:21px;margin:0 0 4px;letter-spacing:.2px}
  .sub{color:var(--sub);font-size:13px;margin-bottom:22px}
  .kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(158px,1fr));gap:12px;margin-bottom:24px}
  .kpi{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
  .kpi .k{color:var(--sub);font-size:12px;margin-bottom:6px}
  .kpi .v{font-size:19px;font-weight:600;font-variant-numeric:tabular-nums}
  .kpi .v.up{color:var(--up)}
  section{background:var(--card);border:1px solid var(--line);border-radius:10px;
          padding:18px 20px;margin-bottom:16px}
  h2{font-size:15px;margin:0 0 14px;padding-left:9px;border-left:3px solid var(--accent)}
  .row{display:flex;align-items:center;gap:10px;margin:7px 0;font-size:13px}
  .nm{width:210px;flex:none;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#333}
  .tr{flex:1;background:#f0f2f5;border-radius:4px;height:16px;overflow:hidden}
  .bar{height:100%;background:linear-gradient(90deg,#e5484d,#f97316);border-radius:4px}
  .vl{width:132px;flex:none;text-align:right;font-variant-numeric:tabular-nums;color:var(--sub)}
  .pc{display:inline-block;width:48px;color:#98a2b3;font-size:12px}
  .hrow{display:flex;align-items:center;gap:8px;font-size:12px;margin:3px 0}
  .hl{width:88px;flex:none;color:var(--sub);font-variant-numeric:tabular-nums}
  .hb{background:linear-gradient(90deg,#2563eb,#60a5fa);height:12px;border-radius:3px}
  .hv{color:var(--sub);font-variant-numeric:tabular-nums}
  table{width:100%;border-collapse:collapse;font-size:13px}
  th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line)}
  th{color:var(--sub);font-weight:500;font-size:12px}
  td.num{font-variant-numeric:tabular-nums;color:var(--up);font-weight:600}
  td.ts{color:#98a2b3;font-variant-numeric:tabular-nums}
  .note{color:var(--sub);font-size:12px;line-height:1.8}
  .empty{color:#98a2b3;font-size:13px}
</style>
</head>
<body>
<div class="wrap">
  <h1>Clash 上传流量归因报告</h1>
  <div class="sub">统计时段 __T0__ → __T1__　·　跨度 __SPAN__ 小时　·　采样点 __N__ 个</div>

  <div class="kpis">
    <div class="kpi"><div class="k">全局上传</div><div class="v up">__GUP__</div></div>
    <div class="kpi"><div class="k">归因上传</div><div class="v">__AUP__</div></div>
    <div class="kpi"><div class="k">归因覆盖率</div><div class="v">__COV__%</div></div>
    <div class="kpi"><div class="k">平均上速</div><div class="v">__AVG__ KB/s</div></div>
  </div>

  __S_PROC__
  __S_HOST__
  __S_CHAIN__

  <section>
    <h2>上传趋势（按小时）</h2>
    <div class="chart">__HOURBARS__</div>
  </section>

  <section>
    <h2>单条连接上传榜 Top 15</h2>
    <table>
      <thead><tr><th>目标</th><th>IP</th><th>进程</th><th>上传</th><th>出口</th><th>最后活跃</th></tr></thead>
      <tbody>__CONNROWS__</tbody>
    </table>
  </section>

  <section>
    <h2>口径说明</h2>
    <p class="note">
      数据来自定时轮询 Clash <code>/connections</code> 并做差分采样（间隔 __IV__ 秒）。<br>
      归因覆盖率 &lt;100% 表示部分上传来自区间内已关闭、来不及观测的连接（Clash 不保留关闭连接的明细）；<br>
      覆盖率 &gt;100% 表示新建立的连接把其建立以来的累计上传一次性计入了当前区间。<br>
      采样时长越长，覆盖率越收敛。
    </p>
  </section>
</div>
</body>
</html>
"""
    avg = d["global_up"] / max(span_h * 3600, 1) / 1024.0
    out = (tpl
           .replace("__T0__", fmt_ts(t0))
           .replace("__T1__", fmt_ts(t1))
           .replace("__SPAN__", "%.2f" % span_h)
           .replace("__N__", str(d["n_samples"]))
           .replace("__GUP__", pretty_bytes(d["global_up"]))
           .replace("__AUP__", pretty_bytes(d["attr_up"]))
           .replace("__COV__", "%.1f" % cov)
           .replace("__AVG__", "%.1f" % avg)
           .replace("__S_PROC__", section("上传来源 · 按进程", "by_process"))
           .replace("__S_HOST__", section("上传去向 · 按域名", "by_host"))
           .replace("__S_CHAIN__", section("上传出口 · 按节点", "by_chain"))
           .replace("__HOURBARS__", hour_bars or '<p class="empty">无数据</p>')
           .replace("__CONNROWS__", conn_rows or '<tr><td colspan="5" class="empty">无数据</td></tr>')
           .replace("__IV__", str(SAMPLE_INTERVAL)))

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(REPORT_DIR, "clash-upload-report-%s.html" % stamp)
    with open(path, "w", encoding="utf-8") as f:
        f.write(out)
    return path


def do_report(open_it=True):
    if not os.path.exists(DB_PATH):
        banner("上传流量归因报告")
        print("  数据库尚不存在，请先启动采样器。")
        return
    since = time.time() - _hours_arg() * 3600
    conn = db_connect()
    d = print_report(conn, since)
    path = None
    if d and d.get("range") and d["range"][0] is not None:
        path = write_html(conn, since, d)
    conn.close()
    if path:
        print()
        print("HTML 报告：%s" % path)
        if open_it and sys.platform == "win32":
            try:
                os.startfile(path)
            except Exception:
                pass


# --------------------------------------------------------------------------
# 菜单
# --------------------------------------------------------------------------

MENU = """
╔══════════════════════════════════════════════════════════════════════╗
║               Clash 上传流量归因采样器                                ║
╠══════════════════════════════════════════════════════════════════════╣
║   Clash 不保留历史流量明细，本工具通过轮询 /connections 差分采样，    ║
║   还原「最近 N 小时谁在上传」。                                       ║
╚══════════════════════════════════════════════════════════════════════╝
"""


def menu():
    while True:
        os.system("cls" if sys.platform == "win32" else "clear")
        print(MENU)
        pid = read_pid()
        running = pid_alive(pid)
        state = ("● 运行中 (PID %d)" % pid) if running else "○ 未运行"
        print("   采样器状态：%s\n" % state)

        print("   [1] 启动采样（后台常驻）")
        print("   [2] 停止采样")
        print("   [3] 查看上传分布报告")
        print("   [4] 查看状态 / 连接诊断")
        print("   [5] 前台运行（调试用，可看实时日志）")
        print("   [0] 退出")
        print()
        try:
            ch = input("   请选择 > ").strip()
        except (EOFError, KeyboardInterrupt):
            return

        if ch == "1":
            print()
            start_background()
            print()
            input("   按回车返回菜单…")
        elif ch == "2":
            print()
            stop_background()
            print()
            input("   按回车返回菜单…")
        elif ch == "3":
            do_report(open_it=True)
            print()
            input("   按回车返回菜单…")
        elif ch == "4":
            show_status()
            print()
            input("   按回车返回菜单…")
        elif ch == "5":
            print()
            try:
                run_sampler(foreground=True)
            except Exception as e:
                print("运行失败：%s" % e)
            print()
            input("   按回车返回菜单…")
        elif ch == "0":
            return
        else:
            continue


# --------------------------------------------------------------------------

def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "menu"
    try:
        if cmd == "run":
            run_sampler(foreground=True)
        elif cmd == "start":
            start_background()
        elif cmd == "stop":
            stop_background()
        elif cmd == "status":
            show_status()
        elif cmd == "report":
            do_report(open_it="--no-open" not in sys.argv)
        elif cmd == "menu":
            menu()
        else:
            print(__doc__)
    except ClashNotRunning as e:
        print()
        print("!! %s" % e)
        print()
        if cmd in ("menu", "run"):
            try:
                input("按回车返回…")
            except Exception:
                pass
        sys.exit(1)


if __name__ == "__main__":
    main()
