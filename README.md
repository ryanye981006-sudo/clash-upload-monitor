# Clash 上传流量归因采样器

> 还原「最近几小时到底是谁在上传」——Clash 自己不会告诉你。

[English summary](#english-summary) · 中文文档

---

## 它解决什么问题

Clash 不提供任何历史流量统计，这是它的设计取舍：

| 你想要的 | Clash 实际给的 |
|---|---|
| 最近 1 小时按软件分的上传量 | ✗ 没有 |
| 历史流量明细查询 | ✗ 没有 |
| 当前活跃连接的上传字节 | ✓ 有，但连接一关闭立即消失 |
| 内核启动以来的累计上传量 | ✓ 有，但重启清零、无明细 |
| 日志里的目标域名 / 规则 / 出口 | ✓ 有，但**不含字节数** |

结果就是一个很别扭的处境：你能看到「本次已累计上传 4.7 GB」，却**无法回答这 4.7 GB 是谁传的**——因为绝大多数上传发生在已经断开的连接上，而 Clash 在连接关闭的那一刻就把明细丢了。

本工具用一个笨但有效的办法补上这一环：**每 15 秒轮询一次 `/connections`，对每条连接的累计字节做差分**，把增量持久化到 SQLite。跑上几小时到几天，就能还原出任意时段的上传分布，并按**进程 / 域名 / 出口节点 / 规则**四个维度归因。

## 原理

```
          每 15 秒
Clash ──────────────►  /connections 快照
  │                        │
  │                        ├─ 逐连接差分 upload 字段 → 增量
  │                        ├─ 新连接首采：全额计入
  │                        └─ 消失的连接：从状态表移除
  │                        │
  │                        ▼
  │                   SQLite (WAL)
  │                        │
  └─ uploadTotal ────►  与 Σ增量 比对 → 归因覆盖率
                             │
                             ▼
                       终端表格 / HTML 报告
```

两个关键设计：

- **控制端口动态发现**：Clash for Windows 每次启动都会分配一个随机
  `external-controller` 端口（不是固定的 9090）。写死端口的工具必然在某次重启后失效。本工具每次从 `config.yaml` 现读端口和 secret，并在读取失败时自动重新发现、无缝重连。
- **归因覆盖率是硬指标**：报告里会同时给出「全局上传量」（来自 `uploadTotal` 差值，真实总量）和「归因上传量」（来自各连接增量之和）。两者的比值就是覆盖率。
  连接关闭过快时采样必然会漏掉尾巴，这个数字告诉你当前结论有多可信。

## 快速开始

**环境要求**：Python 3.8+（仅用标准库，无需安装任何依赖）；Clash / mihomo 已开启外部控制接口。

```bash
git clone https://github.com/ryanye981006-sudo/clash-upload-monitor.git
cd clash-upload-monitor
python monitor.py menu
```

Windows 用户也可以直接双击 `start.bat`（会自动定位 Python 解释器）。

### 菜单功能

```
[1] 启动采样（后台常驻）     ← 双击后选这个，然后就别管了
[2] 停止采样
[3] 查看上传分布报告          ← 终端表格 + 自动打开 HTML 报告
[4] 查看状态 / 连接诊断       ← 确认 Clash 是否连上、已采集多少点
[5] 前台运行（调试用）        ← 可选，能实时看到采样心跳
[0] 退出
```

**采样器必须常驻才能积累数据。** 现在启动，明天才有「一整天的上传分布」可看。

### 直接命令行调用

```bash
python monitor.py start      # 后台启动
python monitor.py stop       # 停止
python monitor.py status     # 状态诊断
python monitor.py report     # 生成报告（可加小时数：report 6 表示近 6 小时）
python monitor.py run        # 前台运行
```

## 报告解读

终端报告的样例（数据为模拟值）：

```
  统计时段 : 2026-09-29 10:03:25 → 2026-09-29 10:09:25  (0.10 小时)
  采样点数 : 24

  全局上传 : 113.02 MB
  归因上传 : 110.85 MB
  归因覆盖 : 98.1%
  平均上速 : 321.5 KB/s

── 上传来源 · 按进程 ──────────────────────────────
   #   名称                                        上传       占比
   1   quark.exe                              72.14 MB    65.1%
   2   ChatGPT.exe                            19.33 MB    17.4%
   3   chrome.exe                             12.05 MB    10.9%

── 上传去向 · 按域名 ──────────────────────────────
   1   pan.quark.cn                           51.22 MB    46.2%
   2   drive.quark.cn                         20.92 MB    18.9%
   ...
```

同时会在 `report/` 下生成一份自包含的 HTML 报告（内联 CSS、纯 CSS 柱状图，无外部依赖，
可直接分享给他人）。样例见 [`_test/demo-report.html`](_test/demo-report.html)（模拟数据）。

| 字段 | 含义 |
|---|---|
| 全局上传 | 区间首末 `uploadTotal` 之差，**真实总量**，含已关闭连接 |
| 归因上传 | 各连接采样增量之和，是能被归因的部分 |
| 归因覆盖率 | 归因上传 ÷ 全局上传。理想值 ≈ 100% |
| 平均上速 | 全局上传 ÷ 区间时长 |

**覆盖率怎么读**：

- **95% ~ 105%**：结果可信，可以放心下结论。
- **明显 < 100%**：有短命连接（来不及观测就关闭）贡献了上传。这类连接通常是
  小的 DNS / 心跳 / 上报请求，但也可能是快速上传小文件的行为。
- **> 100%**：采样启动瞬间，把某些连接的**建立以来累计量**一次性计入了。这只在
  刚开始采样的头几次出现，跑一会儿就收敛。

想提高覆盖率就缩短采样间隔（见下）。

## 配置

全部通过环境变量控制，无需改代码：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `CLASH_MON_INTERVAL` | `15` | 采样间隔（秒）。调小可提高覆盖率、增大数据库 |
| `CLASH_MON_RETAIN_DAYS` | `14` | 数据保留天数，超期每小时自动清理 |
| `CLASH_MON_CONFIG` | 自动探测 | `config.yaml` 路径。探测顺序见下 |
| `CLASH_MON_DATA` | `./data` | 数据目录 |
| `CLASH_MON_REPORT` | `./report` | 报告输出目录 |
| `CLASH_MON_DB` | `<data>/traffic.db` | 数据库文件路径 |
| `CLASH_MON_HOST` / `CLASH_MON_PORT` / `CLASH_MON_SECRET` | — | 手动指定控制接口，覆盖配置文件（离线测试用） |

`config.yaml` 自动探测顺序：`$CLASH_MON_CONFIG` → 本目录 → `~/.config/clash/` →
`~/.config/mihomo/` → `~/.config/clash-verge/` → 各常见安装目录的 `/Data/`。

找不到时程序会明确报错并提示你用 `CLASH_MON_CONFIG` 指定。

例如把间隔调成 5 秒：

```bash
CLASH_MON_INTERVAL=5 python monitor.py run
```

## 目录结构

```
clash-upload-monitor/
├── monitor.py                  # 全部逻辑，单文件
├── start.bat                   # Windows 双击启动器
├── _test/
│   ├── mock_clash.py           # 模拟 Clash API（供离线验证）
│   ├── test_e2e.py             # 端到端测试
│   ├── test_port_change.py     # 控制端口漂移自愈测试
│   └── demo-report.html        # 报告样例（模拟数据）
├── data/                       # 采样数据（已 gitignore）
└── report/                     # HTML 报告（已 gitignore）
```

## 测试

两个测试都**完全隔离**：使用独立端口 + 临时数据库，不会碰 `data/traffic.db`，
所以哪怕你的采样器正在运行也可以安全执行。

```bash
python _test/test_e2e.py          # 端到端：采样 → 落库 → 报告（默认 34 秒）
python _test/test_port_change.py  # 模拟 Clash 重启换端口，验证自愈
```

`test_e2e.py` 覆盖：差分正确性、关闭连接的捕获、多维度归因、报告渲染完整性。
`test_port_change.py` 覆盖一个真实的坑：Clash 重启后控制端口会变，采样器必须能
重新发现而不是一路失败到退出。

`mock_clash.py` 会模拟一条长期上传大户、一条中途关闭的连接，以及略大于归因值的
全局总量（用于验证覆盖率指标）。它的端口与 secret 从 `config.yaml` 读取，
**仓库里不保存任何凭据**。

## 已知限制

- **采样必然有漏量**。间隔期间建立又关闭的连接可能完全观测不到。要看
  「粗粒度分布」够用；要做精确计费请用专门的抓包工具。
- **只在 TCP 层归因**。Clash 的 `/connections` 不含 UDP 明细，UDP 流量只体现在
  `uploadTotal` 里，会拉低覆盖率。
- **归因到进程依赖 Clash 的能力**。Clash Premium 能识别进程；开源内核的
  `process` 字段常为空，此时会落到「(未识别)」——但按域名归因仍然有效。
- **数据库会增长**。15 秒间隔、几十条活跃连接的量级下，一天大约数十 MB。
  已内置 14 天保留策略。
- 本工具只读取 Clash 的控制接口，**不修改任何 Clash 配置**。

## 安全说明

- 仓库中不含任何凭据。控制接口的 `secret` 一律从你本地的 `config.yaml` 运行时读取。
- `data/` 与 `report/` 已在 `.gitignore` 中——它们包含你的真实流量明细
  （访问过的域名、进程路径、IP），**请勿提交到公开仓库**。
- 控制接口默认只监听 `127.0.0.1`。本工具不会把它暴露到局域网。

## License

MIT © 2026 ryanye981006-sudo

---

## English summary

Clash keeps **no historical traffic records**: `/connections` only holds currently
active connections (details vanish the moment a connection closes), `uploadTotal` is a
single cumulative counter reset on restart, and the kernel log has no byte counts. So
you can see *that* 4.7 GB was uploaded, but never *who* uploaded it.

This tool polls `/connections` every 15 seconds and **diffs each connection's byte
counters**, persisting the deltas to SQLite. After a few hours it can attribute upload
traffic by **process, domain, outbound proxy, and matched rule**, and reports an
**attribution coverage** ratio (`Σ deltas / ΔuploadTotal`) so you know how trustworthy
the numbers are.

Single file, standard library only, no dependencies.

```bash
python monitor.py start     # start background sampling
python monitor.py report    # generate report (terminal + HTML)
```

Key design points:

- **Dynamic control-port discovery.** Clash for Windows picks a *random*
  `external-controller` port on every launch (not 9090), so hardcoding it guarantees
  breakage. The port and secret are re-read from `config.yaml` at runtime, and the
  sampler auto-reconnects if the port changes mid-run.
- **Coverage ratio as a first-class metric.** Connections that open and close between
  samples are invisible; the coverage ratio quantifies how much was missed.

MIT licensed.
