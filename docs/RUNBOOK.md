# QuantDesk 运维手册（RUNBOOK）

> 版本：v1.3 · 2026-10-26 · 作者：交付文档维护（eval-data）
> 对应任务：M4-05（备份恢复脚本与运维手册）、M0-08（启动脚本与环境自检）
> 适用对象：**使用系统的用户**、Integrator、值班排查者（不要求会写代码）
> 本文只写"怎么做 + 出问题怎么排"；需求与契约见 `SPEC.md`，任务归属见 `TASKS.md`。

---

## 0. 30 秒速查

| 我要… | 去哪里 |
|---|---|
| 装环境 / 重建虚拟环境 | §1 |
| 启动系统 | §2（双击 `scripts\start.bat`） |
| 第一次跑起来该看到什么 | §3 |
| 报错了，按现象查 | §4 故障排查总表 |
| 找数据库 / 日志 / 导出文件 | §3 |
| 停掉正在跑的服务或自动化 | §5 |
| 备份 / 恢复数据库 | §6（`scripts\backup_db.py` / `scripts\restore_db.py`） |
| 升级 / 回滚 | §7 |
| 发版前自检（依赖/DB/数据源/测试/文档） | §7.3（`scripts\release_check.py`） |
| 常用命令一份带走 | 附录 B |

**本机基线（2026-10-25 实测）**：Windows 10 · Python 3.12.7（Anaconda）· venv 在 `quantdesk\.venv` · numpy 1.26.4 · pandas 2.2.2。完整版本表见附录 A。

---

## 1. 环境准备

### 1.1 目录与解释器约定（先记住这三条）

| 约定 | 路径 / 说明 |
|---|---|
| 项目根目录 | `C:\Users\shanghao\Documents\deepseek-harness\default-workspace\quantdesk` |
| 项目虚拟环境 | `quantdesk\.venv`（**所有命令优先用它**） |
| 解释器 | `quantdesk\.venv\Scripts\python.exe`（回退：`D:\Anaconda3\python.exe`） |
| 运行期目录 | `quantdesk\runtime`（DB / 日志 / 备份 / 导出，见 §5） |

> 规则：**永远不要用全局 Python 跑本项目**。用错解释器会出现"明明装了包却 import 不到"的假故障。
> 路径尽量不含中文和空格（部分第三方库在含空格路径下会失败）。

### 1.2 检查现有环境

```bat
cd /d C:\Users\shanghao\Documents\deepseek-harness\default-workspace\quantdesk
.venv\Scripts\python.exe scripts\check_env.py
```

- **退出码 0**：核心依赖齐备，可以直接进入 §2 启动。
- **退出码 1**：有核心依赖缺失或 Python 版本不符，脚本末尾会打印可直接复制的安装命令；按 §1.3 / §1.4 处理。

也可以手工确认解释器身份：

```bat
.venv\Scripts\python.exe -c "import sys;print(sys.executable, sys.version)"
```

期望输出包含 `...\quantdesk\.venv\Scripts\python.exe 3.12.7 ...`。

### 1.3 重建虚拟环境（`.venv` 损坏或版本错乱时）

```bat
cd /d C:\Users\shanghao\Documents\deepseek-harness\default-workspace\quantdesk
rmdir /s /q .venv
D:\Anaconda3\python.exe -m venv .venv
.venv\Scripts\python.exe -m pip install --isolated --index-url https://mirrors.aliyun.com/pypi/simple/ --upgrade pip
.venv\Scripts\python.exe -m pip install --isolated --index-url https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt --timeout 60
```

> `rmdir /s /q .venv` 会删除虚拟环境本身，**不影响** `runtime` 里的数据库与日志。

### 1.4 【踩坑 1+2】pip 全局 NVIDIA 源导致 SSL 卡死 & numpy/pandas 版本冲突

这是本机环境准备阶段**实际踩到的两个坑**，按顺序处理。

#### 坑 1：`pip install` 长时间无响应 / SSL 握手卡死

**现象**（任一条即符合）：

- `pip install` 停在 `Collecting ...` 几分钟不动，或卡在 `Looking in indexes: https://pypi.nvidia.com/...`；
- 报 `SSL: UNEXPECTED_EOF_WHILE_READING`、`ReadTimeoutError`、`ProxyError`、`CERTIFICATE_VERIFY_FAILED`；
- 换 `-i` 镜像也没用（因为全局配置里还有别的 index / trusted-host 在生效）。

**原因**：本机 pip 的**全局配置指向 NVIDIA 内部源**（内网/不可直连），pip 会先尝试它，在 SSL 握手阶段卡死；`pip.ini` 的全局设置优先级高于命令行 `-i` 之外的默认值，混用多源时容易反复重试。

**处理**：用 `--isolated` 忽略所有全局 pip 配置，并显式指定国内镜像：

```bat
.venv\Scripts\python.exe -m pip install --isolated --index-url https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt --timeout 60
```

备用镜像（阿里云不通时）：

```bat
.venv\Scripts\python.exe -m pip install --isolated --index-url https://pypi.tuna.tsinghua.edu.cn/simple -r requirements.txt --timeout 60
```

确认全局配置里到底有什么（用于判断是否需要长期清理）：

```bat
.venv\Scripts\python.exe -m pip config list -v
```

> 说明：`--isolated` 只对当次命令生效，不改动机器上的全局配置，最安全，推荐一直带着它。
> 需要定位是哪个源在卡：单独执行 `pip install --isolated -i https://mirrors.aliyun.com/pypi/simple/ <包名> -v`，看 `Looking in indexes` 行。

#### 坑 2：venv 与 Anaconda 的 numpy / pandas 版本冲突

**现象**：

- `ImportError: numpy.core.multiarray failed to import`；
- `ValueError: numpy.dtype size changed, may indicate binary incompatibility`；
- `A module that was compiled using NumPy 1.x cannot be run in NumPy 2.x`；
- `ImportError: pandas ... requires numpy>=2.x`（或反向要求 1.x）；
- `check_env.py` 显示 numpy/pandas 已装，但 `import pandas` 就崩。

**原因**：本机 venv 由 **Anaconda 3.12.7** 创建（`pyvenv.cfg` 中 `home = D:\Anaconda3`、`include-system-site-packages = false`）。Conda 环境的 numpy/pandas 是二进制编译版本，与 pip 装的版本、以及后续编译型依赖（TA-Lib/扩展包）会互相要求不同的 ABI，出现"版本打架"。

**处理**：**锁定并统一到本机验证组合 `numpy==1.26.4` + `pandas==2.2.2`**：

```bat
.venv\Scripts\python.exe -m pip install --isolated --index-url https://mirrors.aliyun.com/pypi/simple/ "numpy==1.26.4" "pandas==2.2.2"
```

验证：

```bat
.venv\Scripts\python.exe -c "import numpy,pandas;print(numpy.__version__, pandas.__version__)"
```

期望输出：`1.26.4 2.2.2`。

若装完仍有二进制不兼容残留（历史 wheel 混装），最干净的做法是 §1.3 **重建 venv** 后按锁定版本安装，再跑一次 `check_env.py`。
另检查是否有缓存版本的冲突包：

```bat
.venv\Scripts\python.exe -m pip check
```

> 为什么是这两个版本：pandas 2.2.2 与 numpy 1.26.4 是本项目实测可跑通回测与 akshare 的组合；numpy 2.x 会让部分依赖（含旧版 TA-Lib wheel）加载失败。版本最终锁定由任务 **M4-08** 写进 `requirements.txt`。

### 1.5 【踩坑 3】akshare 缺传递依赖

**现象**：`check_env.py` 显示 akshare 正常，但调用某些接口时抛
`ModuleNotFoundError: No module named 'bs4'` / `'lxml'` / `'openpyxl'` / `'tabulate'` / `'tqdm'` / `'decorator'`。

**原因**：akshare 的多个接口运行时依赖这些包，但它们**未全部写进 akshare 的安装依赖**，装 akshare 时不会自动带上。

**处理**：一次补齐（本机已装版本见附录 A）：

```bat
.venv\Scripts\python.exe -m pip install --isolated --index-url https://mirrors.aliyun.com/pypi/simple/ beautifulsoup4 lxml openpyxl tabulate tqdm decorator
```

验证：

```bat
.venv\Scripts\python.exe -c "import bs4,lxml,openpyxl,tabulate,tqdm,decorator;print('akshare 传递依赖 OK')"
```

### 1.6 【踩坑 4】TA-Lib 装不上（可选依赖，可跳过）

TA-Lib 是**可选**依赖：装不上时 `strategy/base.py` 的指标门面会自动切到 pandas 自实现（`sma/ema/rsi/macd/std`），接口保持一致，结果差异在容差内（AC-F2-04 要求 ≤1e-6），**功能与验收不受影响**。设置页会显示当前使用的实现。

想装的话，按成本从低到高：

| 方案 | 命令 | 说明 |
|---|---|---|
| ① 直接装（推荐先试） | `.venv\Scripts\python.exe -m pip install --isolated --index-url https://mirrors.aliyun.com/pypi/simple/ TA-Lib` | 新版 wheel 已内置 C 库，本机实装 0.8.2 成功 |
| ② 预编译 wheel | `.venv\Scripts\python.exe -m pip install 路径\TA_Lib-0.8.2-cp312-cp312-win_amd64.whl` | 按 **CPython 3.12 / win_amd64** 选文件；来源 <https://github.com/cgohlke/talib-build/releases> |
| ③ 不装 | —— | 什么都不做，自动走 pandas 兜底 |

> **不要**在 Windows 上从源码编译 TA-Lib（需要 MSVC + 底层 C 库，耗时且易失败）。报 `ImportError: DLL load failed while importing _ta_lib` → 说明 C 库缺失，改用方案 ①/②。

确认是否装好：

```bat
.venv\Scripts\python.exe -c "import talib;print('TA-Lib', talib.__version__)"
```

失败时命令会报 `ModuleNotFoundError` —— 这也是**允许**的状态，继续用系统即可。

---

## 2. 一键启动

### 2.1 双击启动（推荐给用户）

1. 打开 `quantdesk\scripts` 目录 → **双击 `start.bat`**；
2. 脚本按顺序做 5 件事：
   1. 切到项目根目录（`%~dp0..`）；
   2. 检查 `app.py` 是否存在（不存在 → 中文提示 + `pause`，不闪退）；
   3. 选解释器：优先 `.venv\Scripts\python.exe`，回退 `D:\Anaconda3\python.exe`，都没有 → 提示建 venv 的命令；
   4. 试运行 `import sys` 与 `import streamlit`（缺 streamlit → 给出安装命令 + 镜像）；
   5. 执行 `python -m streamlit run app.py --server.headless true --browser.gatherUsageStats false`。
3. 浏览器打开 <http://localhost:8501>；
4. **保持命令行窗口打开**，关闭窗口即停止服务。

> **当前状态（2026-10-25 21:28 快照）**：`app.py`（M0-07）已交付，**双击即可启动**，不再出现「未找到 app.py」。
> `app.py` 按 D-9「页面动态发现」注册页面：`ui/pages/` 下每个 `.py` 即一页，**未实现/加载失败的页面自动降级为「建设中」占位页**，不会白屏。当前 **9 个页面文件均已落地并暴露 `render()`**（总览/行情/手动交易/策略/回测/持仓/自动化/设置/日志），各页功能深度不同。
> `runtime/` 已生成（`quantdesk.db` + `-wal`/`-shm`、`logs/`、`backups/`、`exports/`）。启动前先用 §1.2 的 `check_env.py` 确认环境（退出码 0）；页面清单随交付快速变化，以 `ui/pages/` 实际文件与界面为准。

### 2.2 命令行启动（排障用）

```bat
cd /d C:\Users\shanghao\Documents\deepseek-harness\default-workspace\quantdesk
.venv\Scripts\python.exe -m streamlit run app.py --server.headless true --browser.gatherUsageStats false
```

- `--server.headless true`：不自动开浏览器，手动访问 <http://localhost:8501>；
- 换端口：追加 `--server.port 8502`；
- 看完整报错：**不要**用 `pythonw.exe`，用 `python.exe` 在 cmd 里跑，错误会打在窗口里。

### 2.3 首次运行引导（首次启动会发生什么）

> 说明：M5-05 交付后，`start.bat` 会自动化下述步骤（建 venv → 装依赖 → 初始化库 → 启动）；当前阶段需手工执行。

| 步骤 | 操作 | 期望结果 |
|---|---|---|
| 1 | `.venv\Scripts\python.exe scripts\check_env.py` | 依赖表全绿，退出码 0 |
| 2 | `.venv\Scripts\python.exe scripts\init_db.py`（**M0-09 待交付**） | 中文提示"已创建 22 张表"或"已存在，跳过"；出现 `runtime\quantdesk.db` |
| 3 | 双击 `scripts\start.bat` | 窗口不闪退；浏览器出界面；左侧导航 9 项 |
| 4 | 逐页点一遍 | 每页可打开不报错；未开发页显示"建设中"；设置页显示环境/依赖状态 |
| 5 | 看完 `runtime\logs\app-*.log` | 文件存在；每行是 JSON，含时间/级别/消息 |
| 6 | 关闭窗口后再双击 | 热启动 ≤5 秒；不重复建表、不报错 |

> 完整用户验收脚本见 `docs/ROADMAP.md` §2.3（M0）、§3.3（M1）…；可勾选版见 `docs/ACCEPTANCE_CHECKLIST.md`。

### 2.4 第一次使用建议（功能可用后）

1. **设置页**：确认数据源为 `akshare`（无网时改 `mock`）、看一眼当前风控与自动化开关（自动化默认**关闭**）；
2. **行情页**：选 `600519.SH`、区间"近 1 年"、点"刷新数据"，确认 K 线出图；
3. **回测页**：选 `ma_cross`、初始资金 10 万、近 3 年，跑一次，确认 KPI 与权益曲线；
4. **手动交易页**：买 100 股 → 确认风控预检 → 二次确认 → 看持仓与现金变化；
5. 以上 OK 再考虑开启自动化。

---

## 3. 日志、数据与目录位置

### 3.1 运行期目录 `runtime/`

全部运行期产物**只**放在 `quantdesk\runtime`（不在 `data/`，也不在项目根散落）：

```
quantdesk\runtime\
├─ quantdesk.db        # SQLite 主库（行情/订单/持仓/回测/审计，22 张表）
├─ logs\               # 结构化日志（JSON Lines，按日切分，保留 30 天）
│   └─ app-YYYYMMDD.log
├─ backups\            # 每日自动备份（保留最近 7 份）
└─ exports\            # 手工导出的 CSV / HTML 报告
```

> **当前状态（2026-10-25 21:28 快照）**：`runtime` 已生成，包含 `quantdesk.db`（SQLite 主库，WAL 模式，另有 `-wal` / `-shm` 同族文件）、`logs/`、`backups/`、`exports/`。
> `scripts/init_db.py`（M0-09）尚未交付，**重建/重置库**时要么等它交付，要么由应用首次启动自动建库；**不要**手工造空库或单独删除 `-wal`/`-shm`。

### 3.2 日志怎么看

| 用途 | 位置 / 方法 |
|---|---|
| 运行日志（JSON Lines） | `runtime\logs\app-YYYYMMDD.log`，一行一个 JSON：`ts / level / logger / trace_id / module / message / extra` |
| 跟一次完整链路 | 在日志中搜同一个 `trace_id`，可串起"信号 → 风控结论 → 订单 → 成交" |
| 实时看日志（PowerShell） | `Get-Content runtime\logs\app-*.log -Wait -Tail 50` |
| 只看错误 | `Select-String -Path runtime\logs\app-*.log -Pattern '"level":\s*"ERROR"'` |
| 审计流水 | 库表 `audit_logs`（不可删，UI 只读）；覆盖下单、撤单、风控参数变更、策略启停、自动化开关、账户重置、数据源切换、备份恢复 |
| UI 里看 | 日志页（日志 / 审计 / 数据校验 / 通知 4 个 Tab） |

### 3.3 数据源与离线模式

| 操作 | 怎么改 |
|---|---|
| 切离线演示（无网/无 akshare） | `config.yaml` → `data.provider: mock`，`data.fallbacks: [mock]` |
| 切回在线 | `config.yaml` → `data.provider: akshare` |
| 看数据健康 | 日志页 → "数据校验" Tab（DQ001–DQ007 问题清单） |
| 行情过期被风控拦（RK006） | 检查 `data.stale_threshold_hours`（默认 24），重新刷新数据 |

> 离线模式（Mock）是**官方支持的演示路径**：拔网线也能跑通行情、回测、下单、任务、日志全流程（对应 G3 / AC-F1-03 / M4 演示 1）。

---

## 4. 故障排查总表（按现象查）

> 通用三步法：① 用对解释器（`.venv\Scripts\python.exe`）；② 跑 `scripts\check_env.py`；③ 看 `runtime\logs` 最新日志里的 ERROR 与 `trace_id`。

| # | 现象 | 最可能原因 | 处理 |
|---|---|---|---|
| 1 | `pip install` 卡住 / SSL 握手失败 / 超时 | 全局 pip 源指向 NVIDIA 内网源 | 加 `--isolated --index-url https://mirrors.aliyun.com/pypi/simple/`（§1.4 坑 1） |
| 2 | `numpy.dtype size changed` / `pandas requires numpy` / 二进制不兼容 | numpy/pandas 版本打架（Anaconda 与 pip 混装） | 锁 `numpy==1.26.4` + `pandas==2.2.2`，必要时重建 venv（§1.4 坑 2） |
| 3 | `ModuleNotFoundError: bs4 / lxml / openpyxl / tabulate / tqdm / decorator` | akshare 传递依赖缺失 | 一次补齐 6 个包（§1.5） |
| 4 | TA-Lib 装不上 / `DLL load failed while importing _ta_lib` | Windows 缺 C 库，源码编译失败 | **可以不装**，自动 pandas 兜底；要装就换预编译 wheel（§1.6） |
| 5 | 双击 `start.bat` 窗口一闪而过 | 路径不对 / 从资源管理器异常调用 | 从 cmd 进入 `scripts` 目录手动跑 `start.bat` 看完整错误；脚本本身都会 `pause` |
| 6 | 提示「未找到 app.py，无法启动」 | `app.py` 缺失或不在项目根目录 | `app.py`（M0-07）已交付，正常不应出现；用 `dir app.py` 确认文件存在且与 `scripts` 同级，缺失则重新拉取代码 |
| 7 | 提示「当前 Python 环境缺少 streamlit」 | 用错解释器 / 依赖没装齐 | 按提示用 `.venv\Scripts\python.exe -m pip install -r requirements.txt`（带 `--isolated` 与镜像） |
| 8 | `OSError: [Errno 10048]` / 打不开 localhost:8501 | 8501 端口被占用（常见：上次的 streamlit 没关） | `netstat -ano \| findstr :8501` 找 PID → `taskkill /PID <PID> /F`；或换端口 `--server.port 8502` |
| 9 | 页面能开但图表空白 / `ModuleNotFoundError: plotly` | plotly 缺失或版本过低 | `pip install "plotly>=5.24,<8.0"`；确认用的是 venv 解释器 |
| 10 | `sqlite3.OperationalError: database is locked` | 多个进程/窗口同时写库（如两个 streamlit 实例、未退出的定时任务） | 只保留一个实例；确认没有残留 `python.exe`；WAL 模式下短时锁会自动重试，持续报错则重启应用 |
| 11 | `runtime` 目录或 `quantdesk.db` 不存在 | 库文件被删/从未建库；`scripts/init_db.py`（M0-09）尚未交付 | `runtime` 正常情况下会在首次启动时自动生成；不要手工创建空库，也不要单独删 `-wal`/`-shm`。需要重建时先备份（§6） |
| 12 | 拉 A 股行情超时 / 返回空 / 少量标的失败 | akshare 上游限流、改版，或代理/VPN 出口问题 | 先在浏览器确认网络可达；调大 `data.request_interval_ms`；改 `provider: mock` 离线跑通；日志页看 DQ 问题 |
| 13 | 提示"离线演示模式"黄条 | 主源失败已降级到 Mock/缓存 | 属设计内降级（AC-F1-03）；恢复网络后下次任务自动切回 |
| 14 | 所有买入被拒，原因码 RK006 | 行情过期（快照时间超 `stale_threshold_hours`） | 刷新行情数据；或临时放宽该阈值（不推荐） |
| 15 | 所有买入被拒，原因码 RK009 | 当日亏损触发熔断（默认 -3%） | 次日盘前自动重置；或在设置页二次确认后手动解锁（写审计） |
| 16 | 下单被拒 RK007 / RK008 | 单票 20% / 总仓位 80% 上限 | 按提示减少数量；或到设置页调上限（即时生效，写审计） |
| 17 | 下单被拒 RK016 | T+1：当日买入不可卖 | 次日（日初结算后）再卖；持仓页看"可卖数量" |
| 18 | 中文显示乱码（cmd 窗口） | 编码不匹配 | `start.bat` 必须保持 **GBK + CRLF**（脚本内已 `chcp 936`）；不要另存为 UTF-8 |
| 19 | `check_env.py` 退出码 1 但看不出缺什么 | 缺的是"核心依赖"或 Python <3.10 | 看脚本末尾"缺失清单 + 安装命令"段落；确认 `sys.version` ≥ 3.10 |
| 20 | 自动化开着但到点没跑 | 非交易日 / 调度器未启动 / misfire 超时 / 系统休眠 | 看自动化页状态与"下次运行时间"、`job_runs` 表、日志 `SKIPPED` 原因；立即运行方式见 §6.2 |

---

## 5. 停止与重置

### 5.1 停止应用

| 场景 | 操作 |
|---|---|
| 正常停止 | 关闭 `start.bat` 的命令行窗口，或在窗口按 `Ctrl + C` |
| 后台残留进程 | `tasklist \| findstr python` 找到 PID → `taskkill /PID <PID> /F`（**先确认是 QuantDesk 的进程**） |
| 端口占用导致起不来 | 见 §4 第 8 行 |

### 5.2 停止自动化（**重要**）

1. 打开**自动化页** → 总开关切到"停止"（二次确认）；
2. 期望：状态变"已停止"，不再产生新的 `job_runs`；**已提交订单不受影响**；审计里有启停记录；
3. 若界面不可用（无法打开），直接停止应用（§5.1）即可——调度器随进程退出而停止。

> 自动化默认 `automation.enabled: false`（安全默认）；任何情况下都可以用"停整个应用"作为兜底，因为调度器是进程内的。

### 5.3 重置模拟账户

- 入口：设置页"重置账户"（二次确认）→ 清空持仓/订单，**保留审计**；
- 数据库级重置（仅排障用，**会丢数据**）：先按 §7 备份，再删除 `runtime\quantdesk.db`，重新运行初始化脚本；
- 重置后首次启动会重建 22 张表。

---

## 6. 备份与恢复

### 6.1 当前可用方式（手工，立刻可用）

```bat
rem 1) 备份（文件名带时间戳）
cd /d C:\Users\shanghao\Documents\deepseek-harness\default-workspace\quantdesk
if not exist runtime\backups mkdir runtime\backups
copy runtime\quantdesk.db runtime\backups\quantdesk_20261025_2130.db

rem 2) 恢复（先停应用！）
taskkill /PID <QuantDesk 的进程 PID> /F
copy runtime\backups\quantdesk_20261025_2130.db runtime\quantdesk.db
```

> **铁律**：`runtime\quantdesk.db` 是**唯一**的事实来源。备份=复制这个文件；恢复=覆盖它。操作前先停应用（避免 WAL/写入中断）。
> SQLite 是单文件库，`runtime\quantdesk.db-wal`、`-shm` 是同族文件；复制时建议**先停应用**再复制，或确认无写入。

### 6.2 一键备份（`scripts\backup_db.py`，M4-05 已交付）

```bat
cd /d C:\Users\shanghao\Documents\deepseek-harness\default-workspace\quantdesk
rem 备份到 runtime\backups\（自动保留最近 database.backup_keep 份，默认 7）
.venv\Scripts\python.exe scripts\backup_db.py

rem 预演：只打印将生成的备份路径/大小，不写盘（确认参数用）
.venv\Scripts\python.exe scripts\backup_db.py --dry-run

rem 只保留最近 3 份
.venv\Scripts\python.exe scripts\backup_db.py --keep 3
```

行为说明：
- 使用 SQLite **在线备份 API**（WAL 模式下也一致），备份前后都做 `PRAGMA integrity_check`；
- 源库损坏/为空时**拒绝备份**并给中文提示（避免备份坏数据）；
- 输出备份路径、大小、耗时；退出码 0=成功，1=失败。

### 6.3 一键恢复（`scripts\restore_db.py`，M4-05 已交付）

```bat
rem 1) 先看有哪些备份（文件名/大小/时间，从旧到新）
.venv\Scripts\python.exe scripts\restore_db.py --list

rem 2) 恢复指定备份（必须显式 --yes；先停应用与自动化！）
.venv\Scripts\python.exe scripts\restore_db.py runtime\backups\quantdesk-20261025-223616.db --yes
```

安全策略（脚本内建，无需手工做）：
1. **恢复前自动备份当前库**（`quantdesk-before-restore-*.db`）；自动备份失败则中止恢复；
2. 备份文件先校验 `integrity_check` + **22 张核心表存在性**，损坏/缺表的备份**绝不覆盖**当前库；
3. 需要二次确认：命令加 `--yes`，或在交互终端输入 `RESTORE`（非交互环境不加 `--yes` 直接拒绝）；
4. 恢复后再做 `integrity_check` + 22 表校验，并打印账户/订单/成交/日线等关键表行数，便于核对数据一致。

> 演练/测试请始终加 `--db <临时库路径>`（例如 `--db runtime\r6_test.db`），**不要**指向 `runtime\quantdesk.db`。

### 6.4 何时用哪个

| 场景 | 做法 |
|---|---|
| 日常/发版前备份 | §6.2 `backup_db.py` |
| 误删数据、库损坏、升级失败 | 先停应用 → §6.3 `restore_db.py --list` 选备份 → `--yes` 恢复 |
| 恢复后业务数据不对 | 用脚本输出的 `before-restore` 备份回退（同样 `restore_db.py <该备份> --yes`） |
| 没有可用备份 | 只能按 §6.1 手工方式从系统备份/云盘找回；因此请保持每日备份 |

> 在这些脚本交付前，请用 §6.1 的手工方式，效果等价。

---

## 7. 升级与回滚

### 7.1 升级（代码/依赖更新）

```bat
cd /d C:\Users\shanghao\Documents\deepseek-harness\default-workspace\quantdesk
rem 1) 先备份数据
copy runtime\quantdesk.db runtime\backups\quantdesk_before_upgrade.db

rem 2) 更新代码（如果项目来自 git）
git pull

rem 3) 更新依赖（带 --isolated 与镜像）
.venv\Scripts\python.exe -m pip install --isolated --index-url https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt

rem 4) 环境自检 + 启动
.venv\Scripts\python.exe scripts\check_env.py
scripts\start.bat
```

### 7.2 回滚

| 回滚对象 | 做法 |
|---|---|
| 代码 | `git log --oneline` 找到上一个正常提交 → `git checkout <commit>`（或 `git switch -c rollback <commit>`） |
| 依赖 | 回滚到升级前的 `.venv` 快照（升级前把整个 `.venv` 目录复制一份），或按旧 `requirements.txt` 重装 |
| 数据 | 停应用 → 用 `runtime\backups` 里升级前的备份覆盖 `runtime\quantdesk.db`（§6.1） |
| 数据库结构不兼容 | 恢复旧库 + 回滚代码版本；**不要**手工改表结构（迁移由 `core/db.py` 负责） |

> 升级前**一定**先做 §7.1 第 1 步的备份；数据库结构变更（迁移）后的旧库不保证能被新代码直接读。

### 7.3 发版自检（`scripts\release_check.py`，M4-06 已交付）

发版/交付前跑一次，正常状态应输出 **`[PASS]`** 且退出码 0：

```bat
cd /d C:\Users\shanghao\Documents\deepseek-harness\default-workspace\quantdesk
rem 完整自检（含 pytest 全量测试，约 1~2 分钟）
.venv\Scripts\python.exe scripts\release_check.py

rem 快速自检（跳过 pytest，排查环境用）
.venv\Scripts\python.exe scripts\release_check.py --skip-tests

rem 指定数据库（演练/测试库）
.venv\Scripts\python.exe scripts\release_check.py --db runtime\r6_test.db --skip-tests
```

检查项：Python 版本、关键/可选依赖、配置加载、数据库文件+完整性+22 张核心表、`daily_bars`
行数与 source 唯一性（mock/真实数据混存会 FAIL）、数据源可用性（akshare 不可用则自动降级
mock，降级不算失败）、pytest 全绿、文档/许可文件存在性。

注意事项：
- 在 pytest 用例内部调用本脚本时，会检测到 `PYTEST_CURRENT_TEST` 并**自动跳过嵌套测试**
  （打印"在 pytest 内运行，跳过嵌套测试（防递归）"），避免"测试套件里再跑测试套件"卡死；
- 可选依赖缺失（如 ccxt）与可选文档缺失（LICENSE/USER_GUIDE）只做备注、不阻塞发版；
- 有 `[FAIL]` 时退出码为 1，修完再发版。

---

## 8. 责任边界与求助

| 情况 | 找谁 / 怎么办 |
|---|---|
| 环境装不上、启动报错 | 先按 §4 排查；仍不行收集：`check_env.py` 完整输出 + 报错截图 + `runtime\logs` 对应时间段日志 |
| 数据拿不到 / 数据质量异常 | 看日志页"数据校验"；必要时切 `provider: mock` 保证流程可演示 |
| 想改配置/风控参数 | 设置页改（即时生效）；不要手改数据库 |
| 契约/接口层面的问题 | 以 `docs/SPEC.md` 为准提缺陷，不要在文档里"就地改口径"（见 `docs/DOC_INDEX.md` §2 变更规则） |
| 文档与实际不一致 | 按 `SPEC.md` 提缺陷修复；同步更新 `docs/DOC_INDEX.md` 的文档状态 |

---

## 9. 数据完整性：mock 污染清理与数据回填（P0 修复，2026-10-25）

> 背景：历史版本在 akshare 失败时会用 Mock 假数据兜底并写入 `daily_bars`，与真实行情混存。
> 回测跨区间会拼出**虚假收益**（实测出现过 246% 假收益）。当前代码已从根源禁止 mock 入库：
> `BarCache.update` 拦截 mock 行（`UpdateReport.mock_blocked`）；`BarCache.load` 对混源数据 fail-closed 抛错。

### 9.1 判断是否被污染

```powershell
cd quantdesk
.venv\Scripts\python.exe scripts\purge_mock_cache.py --dry-run
# 或直接查库（只读）：
#   SELECT source, COUNT(*) FROM daily_bars GROUP BY source;
#   SELECT source, COUNT(*) FROM adjust_events GROUP BY source;
```

`--dry-run` 行为：**只读统计、绝不修改数据库**（即使存在待清理行）。输出形如：

```
数据库        : ...\runtime\quantdesk.db
daily_bars    : 待清理 0 行（source 含 mock）
adjust_events : 待清理 5 行（source 含 mock）
--dry-run：未修改数据库
```

### 9.2 一键清理（幂等，可重复执行）

```powershell
.venv\Scripts\python.exe scripts\purge_mock_cache.py
```

清理 `daily_bars` / `adjust_events` 中 `source` 含 `mock` 的全部行；清理前建议先备份数据库（`runtime/backups/`，见 §6）。

**输出格式（固定三段）**：

1. 数据库路径 + 每张表「待清理 N 行」统计；
2. 「已删除 daily_bars X 行、adjust_events Y 行」；
3. 清理后 `daily_bars` 数据源分布（`- akshare: 3636 行` 这种逐 source 列表）。

**幂等与退出码**：连续执行第二次会输出「未发现 mock 污染数据（幂等，无需处理）」；
退出码 `0`=成功/无需清理、`1`=数据库错误（损坏/占用/不可写）、`2`=配置错误。
`source` 匹配为大小写不敏感的子串匹配（如 `mock`、`mock/xxx` 都会被清理）。

### 9.3 清理后回填真实数据

```powershell
# 按数据库自选池回填 config.data.history_years 年（或显式 --years 5），只允许真实数据入库
.venv\Scripts\python.exe scripts\fetch_data.py --watchlist --years 5
# 校验 + 数据覆盖报告（每标的首日/末日/行数/缺口/来源）：
#   runtime/exports/data_coverage.md
```

要求：`validate()` 无 DQ 问题、各标的起始日一致、覆盖报告无「混源/mock/缺口」警告。
若数据源不可用，脚本会降级为离线 Mock 但**不会写库**（`UpdateReport.mock_blocked > 0`），此时请检查网络后重跑。

### 9.4 验证回测只使用真实数据

```powershell
# load() 遇到同一标的混存多来源会直接抛中文错误（fail-closed），不会返回拼接数据
.venv\Scripts\python.exe -c "from data.cache import BarCache; from datetime import date; print(BarCache().load(['600519.SH'], date(2024,1,1), date(2025,12,31)).shape)"
```

---

## 附录 A：本机实测环境基线（2026-10-25）

| 组件 | 版本 | 是否核心 |
|---|---|---|
| Python（venv） | 3.12.7（由 `D:\Anaconda3\python.exe -m venv .venv` 创建，`include-system-site-packages = false`） | 必需（≥3.10） |
| numpy | **1.26.4**（锁定） | 核心 |
| pandas | **2.2.2**（锁定） | 核心 |
| pyarrow | **16.1.0**（锁定） | 核心（Streamlit `st.dataframe` 的 Arrow 序列化依赖；**与 numpy 1.26.4 兼容**，升级 numpy/pandas 前必须复核） |
| streamlit | 1.37.1 | 核心 |
| plotly | 5.24.1（`requirements.txt` 区间 `<8.0`） | 核心 |
| SQLAlchemy | 2.1.6 | 核心 |
| pydantic | 2.14.1 | 核心 |
| PyYAML | 6.0.3 | 核心 |
| APScheduler | 3.11.3 | 核心 |
| akshare | 1.19.3 | 核心 |
| requests | 2.34.2 | 核心（akshare/Webhook） |
| TA-Lib | 0.8.2 | 可选（缺失走 pandas 兜底） |
| beautifulsoup4 / lxml | 4.15.0 / 6.1.3 | akshare 传递依赖 |
| openpyxl / tabulate / tqdm / decorator | 3.1.5 / 0.10.0 / 4.70.1 / 5.3.1 | akshare 传递依赖 |
| pytest | 9.1.1 | 开发/验收 |

> 版本最终锁定以任务 **M4-08** 写入 `requirements.txt` 的锁定段为准；本表用于"出故障时对照"。
>
> **pyarrow 说明（2026-10-25 补充）**：Streamlit 的 `st.dataframe`／Arrow 序列化依赖 pyarrow，本机锁定 **`pyarrow==16.1.0`**（与 `numpy==1.26.4` 兼容）。若出现 `pyarrow` 相关的 `ImportError` 或 `Arrow` 报错，先核对版本：`.venv\Scripts\python.exe -c "import pyarrow; print(pyarrow.__version__)"` 应为 `16.1.0`；升级 numpy/pandas 前必须复核 pyarrow 兼容性，避免二进制 ABI 冲突（见 §1.4 坑 2）。

---

## 附录 B：常用命令速查

```bat
rem —— 环境 ——
cd /d C:\Users\shanghao\Documents\deepseek-harness\default-workspace\quantdesk
.venv\Scripts\python.exe scripts\check_env.py                      :: 环境自检（0=齐备/1=缺失）
.venv\Scripts\python.exe -m pip check                              :: 依赖一致性检查
.venv\Scripts\python.exe -m pip config list -v                     :: 看全局 pip 源（NVIDIA 源排查）
.venv\Scripts\python.exe -m pip install --isolated --index-url https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt
.venv\Scripts\python.exe -c "import numpy,pandas;print(numpy.__version__,pandas.__version__)"

rem —— 启动 / 停止 ——
scripts\start.bat
.venv\Scripts\python.exe -m streamlit run app.py --server.headless true --browser.gatherUsageStats false
netstat -ano | findstr :8501
taskkill /PID <PID> /F

rem —— 数据与日志 ——
dir runtime
dir runtime\logs
Get-Content runtime\logs\app-*.log -Wait -Tail 50           :: PowerShell 实时跟踪
Select-String -Path runtime\logs\app-*.log -Pattern '"level":"ERROR"'

rem —— 备份 / 恢复 / 发版自检 ——
.venv\Scripts\python.exe scripts\backup_db.py                     :: 在线备份（保留最近 backup_keep 份）
.venv\Scripts\python.exe scripts\backup_db.py --dry-run           :: 预演，不写盘
.venv\Scripts\python.exe scripts\restore_db.py --list             :: 查看备份
.venv\Scripts\python.exe scripts\restore_db.py <备份文件> --yes    :: 恢复（恢复前自动备份当前库）
.venv\Scripts\python.exe scripts\release_check.py                 :: 发版自检（含 pytest，全 PASS 才发版）
.venv\Scripts\python.exe scripts\release_check.py --skip-tests    :: 快速自检

rem —— 测试 ——
.venv\Scripts\python.exe -m pytest -q
```

---

## 变更记录

| 版本 | 日期 | 变更 | 变更人 |
|---|---|---|---|
| v1.3 | 2026-10-26 | M4-05/M4-06 落地：§0 速查修正章节号并新增"发版自检"；§6.2/6.3/6.4 补 `scripts\backup_db.py`（在线备份/完整性校验/保留策略/--dry-run）与 `scripts\restore_db.py`（恢复前自动备份/22 表校验/--yes 二次确认）用法；§7.3 新增 `scripts\release_check.py` 发版自检（含防递归说明）；附录 B 命令速查同步 | Dev3（dev-trading） |
| v1.2 | 2026-10-25 | 二次校正（21:28 快照）：9 页全部落地 → §2.1 状态说明改为"9 页已落地、未实现区块为建设中"；`runtime` 已生成 → §3.1 与 §4 第 11 行改为"正常会自动生成/勿手造空库/勿单删 WAL 同族文件" | 交付文档维护（eval-data） |
| v1.1 | 2026-10-25 | 校正交付状态：`app.py`（M0-07）与 6 个页面已交付 → §2.1 改为"双击即可启动、未交付页为建设中占位"；§3.1 与 §4 第 6/11 行同步修正（`runtime` 待 `init_db.py` 交付后生成） | 交付文档维护（eval-data） |
| v1.0 | 2026-10-25 | 初版：环境准备（含 venv 路径/依赖安装/4 个实际踩坑）、一键启动、首次运行引导、20 条故障排查总表、日志与数据目录、停止与重置、备份恢复、升级回滚、实测版本基线、命令速查 | 交付文档维护（eval-data） |
