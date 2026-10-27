# QuantDesk v2.0.0 发版验证报告

| 项目 | 内容 |
| --- | --- |
| 产品/版本 | QuantDesk **v2.0.0**（`config.yaml` → `app.version`） |
| 验证日期 | 2026-10-27（Asia/Shanghai） |
| 验证人 | verify-engineer（DSH 团队） |
| 验证方式 | 本机可复现全链路验证（无 CI，本机结果即证据） |
| **最终结论** | **通过（PASS）** —— 6/6 验证项 PASS，全量测试 **405 passed / 0 failed**，界面 HTTP **200**，M6 冒烟 **4/4 配置成功** |

---

## 0. 结论摘要

- **全量测试**：`405 passed, 60 deselected`（0 failed / 0 error），独立运行 + 脚本内运行共 5 次结果一致。
- **环境自检**：核心依赖 9/9 齐备，退出码 0。
- **发版自检**（`--skip-tests`）：`PASS 10 / WARN 1 / FAIL 0`，退出码 0；唯一 WARN 是「按设计跳过嵌套测试」，全量测试已单独执行。
- **数据体检**：`结论 PASS · ERROR 0 / WARN 0 / INFO 10`，3 只标的真实日线、来源唯一（akshare）、零 mock。
- **界面可跑通**：Streamlit 后台启动 → `http://127.0.0.1:<port>/` 返回 **HTTP 200**、HTML 含 `streamlit` 标记、`/_stcore/health=ok`；启动 1.9s（自动端口）/ 4.2s（手工 8611 端口），验证后进程已清理。
- **M6 研究链路**：`scripts/m6_research.py --quick` 4/4 组配置成功，无异常，内部耗时 110.1~115.9s。
- **一键脚本**：`scripts/verify_all.py --with-m6` 实跑 **PASS 6 / FAIL 0**，退出码 0，总耗时 186.6s；负向测试（端口被占用）正确判 FAIL 并以退出码 1 结束。
- **真实缺陷**：**未发现**。未修改任何代码逻辑与既有测试；仅新增 `scripts/verify_all.py` 与本报告。

---

## 1. 验证环境

| 项目 | 值 |
| --- | --- |
| 操作系统 | Windows 10（`10.0.19045`，AMD64） |
| Python | **3.12.7**（CPython，项目内 `.venv`） |
| 解释器 | `quantdesk\.venv\Scripts\python.exe` |
| 运行版本 | 项目版本 QuantDesk **v2.0.0**（`config.yaml` → `app.version`；根目录 `VERSION` 文件内容 `2.0.0`） |
| 发布物料 | `VERSION`、`CHANGELOG.md`、`README.md`、`docs/RELEASE_NOTES_v2.0.0.md`、`docs/M6_INTEGRATION.md`、`docs/DOC_INDEX.md` 已就绪（发版物料定稿后复跑 `verify_all.py --skip-tests`：PASS 4 / SKIP 2，release_check FAIL 0） |
| 关键依赖 | streamlit 1.37.1、pandas 2.2.2、numpy 2.5.4、plotly 5.24.1、SQLAlchemy 2.1.6、pydantic 2.14.1、PyYAML 6.0.3、APScheduler 3.11.3、akshare 1.19.3、TA-Lib 0.8.2、pytest 9.1.1 |
| 可选依赖 | ccxt 未安装（可选，不阻塞 A 股流程） |
| 数据源 | `provider=akshare`，健康检查 OK；`universe=3` 只 |
| 数据库 | `runtime/quantdesk.db`（3,846,144 字节，journal_mode=wal，integrity_check=ok） |
| 行情数据 | `daily_bars` **3,639 行 / 3 只标的 / 2021-10-25 ~ 2026-10-26**，来源唯一（akshare），零 mock |
| 测试口径 | `pytest.ini` 默认快速套件：`addopts = -m "not slow"` → 405 选中、60 个 slow 用例取消选择 |
| 网络 | 验证过程**不依赖外部网络**（界面冒烟仅访问 127.0.0.1 回环） |

> 沙箱注意：DSH 沙箱下普通 `pytest` 会因 `os.mkdir(0o700)` 临时目录权限产生约 33 个环境性 `PermissionError`（与项目代码无关）。
> 本报告统一使用 `runtime\_run_pytest.py` 包装器（仅把临时目录 mode 改为 0777，不改仓库代码/测试/ACL）。
> 用户本机无此限制时，直接 `.venv\Scripts\python.exe -m pytest tests -q` 即可，结果一致。

---

## 2. 结果总览

| # | 验证项 | 命令 | 退出码 | 关键结果 | 耗时 | 判定 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 全量测试 | `.venv\Scripts\python.exe runtime\_run_pytest.py tests -q` | 0 | **405 passed, 60 deselected**，0 failed | 58.7s（独立）/ 61.2s（脚本内） | **PASS** |
| 2 | 环境自检 | `.venv\Scripts\python.exe scripts\check_env.py` | 0 | 核心依赖 9/9 齐备 | 4.4s（独立）/ 3.7s（脚本内） | **PASS** |
| 3 | 发版自检 | `.venv\Scripts\python.exe scripts\release_check.py --skip-tests` | 0 | PASS 10 / WARN 1 / **FAIL 0** | 5.0s（独立）/ 4.0s（脚本内） | **PASS** |
| 4 | 数据体检 | `.venv\Scripts\python.exe scripts\verify_data.py` | 0 | 结论 PASS；ERROR 0 / WARN 0 / INFO 10 | 0.3s（独立）/ 0.2s（脚本内） | **PASS** |
| 5 | 界面 HTTP 冒烟 | `python -m streamlit run app.py --server.headless true` + 轮询 | 0 | **HTTP 200**、`title=Streamlit`、`health=ok` | 4.2s（手工 8611）/ 1.9s（脚本自动端口） | **PASS** |
| 6 | M6 研究链路冒烟 | `.venv\Scripts\python.exe scripts\m6_research.py --quick` | 0 | **4/4 配置成功**，无异常 | 121.2s（独立）/ 114.6s（脚本内） | **PASS** |
| 7 | 一键验证脚本 | `.venv\Scripts\python.exe scripts\verify_all.py --with-m6` | 0 | **PASS 6 / FAIL 0 / SKIP 0** | 186.6s | **PASS** |

> 「独立」= 单独手工执行；「脚本内」= `scripts/verify_all.py` 串跑时的单步耗时。
> 一键脚本全量模式共实跑 2 次（含 `--with-m6`）：184.1s / 186.6s，均 PASS 6 / FAIL 0，结果稳定。
> 发版物料（VERSION=2.0.0、CHANGELOG、README、RELEASE_NOTES、DOC_INDEX）定稿后，再复跑 `verify_all.py --skip-tests`：PASS 4 / SKIP 2（10.6s），`release_check` 仍为 FAIL 0。
> 全部文件定稿（含本报告）后再复跑完整模式（不含 M6）：**PASS 5 / FAIL 0 / SKIP 1，66.5s，退出码 0**（pytest 405 passed / 60 deselected in 53.77s）。

---

## 3. 逐项证据

### 3.1 全量测试（405 passed）

```
命令  : .venv\Scripts\python.exe runtime\_run_pytest.py tests -q
退出码: 0
输出  : 405 passed, 60 deselected in 58.71s
日志  : runtime/verify_v2/pytest_full.out.log
        及 runtime/verify_v2/20261027_195958/tests.log
```

- 独立运行与脚本内运行共 5 次，均 **405 passed / 60 deselected / 0 failed**（58.71s、56.63s、54.53s、59.07s、53.77s，机器负载不同略有波动）。
- 60 个 deselected 为 `pytest.ini` 中 `-m "not slow"` 的既有口径（AppTest 渲染 / 子进程脚本 / 大回测等慢用例），与基线 405/60 完全一致。
- 沙箱下的 33 个环境性 error 已被 `runtime/_run_pytest.py` 包装器规避（wrapper 只调整临时目录 mode，不改代码/测试）。

### 3.2 环境自检（check_env）

```
命令  : .venv\Scripts\python.exe scripts\check_env.py
退出码: 0          耗时: 4.4s
关键  : [1] Python 3.12.7 / 项目内 .venv
        [2] 核心依赖 9/9 [正常]（streamlit/pandas/plotly/SQLAlchemy/pydantic/PyYAML/APScheduler/akshare/numpy）
        [3] pytest 9.1.1、TA-Lib 0.8.2 [正常]；ccxt [未装]（可选）
        [4] 核心依赖：9/9 齐备，环境可用
日志  : runtime/verify_v2/check_env.out.log
```

### 3.3 发版自检（release_check --skip-tests）

```
命令  : .venv\Scripts\python.exe scripts\release_check.py --skip-tests
退出码: 0          耗时: 5.0s
关键  : [PASS] Python 版本 3.12.7
        [PASS] 关键依赖 / 可选依赖
        [PASS] 配置加载：QuantDesk v2.0.0，provider=akshare，universe=3 只
        [PASS] 数据库完整性：integrity_check=OK（3,846,144 字节）
        [PASS] 数据库表：22 张核心表齐全（库内共 29 张表）
        [PASS] daily_bars 数据：3,639 行；来源唯一：akshare 3,639 行
        [PASS] 数据源：akshare（状态 OK）：真实 A 股行情
        [WARN] 测试全绿：已按 --skip-tests 跳过（正式发版请勿跳过）
        [PASS] 文档/配置文件 + 可选文档（LICENSE / USER_GUIDE / ACCEPTANCE_CHECKLIST 均就绪）
        汇总：PASS 10 / WARN 1 / FAIL 0
        结论：[通过] 发版自检通过
日志  : runtime/verify_v2/20261027_195958/release_check.log
```

- 唯一 WARN 为 `--skip-tests` 的设计行为（避免「测试套件里嵌套跑测试套件」递归），对应测试已由 §3.1 独立执行并通过。
- 含测试的完整 `release_check.py`（不带 `--skip-tests`）会再串跑一遍 pytest；本报告选择 `--skip-tests` + 独立全量测试的组合，覆盖等价且更清晰。

### 3.4 数据体检（verify_data）

```
命令  : .venv\Scripts\python.exe scripts\verify_data.py
退出码: 0          耗时: 0.3s
关键  : C1 数据库完整性 OK / WAL；C2 daily_bars 3,639 行 / 3 只标的 / 2021-10-25~2026-10-26
        C3 来源唯一（每标的一个 source，全库 akshare）；C4 交易日历 0 缺口
        C5 复权因子无空值；C6 停牌标记一致；C7 DQ 记录为空；C8/C9 元数据与日历覆盖一致
        结论：PASS · ERROR 0 个 / WARN 0 个 / INFO 10 个
日志  : runtime/verify_v2/20261027_195958/verify_data.log
```

### 3.5 界面可跑通（Streamlit HTTP 冒烟）

**手工冒烟（固定端口 8611）**

```
命令  : .venv\Scripts\python.exe -m streamlit run app.py --server.headless true \
        --server.port 8611 --server.fileWatcherType none --browser.gatherUsageStats false
退出码: 进程按预期被结束（验证后 Stop-Process，无后台残留）
结果  : HTTP 200（4.19s 内就绪），HTML 891 字节，<title>Streamlit</title>，含 streamlit 标记
        GET http://127.0.0.1:8611/_stcore/health → 200 "ok"
证据  : runtime/verify_v2/streamlit_8611.out.log / streamlit_8611.err.log
        runtime/verify_v2/streamlit_8611.index.html
```

**脚本自动冒烟（`verify_all.py`，自动选空闲端口）**

```
轮询  : http://127.0.0.1:58192/ → HTTP 200，标题「Streamlit」，891 字节，启动 1.94s，health=ok
清理  : 验证后进程 terminate 并确认退出（无残留监听端口 / 无 python 进程）
日志  : runtime/verify_v2/20261027_195958/streamlit.log
        runtime/verify_v2/20261027_195958/streamlit_index.html
```

> 说明：HTTP 200 + 静态首页 + `_stcore/health=ok` 证明「服务能启动、端口能监听、页面资源可返回」；
> 页面内的行情图表/交互需浏览器执行 JS 后渲染，不在本项（HTTP 层冒烟）覆盖范围内。

### 3.6 M6 研究链路冒烟（4/4 配置成功）

```
命令  : .venv\Scripts\python.exe scripts\m6_research.py --quick
退出码: 0          耗时: 121.2s（脚本内部 115.9s）
关键  : [冒烟] 强制使用 runtime/short_term/bars_50.csv
        [数据] bars (89000, 10)，标的 50，区间 2019-01-02 ~ 2026-10-23，指数 有
        [面板] (82695, 93)，构建 3.0s，市场状态来源=沪深300，价格口径=adjclose
        [配置] 4 组 → ['ret3_ge2|v1|logistic', 'ret3_ge2|v1|gbdt', 'ret3_ge2|all|logistic', 'ret3_ge2|all|gbdt']
        [OK 1/4] ret3_ge2 v1  logistic folds=76 hit=0.38373 (11.0s)
        [OK 2/4] ret3_ge2 v1  gbdt     folds=76 hit=0.368519 (39.9s)
        [OK 3/4] ret3_ge2 all logistic folds=76 hit=0.384656 (11.2s)
        [OK 4/4] ret3_ge2 all gbdt     folds=76 hit=0.369444 (50.4s)
        [完成] 4/4 组成功，耗时 115.9s
产出  : runtime/m6/quick_smoke/m6_matrix.csv（4 行 OOS 指标）、m6_experiments.json
日志  : runtime/verify_v2/20261027_195958/m6_quick.log（`verify_all.py --with-m6` 自动采集口径，该次内部 110.1s；独立运行同命令 115.9s，输出一致）
```

> 冒烟配置下 4 组均 `gate=False`（如 `gate_failed=rank_ic_gt_0.03`）属**预期研究结论**（严格门槛未过），不是运行缺陷；M6 的结论性数据见 `docs/M6_RESEARCH_REPORT.md`。

### 3.7 一键验证脚本 `scripts/verify_all.py`（本次新增，唯一改动代码文件）

- 路径：`scripts/verify_all.py`
- 作用：一条命令串起「全量测试 + check_env + release_check --skip-tests + verify_data + Streamlit HTTP 冒烟」，可选追加 M6 冒烟；逐项打印 PASS/FAIL 与耗时，**任一失败退出码 1**。
- 实现要点：
  - 仅标准库；无外部网络（界面冒烟只访问 127.0.0.1）；Windows/macOS/Linux 通用；
  - pytest 优先调用 `runtime/_run_pytest.py`（存在时），否则 `-m pytest`；
  - 子进程输出重定向到日志文件（不经管道），兼容 DSH 沙箱；
  - 界面冒烟自动申请空闲端口、轮询 HTTP、超时/结束必定清理进程；
  - 日志与 summary（`summary.json` / `summary.txt`）写入 `runtime/verify_v2/<时间戳>/`。
- 支持参数：`--skip-tests`、`--skip-ui`（快速跑）、`--with-m6`、`--ui-port`、`--ui-timeout`、`--tests-timeout`、`--m6-timeout`。

**完整模式实跑（最终版本，含 M6）**

```
命令  : .venv\Scripts\python.exe scripts\verify_all.py --with-m6
退出码: 0        总耗时: 186.6s
[1/6] 全量测试        PASS（61.2s）405 passed, 60 deselected in 59.07s
[2/6] 环境自检        PASS（ 3.7s）退出码 0；核心依赖 9/9
[3/6] 发版自检        PASS（ 4.0s）汇总：PASS 10 / WARN 1 / FAIL 0
[4/6] 数据体检        PASS（ 0.2s）结论 PASS；ERROR 0 个 / WARN 0 个 / INFO 10 个
[5/6] 界面 HTTP 冒烟  PASS（ 1.9s）HTTP 200，标题「Streamlit」，891 字节，启动 1.94s，health=ok
[6/6] M6 研究冒烟     PASS（114.6s）4/4 组成功，内部耗时 110.1s
汇总: PASS 6 / FAIL 0 / SKIP 0   结论: [通过] 全部验证项通过
证据: runtime/verify_v2/20261027_195958/（summary.json、summary.txt + 各项日志）
```

**退出码语义验证（负向测试）**：用已占用端口 55814（本机 GUI 端口）强制界面冒烟失败：

```
命令  : .venv\Scripts\python.exe scripts\verify_all.py --skip-tests --ui-port 55814 --ui-timeout 20
结果  : [5/6] 界面 HTTP 冒烟 …… FAIL（20.1s）
        汇总：PASS 3 / FAIL 1 / SKIP 2
        退出码：1  ✅（失败时退出码非 0，符合发版门槛要求）
```

---

## 4. 失败与修复

**本次验证未发现产品缺陷**，全部验证项一次通过。过程中遇到的两项均为**环境/工具层面**问题，已在验证脚本内规避，未改动任何业务代码、既有测试或 ACL：

| 问题 | 影响 | 处理 |
| --- | --- | --- |
| DSH 沙箱下 pytest 的 `tmp_path` 0700 目录导致约 33 个环境性 `PermissionError` | 普通 `pytest` 无法全绿（与项目无关） | 使用既有 `runtime/_run_pytest.py` 包装器（只把临时目录 mode 改为 0777）；`verify_all.py` 自动优先使用它 |
| PowerShell `Start-Process -RedirectStandardOutput` 后 `ExitCode` 偶发为空（PS 5.1 行为） | 仅影响手工采集退出码的方式 | 验证脚本统一用 Python `subprocess` 判定退出码；手工复核改用直接调用 + `$LASTEXITCODE` |

> 说明：`release_check --skip-tests` 的 `[WARN] 测试全绿` 是脚本设计行为（防递归），非缺陷；已由独立全量测试补齐证据。

---

## 5. 残余风险与建议

1. **slow 用例未纳入本次全量数字**：默认口径 405 passed 排除了 60 个 slow 用例（`pytest.ini` 既有策略）。发版前如需完整套件，可执行 `.venv\Scripts\python.exe runtime\_run_pytest.py tests -q -m "slow or not slow"`（预计更长；本报告未执行，故 405/60 与基线一致）。
2. **界面验证为 HTTP 层冒烟**：证明服务启动 + 端口监听 + 首页/health 可返回；不覆盖浏览器端 JS 渲染与交互（如需可加 `tests/test_ui_*.py` 的 AppTest，属 slow 口径）。
3. **数据为本地快照**：验证使用本机 `runtime/quantdesk.db`（3 只标的、akshare 真实日线）。未验证联网重新拉取（本机/CI 无网络时不可复现）；重拉流程见 `docs/DATA_REPRODUCIBILITY.md`。
4. **仓库无 CI**：本机验证结果即证据，建议按 §6 一键复跑。发版窗口内 `release-engineer` 并行落地发布物料（`VERSION`/`CHANGELOG.md`/`README.md`/`docs/RELEASE_NOTES_v2.0.0.md`/`docs/M6_INTEGRATION.md`/`docs/DOC_INDEX.md`），物料定稿后已复跑 `verify_all.py --skip-tests`：**PASS**（release_check FAIL 0）。注：`docs/DOC_INDEX.md` 若需收录本验证报告，由发布物料维护方补一行索引即可（本报告写权限外）。

---

## 6. 如何复现（照抄即可）

所有命令在**项目根目录** `quantdesk\` 下执行。

```bat
:: 0) 进入项目并确认解释器
cd C:\Users\shanghao\Documents\deepseek-harness\default-workspace\quantdesk
.venv\Scripts\python.exe -V

:: 1) 一键全链路验证（推荐；约 1 分半，界面冒烟自动选端口并清理进程）
.venv\Scripts\python.exe scripts\verify_all.py

:: 1b) 含 M6 研究链路冒烟（约 3 分钟）
.venv\Scripts\python.exe scripts\verify_all.py --with-m6

:: 1c) 快速冒烟（跳过全量测试与界面，约 10 秒）
.venv\Scripts\python.exe scripts\verify_all.py --skip-tests --skip-ui

:: 2) 逐项手工执行（等价于脚本内部步骤）
.venv\Scripts\python.exe runtime\_run_pytest.py tests -q        :: 期望 405 passed, 60 deselected
.venv\Scripts\python.exe scripts\check_env.py                  :: 期望退出码 0
.venv\Scripts\python.exe scripts\release_check.py --skip-tests :: 期望 FAIL 0
.venv\Scripts\python.exe scripts\verify_data.py                :: 期望 结论 PASS

:: 3) 界面手工冒烟（另开一个终端；验证完 Ctrl+C 或 kill 进程）
.venv\Scripts\python.exe -m streamlit run app.py --server.headless true --server.port 8611 --server.fileWatcherType none

:: 4) M6 研究链路冒烟（约 2 分钟）
.venv\Scripts\python.exe scripts\m6_research.py --quick
```

> 用户本机（非 DSH 沙箱）可直接 `pytest tests -q`（把第 2 步的 wrapper 换掉即可），预期同样是 405 passed / 60 deselected。
> 每次 `verify_all.py` 运行都会在 `runtime/verify_v2/<时间戳>/` 留下 `summary.json`、`summary.txt` 与逐项日志，便于归档与审计。

---

## 7. 证据文件清单

| 文件 | 内容 |
| --- | --- |
| `runtime/verify_v2/20261027_195958/` | 最终 `verify_all.py --with-m6` 全量运行证据（summary.json/txt + 6 项日志 + 界面 HTML 快照） |
| `runtime/verify_v2/20261027_200350/` | 发版物料定稿后 `verify_all.py --skip-tests` 复跑证据（PASS 4 / SKIP 2，release_check FAIL 0） |
| `runtime/verify_v2/20261027_200417/` | 全部文件定稿后完整模式复跑证据（PASS 5 / FAIL 0 / SKIP 1，66.5s） |
| `runtime/verify_v2/20261027_195615/` | 上一次含 M6 全量运行（184.1s，PASS 6/0）用于稳定性对照 |
| `runtime/verify_v2/20261027_195544/` | 负向测试（占用端口 → FAIL，退出码 1） |
| `runtime/verify_v2/pytest_full.out.log` | 独立全量测试原始输出（405 passed, 60 deselected in 58.71s） |
| `runtime/verify_v2/check_env.out.log` | 独立 check_env 原始输出（9/9 齐备） |
| `runtime/verify_v2/streamlit_8611.index.html` | 手工 8611 端口首页 HTML（HTTP 200 证据） |
| `runtime/m6/quick_smoke/m6_matrix.csv`、`m6_experiments.json` | M6 冒烟 4 组配置的 OOS 指标与运行记录 |
| `scripts/verify_all.py` | 一键验证脚本（本次唯一新增代码文件） |
| `docs/RELEASE_VERIFICATION_v2.0.0.md` | 本报告 |

---

## 8. Lead 发布前最终门禁（全部发版文件定稿后复跑）

| # | 项 | 命令 | 结果 |
| --- | --- | --- | --- |
| L1 | 一键全链路（含 M6） | `.venv\Scripts\python.exe scripts\verify_all.py --with-m6` | **PASS 6 / FAIL 0 / SKIP 0**，180.9s，退出码 0（2026-10-27 20:06，全部发版文件定稿后） |
| L2 | 发版自检（完整模式，含嵌套 pytest） | `.venv\Scripts\python.exe scripts\release_check.py` | **PASS 11 / WARN 0 / FAIL 0**，退出码 0；其中 `pytest -q` = **405 passed, 60 deselected**（54.23s） |
| L3 | 发布物料静态一致性 | `.venv\Scripts\python.exe runtime\_release_static_check.py` | **PASS**：版本链（VERSION/config/core/UI）一致、7 份文档相对链接全部可解析、无肯定性收益宣称、物料齐全、README 徽章正确 |

> 沙箱说明（仅本机 DSH 环境，不随仓库分发）：`release_check.py` 的嵌套 pytest 在沙箱下需要
> `PYTHONPATH=runtime/_sandbox_site`（该目录内的 `sitecustomize.py` 只把 pytest 临时目录 mode 改为 0777，
> 不改仓库代码/测试/ACL）；用户本机（非沙箱）直接运行 `python scripts\release_check.py` 即可得到同样结果。
> `verify_all.py` 内置了同样的沙箱兼容（自动优先 `runtime/_run_pytest.py`）。

---

*报告生成：2026-10-27 · QuantDesk v2.0.0 · verify-engineer（§8 为 Lead 发布前复跑补充）*

---

## 9. 发布后干净源码复核（v2.0.0 → v2.0.1）

> 目的：验证从 GitHub 下载的**发布 tarball 在干净目录**（无 `runtime/`、无本地数据库）中能否跑通全量测试，并记录 v2.0.0 → v2.0.1 的修复闭环。
> 背景：`runtime/` 按 `.gitignore` 不入库，部分测试依赖 `runtime/short_term/bars_*.csv` 等**运行期数据**，干净克隆下这些用例应 skip 而非 error。
> 本节为 v2.0.0 发布后的补充复核记录（追加于 v2.0.1 补丁发布），不改变 v2.0.0 的验证结论。

### 9.1 v2.0.0 tarball（干净目录，未构建数据）

| 项 | 结果 |
| --- | --- |
| 操作 | 从 GitHub 下载 v2.0.0 发布 tarball，解压到干净目录后运行全量测试 |
| 结果 | **383 passed / 17 skipped / 5 errors** |
| errors 位置 | **全部**在 `tests/test_backtest_passive.py` |
| 原因 | 该文件 module 级 fixture `real_bars` 对运行期数据 `runtime/short_term/bars_50.csv` 使用 `assert`；干净克隆缺该文件（`runtime/` 不入库）→ fixture 抛 `AssertionError` → 5 个用例 **error** 而非 skip |
| 判定 | **测试健壮性缺陷**：不影响已构建数据的本地/用户环境（405 passed / 60 deselected），也不影响 v2.0.0 的功能、数据与 M6 研究结论 |

### 9.2 修复（v2.0.1）

- `tests/test_backtest_passive.py`：module 级 fixture `real_bars` 在 `bars_50.csv` 缺失时改为 **`pytest.skip(...)`**（数据构建后自动启用），与项目其余依赖运行期数据的用例行为一致。
- **不改生产代码**：`strategy/`、`backtest/`、`trading/`、`automation/`、`ui/` 均未改动；不改其他测试。
- 版本号统一为 `2.0.1`（`VERSION` / `config.yaml` / `core/config.py` / `core/__init__.py` / UI meta 兜底 / `docs/SPEC.md` 配置示例）。
- 已发布的 `v2.0.0` tag **不移动、不删除**；v2.0.1 以新 tag 发布。

### 9.3 修复后干净目录复核（Lead 实测，已填入）

### 9.3 修复后干净目录复核（Lead 实测）

<!-- CLEAN_RESULT -->
| 项 | 结果 |
| --- | --- |
| 复核方式 | 只复制**受版本控制的项目文件**（排除 `runtime/`、`.venv/`、缓存），模拟干净克隆；再运行全量测试 |
| 修复后结果（无运行期数据） | **383 passed / 22 skipped / 60 deselected / 0 failed / 0 error**，退出码 0（35.17s） |
| 对照修复前 | 383 passed / 17 skipped / 5 errors → 5 个 **error 变为 skip**，其余用例结果不变 |
| 数据就绪环境（本项目工作区） | **405 passed / 60 deselected / 0 failed**（57.00s） |
| 结论 | **干净克隆可跑通**：缺失运行期数据的用例按 skip 处理；构建数据后自动变为全量 405 passed；无 error |

> 复核命令（用户本机，非沙箱）：`python -m pytest tests -q`（干净克隆直接可用；本项目工作区含数据时为 405 passed）。
> 本沙箱环境使用 `runtime/_run_pytest.py` 包装器规避临时目录权限问题；该包装器不入库，仅影响测试临时目录 mode，不影响被测代码。
