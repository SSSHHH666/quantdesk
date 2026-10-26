# QuantDesk 数据可复现性说明（DATA_REPRODUCIBILITY）

> 版本：v1.0 · 2026-10-26 · 作者：数据研究支持（eval-data）
> 面向：需要**从零重建行情库**、审计数据状态、或做发布前数据体检的人（用户 / Dev1 / QA / Integrator）
> 配套文档：[`DATA_SOURCES.md`](DATA_SOURCES.md)（接口实测矩阵）、[`DATA_QUALITY_NOTES.md`](DATA_QUALITY_NOTES.md)（A 股数据口径坑）、[`RUNBOOK.md`](RUNBOOK.md)（运维排障）

---

## 0. 30 秒版

| 问题 | 答案 |
|---|---|
| 怎么从零重建行情库？ | `scripts/init_db.py` → `scripts/fetch_data.py --watchlist --years 5`（可选 `--market-snapshot`）→ `scripts/verify_data.py` |
| 数据源顺序？ | **新浪（主）→ 腾讯（备）→ Mock（仅内存，绝不落库）**；东财接口在本机不可用（已知） |
| 怎么校验？ | `scripts/verify_data.py`（一键 9 项）· `data/cache.py::BarCache.validate`（DQ001–DQ007）· `scripts/purge_mock_cache.py`（清 mock）· `runtime/exports/data_coverage.md`（覆盖报告） |
| 当前库状态？ | **PASS**：`daily_bars` 3,639 行 / 3 只标的 / 2021-10-25~2026-10-26 / 全部 `akshare` / 零 mock / 零缺口（详见 §5） |
| 合规边界？ | 仅个人研究；不对外分发、不商用；密钥只放环境变量（详见 §6） |

---

## 1. 前置条件

| 项 | 要求 / 实测 |
|---|---|
| 解释器 | `quantdesk\.venv\Scripts\python.exe`（Python **3.12.7**，由 Anaconda 创建） |
| 关键依赖 | akshare **1.19.3**、pandas 2.2.2、SQLAlchemy（版本基线见 `RUNBOOK.md` 附录 A） |
| 网络 | 需能访问**新浪财经** `finance.sina.com.cn`（当前唯一可用的行情主源） |
| 代理 | 系统代理对 akshare 不可用 → 取数前设 `NO_PROXY=*`、清空 `HTTP(S)_PROXY`（`build_universe.py` 等脚本已内置；手工调试见 RUNBOOK §4） |
| 数据库 | `runtime/quantdesk.db`（SQLite + WAL）；首次需初始化（§2 第 1 步） |

> **东财不可用是环境事实，不是配置错误**：`push2.eastmoney.com` / `push2his.eastmoney.com` 全部 ProxyError（详见 `DATA_SOURCES.md` §1）。
> 因此行情走新浪、腾讯备源；任何依赖东财的数据（资金流、PE/PB 快照）当前拿不到。

---

## 2. 从零重建行情库（4 步）

### 第 1 步 · 初始化数据库

```bat
cd /d <项目根>\quantdesk
.venv\Scripts\python.exe scripts\init_db.py
```

- **预期**：创建业务表（22 张 + 扩展表），输出中文进度；重复执行幂等（已存在则跳过）。
- **参数**：`--reset` 会先备份再重建（会清空持仓/订单/行情，慎用）。

### 第 2 步 · 确认自选池

`--watchlist` 读的是**数据库自选池表** `watchlist_items`（分组 `default/long_term/short_term/manual`）。
若为空，可先用 `config.yaml` 的 `data.universe` 初始化，或直接指定标的：

```yaml
# config.yaml（默认 3 只示例）
data:
  universe: ["600519.SH", "000001.SZ", "300750.SZ"]
  history_years: 5
```

### 第 3 步 · 拉取行情（可选：全市场快照）

```bat
rem 先演练（不联网、不写库），确认标的清单正确
.venv\Scripts\python.exe scripts\fetch_data.py --watchlist --years 5 --dry-run

rem 正式拉取（新浪主源，单只约 1~3s，3 只约 10s）
.venv\Scripts\python.exe scripts\fetch_data.py --watchlist --years 5

rem 可选：同时采全市场快照（落 market_snapshot，供异动雷达/名称映射；失败降级上次快照）
.venv\Scripts\python.exe scripts\fetch_data.py --watchlist --years 5 --market-snapshot
```

**常用变体**

| 目的 | 命令 |
|---|---|
| 指定标的 | `fetch_data.py --symbols 600519.SH,000001.SZ --years 5` |
| 只拉某个分组 | `fetch_data.py --watchlist --group short_term --years 5` |
| 覆盖历史年数 | `--years 5`（默认取 `config.data.history_years`） |
| 只演练不写库 | `--dry-run` |
| 指定配置 | `--config other.yaml` |

- **预期输出**：逐标的进度 + 写库统计（`UpdateReport.added/updated/skipped`），并生成覆盖报告 `runtime/exports/data_coverage.md`。
- **幂等性**（AC-F1-01）：同一区间重复拉取**不产生重复行**，第二次 `added = 0`；价格类数据 upsert。
- **失败处理**：单只失败会跳过并告警，不影响其它标的；主源失败自动降级（见 §3）。

### 第 4 步 · 校验（必做）

```bat
rem ① 一键数据体检（新脚本，只读，9 项检查）
.venv\Scripts\python.exe scripts\verify_data.py

rem ② 清理 mock 污染（先看再删）
.venv\Scripts\python.exe scripts\purge_mock_cache.py --dry-run
.venv\Scripts\python.exe scripts\purge_mock_cache.py

rem ③ 看覆盖报告（fetch_data 已生成）
notepad runtime\exports\data_coverage.md
```

三条命令的判定标准见 §4。

---

## 3. 数据源顺序与降级规则

| 优先级 | 通道 | 接口 | 状态（实测） | 说明 |
|---|---|---|---|---|
| 1（主） | 新浪 | `stock_zh_a_daily(adjust='')` / `('hfq-factor')` | ✅ 可用，0.7–3s/只 | 日线主源；`volume`=股、`amount`=元、`turnover`=小数 |
| 2（备） | 腾讯 | `stock_zh_a_hist_tx` | ✅ 可用，5–15s/只 | 主源失败时切换；成交量=股；复权因子由 hfq/raw 推导 |
| — | 东财 | `stock_zh_a_hist` / `stock_zh_a_spot_em` | ❌ **不可用** | 网络层拦截（ProxyError），**不参与主备链** |
| 3（兜底） | Mock | 内置确定性示例数据 | ✅ 可用 | **仅内存使用**：`source='mock'`，**绝不写入 `daily_bars`** |

**三条铁律**（违反会算出假收益，历史事故见 `DATA_SOURCES.md` §5）：

1. **mock 不落库**：`BarCache.update` 在 `provider=MockProvider` 时直接拒绝写入（`UpdateReport.mock_blocked`）。
2. **混源 fail-closed**：同一标的同一区间出现多个 `source` → `BarCache.load` 抛中文错误，**不做拼接**。
3. **降级必须可见**：降级到 mock 时 UI 显示「离线演示模式」，日志 WARN；该数据只能演示，不可回测。

配置开关（`config.yaml`）：

```yaml
data:
  provider: akshare     # akshare | mock（改 mock 即离线演示）
  fallbacks: [mock]     # 主源失败后的降级顺序
  request_interval_ms: 300   # 请求间隔（限流）
  max_retries: 3             # 重试次数
  stale_threshold_hours: 24  # 行情过期阈值（触发 DQ006 / 风控 RK006）
```

---

## 4. 如何校验（4 种手段 + 判定标准）

### 4.1 `scripts/verify_data.py`（推荐：一键体检）

```bat
.venv\Scripts\python.exe scripts\verify_data.py                       :: 默认库
.venv\Scripts\python.exe scripts\verify_data.py --db runtime\quantdesk.db
.venv\Scripts\python.exe scripts\verify_data.py --symbols 600519.SH   :: 只看单只
.venv\Scripts\python.exe scripts\verify_data.py --json                :: 机器可读（供 release_check）
.venv\Scripts\python.exe scripts\verify_data.py --strict              :: WARN 也算失败
```

| 编号 | 检查项 | 级别 |
|---|---|---|
| C1 | 数据库文件 + `PRAGMA integrity_check` + journal_mode | ERROR |
| C2 | `daily_bars` 规模（行数/标的数/日期范围）；指定 `--symbols` 时检查覆盖 | ERROR（空库） |
| C3 | 来源唯一性（同标的多 source）/ 是否存在 `mock` 行 | ERROR |
| C4 | 缺口（对照 `trading_calendar`） | WARN |
| C5 | `adj_factor` 空值 | WARN |
| C6 | 停牌标记一致性（零成交量未标记 / 标记却有成交） | WARN |
| C7 | `data_quality_issues` 统计，**并复算判断是否已过期（stale）** | ERROR（active）/ WARN（stale） |
| C8 | 标的元数据一致性（`symbols.exchange` / `board` vs 代码前缀） | WARN |
| C9 | 交易日历是否覆盖 `daily_bars` 区间 | WARN |

**退出码**：`0` = PASS（无 ERROR）· `1` = FAIL（有 ERROR；`--strict` 下 WARN 也算）· `2` = 环境错误（库缺失/打不开/损坏）。

> 给 `release_check.py` 的接入方式（**无需改 release_check**）：子进程调用并读退出码，或 `--json` 解析 `summary.error`/`summary.warn`。

### 4.2 `BarCache.validate`（DQ001–DQ007，会写库）

**常规做法：不需要单独执行** —— `scripts/fetch_data.py` 在每次拉取结束后会自动调用
`BarCache.validate(symbols, start, end)`，把结果写入 `data_quality_issues` 并生成覆盖报告。

```bat
rem 拉取 + 自动校验（一步完成）
.venv\Scripts\python.exe scripts\fetch_data.py --watchlist --years 5

rem 只想"只读"看现有 DQ 记录与是否过期 → 用体检脚本（不写库）
.venv\Scripts\python.exe scripts\verify_data.py
```

> 需要在代码里单独校验某区间时，按 `fetch_data.py` 第 6 步的方式构造：
> `cache = BarCache(provider=provider, config=config)` → `cache.validate(symbols, start, end)`
> （需要先按 `data/provider.py` 解析出 provider；不想搭 provider 就用上面的 `verify_data.py`）。

检测 7 类问题并写入 `data_quality_issues`：

| 码 | 含义 | 级别 | 建议动作 |
|---|---|---|---|
| DQ001 | 缺失 K 线（对比日历） | ERROR | 重新拉取该区间 |
| DQ002 | 重复 K 线 | WARN | 去重保留最新 |
| DQ003 | 价格异常跳变（>20% 且当日无除权事件） | ERROR | 先查除权事件，否则重拉 |
| DQ004 | 零成交量 | WARN | 确认停牌/节前，补 `is_suspended` |
| DQ005 | 复权因子缺失 | WARN | 补齐因子（否则退化为不复权） |
| DQ006 | 数据过期（> `stale_threshold_hours`） | WARN | 刷新行情 |
| DQ007 | 停牌未标记 | ERROR | 补 `is_suspended=1`，勿用 0 价格撮合 |

> 注意：`validate` 会**追加**问题记录，不会自动清理旧记录 → 老记录可能已"过期"（数据已修好但仍留在表里）。`verify_data.py` 的 C7 会复算并标出 stale 记录。

### 4.3 `scripts/purge_mock_cache.py`（清理 mock 污染）

```bat
.venv\Scripts\python.exe scripts\purge_mock_cache.py --dry-run   :: 只统计将删除行数
.venv\Scripts\python.exe scripts\purge_mock_cache.py             :: 执行清理（幂等）
```

清理 `daily_bars` / `adjust_events` 中 `source='mock'` 的行。清理后应重跑 `fetch_data` 回填真实数据。

### 4.4 覆盖报告与发布检查

| 手段 | 位置 / 命令 | 判定 |
|---|---|---|
| 覆盖报告 | `runtime/exports/data_coverage.md`（`fetch_data.py` 每次自动生成） | 各标的起止日一致、缺口 0、无 mock 混源 |
| 发布检查 | `scripts/release_check.py`（含 `daily_bars` 行数 + source 唯一性检查） | 退出码 0；PASS 项全绿 |
| 备份完整性 | `scripts/backup_db.py` / `restore_db.py`（见 RUNBOOK §7） | 恢复后数据一致 |

---

## 5. `runtime/quantdesk.db` 当前数据快照（2026-10-26 21:50 实测）

> 采集方式：`scripts/verify_data.py` + 只读 SQL 查询（`mode=ro`），命令与输出可复现。

### 5.1 库基本信息

| 项 | 值 |
|---|---|
| 文件 | `runtime/quantdesk.db`，**2,789,376 字节**（+ `-wal` 4,935,792B、`-shm` 32,768B） |
| 日志模式 | `wal` |
| 完整性 | `PRAGMA integrity_check = ok` |
| 表数量 | 29 张（22 张业务表 + 扩展表 + 系统表） |
| schema_version | 2 条：`M0 初始化（22 张业务表）` / `Phase 2 扩展表（SPEC §5.4 + 全市场快照 + 自选池迁移）` |

### 5.2 核心表行数

| 表 | 行数 | 说明 |
|---|---:|---|
| `daily_bars` | **3,639** | 3 只标的 × **1,213** 行 |
| `trading_calendar` | 1,213 | 2021-10-25 ~ 2026-10-26 |
| `adjust_events` | 86 | source = `akshare/eastmoney`（分红送配） |
| `market_snapshot` | 5,576 | 2026-10-26 全市场快照（单日） |
| `symbols` | 5,576 | 标的主数据（⚠️ 见 §5.4 缺陷 2） |
| `watchlist_groups` / `watchlist_items` | 4 / 3 | 分组：default/long_term/short_term/manual；3 只自选 |
| `data_quality_issues` | **1** | 历史遗留 DQ003（⚠️ 见 §5.4 缺陷 1） |
| `accounts` / `orders` / `cash_flows` / `job_runs` / `audit_logs` | 1 / 1 / 1 / 4 / 15 | 模拟盘与审计的运行痕迹 |

### 5.3 `daily_bars` 逐标的（核心结论）

| 标的 | 行数 | 首日 | 末日 | source | mock 行 | 空 adj_factor | 零成交量 | 缺口 |
|---|---:|---|---|---|---:|---:|---:|---:|
| 000001.SZ | 1,213 | 2021-10-25 | 2026-10-26 | akshare | 0 | 0 | 0 | **0** |
| 300750.SZ | 1,213 | 2021-10-25 | 2026-10-26 | akshare | 0 | 0 | 0 | **0** |
| 600519.SH | 1,213 | 2021-10-25 | 2026-10-26 | akshare | 0 | 0 | 0 | **0** |

**汇总**：`3,639` 行 / `3` 只 / 全库仅 `akshare` 一个 source / 重复主键 `0` / mock `0`。

**体检结论：PASS**（`verify_data.py` 退出码 0，ERROR **0** 个、WARN **3** 个、INFO 7 个）。

> **与「3×1212 行、截至 2026-10-23」的差异（重要，不是问题）**：
> 2026-10-26 是交易日，当日已跑过 `fetch_data`，因此多出**当天 1 根 bar**（1212 → 1213 行，末日 2026-10-23 → 2026-10-26）。
> 这是**正常增量**：交易日 15:30 后抓取即包含当日完整日线。核对记录为 `600519.SH 2026-10-26 close=1349.98`。
> 需要"截至 2026-10-23"的口径时，取数时用 `end_date=2026-10-23` 截断即可。

### 5.4 当前已知的 2 个遗留问题（需 Dev1 处理，不阻塞模型/回测）

| # | 问题 | 实测证据 | 影响 | 建议 |
|---|---|---|---|---|
| 1 | `data_quality_issues` 有 **1 条过期（stale）DQ003** | `#1 600519.SH 2025-01-02 DQ003`；**当前数据复算仅 2.36%**（阈值 20%），该日 bar 正常（1488.00 vs 前日 1524.00） | 无实际影响：属 mock 事故时期写入的历史记录，数据已修复但记录未清 | 清理历史 DQ 记录或在报告里忽略 stale 项（`verify_data.py` C7 已能自动识别） |
| 2 | `symbols` 表 `exchange` / `board` **大面积错值** | 全表 5,576 行 `exchange` 全为 `SH`、`board` 全为 `MAIN`：**2,905 只** 深市标的 exchange 应为 `SZ`、**2,028 只** 创业板/科创板 board 应为 `GEM`/`STAR`（如 `300750.SZ`、`688001.SH`） | **P0 风险**：若用该表判断板块，会把创业板/科创板（±20%）当主板（±10%），**涨跌停幅度、整手规则全错** | 按代码前缀修正：`600/601/603/605`→SH/MAIN，`000/001/002/003`→SZ/MAIN，`300/301`→SZ/GEM，`688/689`→SH/STAR。在修复前，模型/风控**不要用 `symbols.board`**，改用代码前缀判定 |

> 修复后请重跑 `verify_data.py`，C7/C8 应变绿（C8 无 WARN）。

---

## 6. 合规与使用边界

1. **用途**：本系统与本文档描述的取数行为**仅供个人学习与研究**，不构成投资建议。
2. **数据来源**：akshare 聚合的是公开财经接口（新浪/腾讯/东财等）；`DATA_SOURCES.md` 与 akshare 本身均声明数据"仅用于学术研究"。
3. **不对外分发**：不得将抓取的行情数据二次分发、转售、或用于生产/商业服务；不得高频并发抓取（`request_interval_ms` 默认 300ms，勿调至 0）。
4. **遵守上游条款**：各数据源的 robots/使用条款优先于本项目需要；若上游要求停止，应停止对应接口。
5. **密钥安全**：任何 token/密码只从环境变量读取（`QD_` 前缀），不写入 `config.yaml`、数据库、日志或本文档。
6. **结果不承诺**：回测/模拟盘结果不代表真实收益；真实资金接入不在本版本范围（只有 `QmtBroker` 骨架）。
7. **数据留存**：`runtime/` 属运行期产物（不入库/不外发）；对外演示请使用 Mock 模式（`provider: mock`）。

---

## 7. 常见问题（FAQ）

| 问题 | 处理 |
|---|---|
| 完全没有网络，还能演示吗？ | 能。`config.yaml` 设 `provider: mock` → 内存 mock 数据，全流程可跑；**但 mock 不落库、不可用于真实回测** |
| `fetch_data` 报 ProxyError / SSL 错误 | 系统代理对 akshare 不可用：设 `NO_PROXY=*` 并清空 `HTTP(S)_PROXY`（RUNBOOK §4 第 1 行） |
| 东财接口全挂怎么办？ | 已知环境事实，走新浪主源即可；不要依赖资金流/PE-PB 等东财专属数据（`DATA_SOURCES.md` §3） |
| 拉取很慢 / 被限流 | 单只 1~3s 属正常；增大 `request_interval_ms`、减少标的数；失败单只会跳过并记录 |
| 库里只有 3 只标的正常吗？ | 正常：MVP 默认自选池就是 3 只示例（`config.data.universe`）。扩池用 `--symbols` 或先维护 `watchlist_items` |
| 今天的数据什么时候能拉到？ | 交易日 **15:30 后**再抓才包含当日完整日线（收盘 15:00 + 结算延迟） |
| 重复跑 `fetch_data` 会重复写吗？ | 不会，upsert 幂等（AC-F1-01）；第二次 `added=0` |
| 想确认"某只标的某天为什么没有 bar" | 先查是否停牌：与交易日历比对 + **用腾讯备源交叉验证**（两源都缺=停牌；只有一处缺=数据洞，见 `DATA_QUALITY_NOTES.md` §3） |
| 验收时数据侧看什么？ | 跑 `verify_data.py`（看退出码与 ERROR 数）→ `runtime/exports/data_coverage.md` → `release_check.py` |

---

## 8. 一键复现（TL;DR）

```bat
cd /d C:\Users\shanghao\Documents\deepseek-harness\default-workspace\quantdesk

rem 1) 建库
.venv\Scripts\python.exe scripts\init_db.py

rem 2) 拉数据（先演练，再正式；可选全市场快照）
.venv\Scripts\python.exe scripts\fetch_data.py --watchlist --years 5 --dry-run
.venv\Scripts\python.exe scripts\fetch_data.py --watchlist --years 5
rem    （需要全市场快照时追加 --market-snapshot）

rem 3) 清理可能的 mock 污染
.venv\Scripts\python.exe scripts\purge_mock_cache.py --dry-run
.venv\Scripts\python.exe scripts\purge_mock_cache.py

rem 4) 体检（0=PASS / 1=FAIL / 2=环境错误）
.venv\Scripts\python.exe scripts\verify_data.py
```

---

## 9. 变更记录

| 版本 | 日期 | 变更 | 变更人 |
|---|---|---|---|
| v1.0 | 2026-10-26 | 初版：从零重建 4 步（init_db / 自选池 / fetch_data 含 `--market-snapshot` / 校验）、数据源顺序与三条铁律、4 种校验手段与判定、`runtime/quantdesk.db` 2026-10-26 21:50 实测快照（3×1213 行、全 akshare、零 mock、零缺口、PASS）与 2 个遗留问题（stale DQ003、symbols exchange/board 错值）、合规边界、FAQ、一键复现命令 | 数据研究支持（eval-data） |
