# QuantDesk 测试验收报告（QA 视角）

- 日期：2026-10-26
- 环境：Windows · `.venv` Python 3.12.7 · pytest 9.1.1 · 数据源 akshare（真实库）/ mock（测试隔离）
- 执行人：QA（eval-bots）
- 套件规模：**438 条**（默认快速套件 378 条 + `slow` 60 条）

## 1. 结果汇总

### 1.1 默认快速套件（开发内循环，目标 ≤ 90s）

命令：`.venv\Scripts\python.exe -m pytest tests -q`（`pytest.ini` 的 `addopts` 含 `-m "not slow"`）

| 次数 | 结果 | 耗时 |
|---|---|---|
| 第 1 次 | **378 passed, 60 deselected, 0 failed** | 40.05s |
| 第 2 次 | **378 passed, 60 deselected, 0 failed** | 46.85s |
| 第 3 次 | **378 passed, 60 deselected, 0 failed** | 50.50s |

结论：三次结果一致且全绿；耗时 40–51s，**满足 ≤ 90s 目标**。

### 1.2 集成层（目标：跨模块 + 临时库/SQLite）

命令：`.venv\Scripts\python.exe -m pytest tests -q -m integration`

| 次数 | 结果 | 耗时 |
|---|---|---|
| 第 1 次 | **59 passed, 379 deselected** | 35.79s |
| 第 2 次 | **59 passed, 379 deselected** | 36.83s |
| 第 3 次 | **59 passed, 379 deselected** | 36.07s |

### 1.3 全量套件（含 `slow`，发版/夜间口径，目标 ≤ 5min）

命令：`.venv\Scripts\python.exe -m pytest tests -q -m "slow or not slow"`

最近实测：**约 110–120s**（约 2 分钟），满足 ≤ 5min 目标。全量包含 AppTest 渲染、子进程脚本（备份/恢复/发版自检）、性能基线、大数据量回测等 60 条 `slow` 用例。

> 说明：本日开发仍在并发提交（新增页面/筛选/推荐 name 等），全量用例总数由 433 → 437 → 438 递增；上表默认/集成三次连跑期间无外部改动，数字一致。

### 1.4 发版前自检（release_check.py，不跳过测试）

命令：`.venv\Scripts\python.exe scripts\release_check.py`

```
汇总：PASS 11 / WARN 0 / FAIL 0
结论：[通过] 发版自检通过
```

包含项：Python 版本、关键依赖、可选依赖、配置加载、数据库完整性（integrity_check=OK）、22 张核心表、daily_bars 数据与来源唯一性（3,639 行，全部 akshare）、数据源健康（akshare OK）、**测试全绿（pytest -q 通过，378 passed）**、文档/配置、可选文档。

> 记录一次**瞬时** FAIL：首次运行时命中开发并发热更新窗口（`data.symbols.infer_exchange` 正处于落地过程中，报 `AttributeError`），重跑即 PASS 11/0/0。结论：**发版应在代码冻结后执行自检**，避免把热更新中间态当成发版缺陷。

### 1.5 备份/恢复脚本冒烟

| 命令 | 结果 |
|---|---|
| `scripts/backup_db.py --dry-run` | ✅ `[预演]` 正常，未写盘；识别源库 2,789,376 字节、保留 7 份 |
| `scripts/restore_db.py --list` | ✅ 列出备份目录，共 1 份（`quantdesk-20261026-213642.db`, 1,314,816 字节） |
| 备份→恢复 e2e（`tests/test_m4_delivery.py`） | ✅ 备份文件 integrity_check=OK、行数一致；恢复后数据一致；重复备份/恢复幂等；非交互无 `--yes` 拒绝 |

## 2. 验收标准（AC）覆盖矩阵

| AC / 契约 | 覆盖用例文件 | 覆盖情况 |
|---|---|---|
| AC-F1 / CF-2 数据层：mock 不落库、混源 fail-closed、purge 幂等 | `test_cache_source_isolation.py` | ✅ 含 mock 拦截 + 混源 `DataIntegrityError` + `purge_mock_cache.py` 幂等/`--dry-run` |
| AC-F2 策略注册/参数 schema/可评分/无未来数据 | `test_strategy_recommend.py`、`test_recommend_contract.py` | ✅ 6 策略注册、确定性、`as_of` 无前视（完整 vs 截断 bars 结果一致） |
| AC-F3-01…04 涨跌停/停牌/整手/T+1/费用精确值 | `test_broker_sim.py`（91 条） | ✅ 费用 5.64/10.64、主板/ST/创业板/科创板/北交所幅度、100 股整手、科创板 200 起、卖无持仓 fail-closed |
| AC-F3-05 / M1-16 / SPEC v1.1 绩效口径（回撤负值 + abs + calmar） | `test_metrics.py` | ✅ 手算序列（容差 1e-6）、零回撤/单日/无交易边界 |
| AC-F4 交易闭环（账户/持仓/资金曲线/幂等） | `test_models.py`、`test_broker_sim.py`、`test_backtest_persistence.py` | ✅ 状态机/费用/成交/落库往返 |
| AC-F6-01…09 / RK001–RK020 / fail-closed / 10 步顺序 / 卖出不受金额上限 | `test_risk.py`（53 条） | ✅ 20 个原因码逐一构造、顺序与文案、纯函数不写库、v1.1 卖出豁免 |
| AC-F7 UI：12 页渲染/交互/二次确认/错误卡/降级/文案审计 | `test_ui_pages.py`、`test_ui_copy_audit.py`、`test_m5_acceptance.py` | ✅ 逐页无异常+标题、按钮反馈、confirm 勾选解锁、RK007 错误卡、配置降级中文错误卡、禁止裸「无法获取/服务不可用」、无裸 `None` |
| AC-F8 事件/日志/审计/trace_id/脱敏 | `test_events_logging.py` | ✅ 多订阅者/异常隔离/取消订阅、JSON Lines、trace_id 贯穿、敏感信息掩码、审计 before/after |
| M0 骨架 / CF-13 测试基建 | `conftest.py`、`test_import_smoke.py` | ✅ 临时 SQLite、确定性工厂、markers；7 包 + 页面导入烟测、关键公开符号、registry ≥6 |
| M4 交付：备份/恢复/发版自检 | `test_m4_delivery.py` | ✅ 备份→恢复→数据一致、幂等、损坏库检出（中文提示）、`release_check` PASS |
| M5 验收：报告导出/演示模式/性能基线 | `test_m5_acceptance.py`、`test_performance_baseline.py` | ✅ HTML 报告（绩效+交易+可解析）、CSV 可解析回读、演示模式黄条、11 页渲染；冷启动 ≤15s / 二次渲染 ≤2s / 3 标的 5 年回测 ≤10s |
| 自选池数据层（多分组/source/导入导出/ST 提示） | `test_watchlist_store.py`（24 条） | ✅ 多分组并存、按 (symbol,group) 幂等 upsert、`group_name` 中文、`source=MANUAL/IMPORT`、JSON 导入导出键契约、ST 只 WARN |
| P0 数据混源回归（缓存/引擎） | `test_cache_source_isolation.py`、`test_engine_data_guard.py` | ✅ 混源禁止回测（中文「数据源混用」）、纯 mock `is_mock=True` + 「离线 Mock」、价格跳变告警 |

## 3. 未覆盖项与原因（QA 声明）

| 未覆盖项 | 原因 |
|---|---|
| 真实浏览器渲染（CSS/JS/图表交互、截图比对） | 使用 `streamlit.testing.AppTest` 在进程内渲染，不等同真实浏览器；如需视觉验收需 Playwright/Selenium（当前未纳入） |
| akshare 真实网络路径（限流/超时/字段变更） | 测试固定 `mock` + 离线以保证可复现；真实数据源由 `release_check` 的数据源健康检查与 `daily_bars` 来源唯一性检查兜底 |
| APScheduler 真实定时触发（到点执行） | 时间不可控、易 flaky；仅覆盖 scheduler 状态/启停 API 与 jobs 单元逻辑（他人用例） |
| QMT / CCXT 实盘适配 | 第一版仅骨架（无真实账户）；已覆盖错误码（如 `BR004`）与「未配置不崩溃」行为 |
| 异动雷达页 | 该页面当前未落地；落地后 `test_ui_pages.py` 的动态发现会自动覆盖 |
| F14 新功能「筛选」 | 按用户要求暂停筛选研究、专注收尾；未新增功能测试（既有 `test_screener.py` 4 条为他人补充的基础用例） |

## 4. 复现命令

```powershell
cd quantdesk
# 默认快速套件（≤90s）
.venv\Scripts\python.exe -m pytest tests -q
# 集成层
.venv\Scripts\python.exe -m pytest tests -q -m integration
# 全量（含 slow，≤5min）
.venv\Scripts\python.exe -m pytest tests -q -m "slow or not slow"
# 发版自检（代码冻结后执行）
.venv\Scripts\python.exe scripts\release_check.py
# 备份/恢复冒烟
.venv\Scripts\python.exe scripts\backup_db.py --dry-run
.venv\Scripts\python.exe scripts\restore_db.py --list
```

## 5. 结论

- 默认套件：**378 passed / 0 failed**，三次一致，40–51s（≤90s 达标）。
- 集成层：**59 passed / 0 failed**，三次一致，~36s。
- 全量：~2 分钟（≤5min 达标），无失败。
- 发版自检：**PASS 11 / WARN 0 / FAIL 0**。
- 备份/恢复与发版自检脚本冒烟全部通过；P0 数据混源、卖出持仓校验、AUTO 单 fail-closed、推荐 name 契约等回归均有专项用例保护。
- 剩余未覆盖项均为**环境性限制**（真实浏览器/真实网络/真实定时/实盘账户），已在第 3 节逐条说明。
