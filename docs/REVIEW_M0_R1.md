# M0 / R1 阶段评审报告（REVIEW_M0_R1）

> 版本：**v1.5（M0 退出复核 + Phase 2 批准 + M1 修订指针）** · 日期：2026-10-25 · 评审人：PM（pm-quant）
> 评审对象：R1 首批交付（10 项任务）+ M0 里程碑全部交付物 + P0/P1 修复复核
> 评审方式：静态走查 + 运行模块自检 + AC/P0 场景脚本复跑 + pytest 全量 + Streamlit 冒烟 + AppTest（只读评审，未修改代码）
> 依据：`BRIEF.md`、`docs/SPEC.md`（**v1.2**）、`docs/TASKS.md`（**v1.2**）、`docs/ROADMAP.md`（v1.1）、`docs/ARCHITECTURE.md`
> 说明：本报告只写文档；修复由对应 owner 执行，Lead 验收。

---

## 0. 结论摘要（v1.1 复核后）

| 维度 | 结论 |
|---|---|
| **M0 是否可退出** | ✅ **可以退出（有条件通过）**：M0 退出条件 6/6 达成；遗留 1 项构造性 P1（`app.py` 启动接线 `setup_logging()/init_db()`）列为 M1 首轮收口，不影响骨架退出 |
| P0 缺陷 | **3/3 已修复**（P0-1 卖出无持仓拒单、P0-2 默认 RiskGate 对 AUTO 拒单、P0-3 tests 缺失），均有回归用例并可复跑 |
| D-01 日志掩码 | ✅ **已修复**：Bearer token 完整掩码为 `Authorization: ***`，回归用例 `test_authorization_bearer_header_is_fully_masked` 通过 |
| QA 全量 | ✅ **295 passed / 0 failed**（1.71s，本机复跑一致） |
| R1 代码质量 | 优秀：中文错误、类型标注、契约注释、模块自检；费用/T+1/涨跌停/整手/风控核心计算经实测正确 |
| P1 剩余 | 2 项待收口：`trading/broker.py` 仍保留自带 BR 枚举（应统一到 `core.models`）、`app.py` 启动未接线日志/DB 初始化 |
| P2 剩余 | 5 项（详见 §6：自检命令文档、metrics CF-5 注释误标、`_board_of` 430、风控费用估算舍入 0.01、`OrderResult.from_order` PENDING 语义） |
| 口径登记 | 6 项已定口径已写入 SPEC v1.2 / TASKS v1.2（§8） |

---

## 1. 评审范围与方法

### 1.1 本轮复核覆盖的交付物

| 类别 | 文件（已读/已验） | 规模 |
|---|---|---|
| 基础设施 | `core/models.py`、`core/config.py`、`core/db.py`、`core/logging.py`、`core/events.py` | 约 4.5k 行 |
| 数据层 | `data/cache.py`（R2 起交付） | — |
| 策略/回测 | `strategy/base.py`、`strategy/builtin/{ma_cross,rsi_reversal,multi_factor}.py`、`backtest/{broker_sim,engine,metrics}.py` | 约 3.5k 行 |
| 交易执行 | `trading/{broker,risk,order_manager,portfolio,paper}.py` | 约 3k 行 |
| 自动化/UI | `automation/{scheduler,jobs,notify}.py`、`app.py`、`ui/pages/*`（9 页 + `_common.py`） | — |
| 脚本/文档 | `scripts/{start.bat,check_env.py,init_db.py}`、`docs/{RUNBOOK,ACCEPTANCE_CHECKLIST,THIRD_PARTY}.md`、`README.md` | — |
| 测试 | `tests/{conftest,test_models,test_events_logging,test_broker_sim,test_metrics,test_risk}.py` | 295 用例 |

### 1.2 验证方法（本轮实际执行）

1. **全量回归**：`.venv\Scripts\python.exe -m pytest -q` → **295 passed / 0 failed（1.71s）**。
2. **P0 场景复跑**（见 §4）：卖出无持仓 / 默认 RiskGate AUTO / Bearer 掩码。
3. **M0 退出证据**（见 §9）：`scripts/init_db.py`、`scripts/check_env.py`、`streamlit run app.py`（headless，HTTP 200）、`AppTest`、DB 表检查、`load_config()`、`core.events`/`core.logging` 自检。
4. **静态走查**：契约一致性（CF-1…CF-13）、依赖方向、风控唯一入口、口径与 SPEC v1.2 对齐。
5. **局限**：未做真实 akshare 长期跑数（M1-02 待做）；未双击验证 `start.bat`（无头环境，改为 `streamlit run` + 静态检查）；逐页 AppTest 切换受 Streamlit 1.37 `radio` 选项图标格式化限制，改用“9 页模块契约检查 + 主入口导航 9 项 + Lead 独立 AppTest 证据”组合。

---

## 2. 架构与契约一致性

| 检查项 | 结论 |
|---|---|
| 依赖方向（core 不 import 业务；backtest 不 import trading） | ✅ 符合；`trading → backtest.broker_sim` 为 CF-10 允许的单向依赖 |
| 22 张业务表 + 迁移表 | ✅ DB 实测 23 张表 = SPEC 22 张业务表 + `schema_version`（迁移版本表，非业务表） |
| 枚举/原因码 | 🟡 `risk.py` 已改为以 `core.models.RiskReasonCode` 为唯一来源（P1-2 部分修复）；`trading/broker.py` 仍自带 `BrokerErrorCode`（待统一） |
| `config.yaml` 键名（CF-9） | 🟡 与 SPEC §7 一致；**缺 `risk.allow_auto_orders` 键**（SPEC v1.2 已登记，实现有默认值，建议 Dev1 补键 + 注释） |
| 风控唯一入口 | ✅ `OrderService.place → RiskGate.check`；`RiskGate` 纯函数、fail-closed；回归用例覆盖 |
| 自动交易授权 | ✅ `allow_auto_orders` 默认 false，AUTO 单 `RK020`；显式授权 / `automation.enabled=true` 映射放行 |
| 关键数据缺失 fail-closed | ✅ 缺 `price_times`→`RK006`、缺时段→`RK005`、卖出缺持仓→`RK015`（回归覆盖） |
| 绩效口径 | ✅ `max_drawdown` 负值 + `max_drawdown_abs` 正值 + `calmar` 除零 None（SPEC v1.1） |
| 页面发现 | ✅ `app.py` 动态发现 `ui/pages/*.py`，9 页均暴露 `PAGE_TITLE` + `render()`；`strategies.py` 与旧标题去重 |

---

## 3. AC 逐项对照（复核后状态）

> 图例：✅ 通过 · 🟡 部分通过 · ⚪ 未验证（依赖外部数据/真实环境）

| AC | 结论 | 证据/说明 |
|---|---|---|
| AC-F2-01（3 个内置策略） | ✅ | `list_strategies()` → `['ma_cross','multi_factor','rsi_reversal']` |
| AC-F2-04（TA-Lib/pandas 一致） | ✅ | 双实现最大差 1e-14 量级（远优于 1e-6） |
| AC-F3-01/03/04（涨跌停/费用/整手） | ✅ | 费用 5.64/10.64 精确；整手与科创 200 股、一字板/涨停价委托符合 |
| AC-F3-05（指标手算） | ✅ | 负回撤口径 + abs + calmar 除零，QA `test_metrics.py` 覆盖 |
| AC-F3-06/07（3 策略出报告/可复现） | 🟡 | 3 策略已交付，Lead 手工首次回测成功（Mock 数据，见 ROADMAP §3.3 基线）；M1-11 引擎端到端验收待 R3 |
| AC-F4-*（模拟盘） | 🟡 | `PaperBroker`/`OrderService`/`PortfolioService` 已交付，QA 订单/账户用例通过；UI 接线待 M2 |
| AC-F6-01（唯一入口） | ✅ | `order_manager` 为唯一 `submit_order` 调用点 + 单测断言（待 QA 全链路用例补强） |
| AC-F6-02（20 码覆盖） | ✅ | 295 用例覆盖 RK 码与判定顺序 |
| AC-F6-03…07（风控关键场景） | ✅ | 实测/QA 用例通过 |
| AC-F6-08（参数热更新） | 🟡 | `risk_config` 覆盖机制 + 设置页待接线验收 |
| AC-F6-09（金额仅买入） | ✅ | 卖出豁免用例 `test_sell_is_exempt_from_single_order_value_cap` |
| AC-F6-10（自动交易授权） | ✅ | `test_default_gate_rejects_auto_order` / `test_explicit_authorization_allows_auto_order` |
| AC-F6-11（数据缺失 fail-closed） | ✅ | 缺 price_times/时段/持仓用例 |
| AC-F7-01/02（9 页/4 核心任务） | 🟡 | 9 页模块可加载、AppTest 主入口 0 异常、导航 9 项、Lead AppTest 9 页全渲染；页面业务接线待 M1/M2 |
| AC-F8-01/02/05（日志/审计/过滤） | ✅ | 48 项日志/事件用例通过；Bearer 掩码修复 |
| AC-F8-03/04（通知） | 🟡 | `notify.py` 已交付，渠道实测待 M3 |
| AC-F9-01…04（适配层） | 🟡 | `BrokerAdapter` 抽象 + Paper 实现路径已交付；Ccxt/Qmt 骨架验收待 M4 |
| AC-F1-*（数据层） | 🟡 | `data/cache.py` 已交付；M1-02 真实 akshare 拉数/增量/校验待 R3 验收 |

**复核结论**：本轮相关 AC 无 ❌；🟡 项均为“实现已交付、端到端验收待后续里程碑”，符合 M0 阶段定位。

---

## 4. P0 修复复核（2026-10-25 复跑）

| 编号 | 原缺陷 | 修复状态 | 回归用例（`tests/`） | 复跑证据 |
|---|---|---|---|---|
| **P0-1** | `SimBroker` 卖出无持仓/未传 positions 仍成交（可卖空） | ✅ **已修复**（`backtest/broker_sim.py` 卖出分支 fail-closed：未传持仓→`RK015`+warning；无持仓/超持仓→`RK015`；超可卖→`RK016`） | `test_broker_sim.py::test_sell_without_positions_argument_is_rejected`、`::test_sell_with_empty_positions_mapping_is_rejected`、`::test_sell_more_than_held_is_rejected`、`::test_same_day_buy_then_sell_is_rejected` | `positions=None/[]` → **fills=0、rejected=['RK015']**；持仓 50 卖 100 → RK015；持仓 100 卖 100 → fills=1 正常 |
| **P0-2** | 默认 `RiskGate()` 对 AUTO 单放行（fail-open） | ✅ **已修复**（新增 `allow_auto_orders`，默认 false；AUTO 单 `RK020`；显式授权或 `automation.enabled=true` 才放行；`risk.enabled=false` 也不能绕过） | `test_risk.py::test_default_gate_rejects_auto_order`、`::test_auto_order_needs_explicit_authorization`、`::test_explicit_authorization_allows_auto_order`、`::test_risk_gate_disabled_still_blocks_unauthorized_auto_order` | `RiskGate()` AUTO → **False RK020**；`RiskGate(RiskConfig())` AUTO → False RK020；MANUAL → True |
| **P0-3** | `tests/` 不存在，验收无法回归 | ✅ **已修复**（QA 交付 6 个测试文件 `conftest` + models/events_logging/broker_sim/metrics/risk） | 全量 `pytest -q` | **295 passed / 0 failed（1.71s）** |
| **D-01** | 日志 Bearer token 掩码缺陷 | ✅ **已修复**（`mask_text` 完整掩码 `Authorization: ***`） | `test_events_logging.py::test_authorization_bearer_header_is_fully_masked`、`::test_mask_text_removes_secrets` | `mask_text("Authorization: Bearer eyJ...")` → `Authorization: ***`；`password=hunter2 token=abc123` → 全部 `***` |

> **P0 结论：3/3 已修复并固化为回归用例，可关闭。**

---

## 5. P1 处置状态

| 编号 | 原问题 | 当前状态 | 说明/剩余动作 |
|---|---|---|---|
| P1-1 | `Bar.prev_close` 未落库，涨跌停判定可能被静默跳过 | ✅ 已修复 | Lead 裁决“engine 必须传入 prev_close，缺失则拒绝成交并记 warning（fail-closed）”；`broker_sim` 已实现 fail-closed 路径，engine 负责注入 |
| P1-2 | RK/BR 枚举两处重复定义（漂移风险） | 🟡 部分修复 | `trading/risk.py` 已改为以 `core.models.RiskReasonCode` 为唯一来源；**`trading/broker.py` 仍自带 `BrokerErrorCode`**（待 Dev3 统一为 re-export） |
| P1-3 | `MarketSnapshot.is_trading_time` 默认 False 的集成陷阱 | ✅ 已修复 | 改为“数据缺失即拒绝”（RK005/RK006）+ `assemble_market_snapshot()` 统一组装；回归覆盖 |
| P1-4 | `OrderService` 未交付 → AC-F6-01 不可验证 | ✅ 已交付 | `trading/order_manager.py` 已交付；唯一下单入口 + 风控前置由用例覆盖 |
| P1-5 | `RiskConfig` 字段归属/来源不一致 | ✅ 已裁决 | **`config.yaml` 为唯一运行时来源，`core.models.RiskConfig` 仅 DTO**；SPEC v1.2 §6.3/§9.1 已登记 |
| P1-6 | `AuditActor.AUTO` 契约扩展 | ✅ 已批准 | **保留并用于自动化审计**（Lead 批准）；SPEC 已同步 |
| P1-7 | 多模块未交付（数据/引擎/UI/自动化等） | 🟡 大部分已交付 | `data/`、`backtest/engine.py`、`trading/{order_manager,portfolio,paper}.py`、`automation/*`、`app.py`、9 页 UI 均已就位；仍缺真实调度验收、`USER_GUIDE.md`、`backup_db.py/restore_db.py`（后两者按 TASKS v1.2 保持“未交付，Integrator，后续轮次”） |

---

## 6. P2 处置状态

| 编号 | 原问题 | 当前状态 |
|---|---|---|
| P2-1 | 自检命令文档写“直接运行 `python core/xxx.py`”但实际不可用（sys.path 遮蔽 `logging`） | 🟡 未修复；`core/events.py:408`、`core/models.py:1798` 注释仍如此写；建议统一为 `python -m core.models` 或提供 `scripts/selftest.py` |
| P2-2 | `backtest/metrics.py`、`backtest/__init__.py` 把绩效指标标为“CF-5”（CF-5 是 RiskGate） | 🟡 未修复（文档注释问题，不影响行为；TASKS 已修正为“无冻结”） |
| P2-3 | `trading/risk.py::_board_of` 未识别北交所 `430xxx`（判为 MAIN） | 🟡 未修复（实测 `430047 → MAIN`；MVP 不交易北交所，低风险；接入前统一复用 CF-10 `guess_board`） |
| P2-4 | 风控 `_estimate_fees` 用银行家舍入，与 CF-10 可能差 0.01 元 | 🟡 未修复（实测卖 501@10：风控 7.82 vs 实际 7.83；建议复用 `FeeModel`/Decimal HALF_UP） |
| P2-5 | `check_env.py` 在可选 ccxt 缺失时退出码 1 | ✅ 已修复（实测 exit=0，仅提示可选缺失） |
| P2-6 | `trading/broker.py` 无自检/单测 | 🟡 未修复（M2-01 AC ④ 待补用例） |
| P2-7 | `OrderResult.from_order` 对 PENDING 默认 `success=True` | 🟡 未修复（实测 PENDING→True；建议按 status 映射） |
| P2-8 | `min_order_amount_enforce_sell` 与旧 SPEC 表述冲突 | ✅ 已裁决（金额仅买入，SPEC v1.1/AC-F6-09 覆盖） |

---

## 7. M0 里程碑完成度（全项已交付）

| 任务 | 状态 | 交付物 |
|---|---|---|
| M0-01 依赖与配置 | ✅ | `requirements.txt`、`config.yaml`（缺 `allow_auto_orders` 键，见 §2/§9.1） |
| M0-02 配置加载 | ✅ | `core/config.py`（pydantic 校验，`load_config()` 实测通过） |
| M0-03 领域模型与 22 表 | ✅ | `core/models.py` + 自检（建表/PK/唯一/索引） |
| M0-04 DB 引擎/建表/备份 | ✅ | `core/db.py` + `scripts/init_db.py`（幂等，实测 1.17s） |
| M0-05 日志与审计 | ✅ | `core/logging.py`（JSON Lines + 脱敏 + 审计；含 D-01 修复） |
| M0-06 事件总线 | ✅ | `core/events.py` + 自检 + 48 项事件/日志用例 |
| M0-07 入口与 9 页 | ✅ | `app.py` 动态发现 + `ui/pages/*` 9 页（模块契约 9/9，AppTest 主入口 0 异常） |
| M0-08 启动/环境自检 | ✅ | `scripts/start.bat`、`check_env.py`（exit=0，9/9 核心依赖） |
| M0-09 初始化脚本 | ✅ | `scripts/init_db.py`（幂等、22 业务表 + schema_version） |
| M0-10 测试骨架 | ✅ | `tests/`（conftest + 6 文件，295 passed） |

> 一级结论：**M0 的 10 项任务全部交付**，较 R1 评审时的“未达退出条件”已闭环。

---

## 8. 已定口径登记（Lead 裁决，已写入 SPEC v1.1/v1.2、TASKS v1.1/v1.2）

| # | 裁决内容 | 责任/落地 | 文档位置 |
|---|---|---|---|
| 1 | **RK012 金额类仅约束买入**：`max_single_order_value_pct` 与 `min_order_amount` 仅约束买入；卖出只受整手/持仓可用(T+1)/停牌/涨跌停/时段约束 | Lead 裁决 / Dev3 已落地 | SPEC §4.4、§8.6、§9.1、§9.2、AC-F6-09 |
| 2 | **绩效口径**：`max_drawdown` 为负数、另出 `max_drawdown_abs` 正数；`calmar=annual/\|mdd\|`，除零 None + `notes` | Lead 裁决 / Dev2 已落地 | SPEC §6.4、§8.3、AC-F3-05 |
| 3 | **prev_close fail-closed**：engine 必须传入 prev_close；缺失则拒绝成交并记 warning | Lead 裁决 / Dev2 已落地 | SPEC §8.3（回测异常）、本报告 P1-1 |
| 4 | **RiskConfig 归属**：`config.yaml` 为唯一运行时来源（`risk_config` 表为 UI 覆盖值），`core.models.RiskConfig` 仅 DTO；`RiskGate` 纯函数、参数由调用方传入 | Lead 裁决 / Dev3 已落地 | SPEC §6.3、§9.1 |
| 5 | **AuditActor.AUTO 保留**：用于自动化审计 | Lead 批准 / Dev1 已落地 | SPEC §5.2、本报告 P1-6 |
| 6 | **自动交易授权 + 快照扩展**：新增 `risk.allow_auto_orders`（默认 false，AUTO 单 fail-closed）；`automation.enabled=true` 自动映射授权；`MarketSnapshot` 扩展字段经 `extra="allow"` 透传、缺关键数据即拒绝 | Dev3 实现 / Lead 确认 | SPEC §6.7、§7、§8.6 规则 8/9、§9.1、AC-F6-10/11 |

---

## 9. M0 退出检查清单（2026-10-25 复核）

> 对照 `ROADMAP.md` §2.3「用户视角验收演示脚本（M0）」+ `TASKS.md` §0.3「M0 退出条件」逐条勾选。

| # | 检查项 | 结论 | 证据（命令/结果） |
|---|---|---|---|
| C1 | `streamlit run app.py` 可启动 | ✅ | `python -m streamlit run app.py --server.headless true --server.port 8511` → 启动日志输出 Local URL；`http://127.0.0.1:8511` **HTTP 200（约 0.8s）** |
| C2 | 9 页可切换 | ✅ | 9 个页面模块均暴露 `PAGE_TITLE` + `render()`；AppTest 主入口 **0 异常**、导航 **9 项**；Lead 独立 AppTest 验证 9 页全渲染 |
| C3 | DB 建表成功 | ✅ | `python scripts/init_db.py` → exit 0；SQLite 实测 **23 张表 = 22 业务表 + `schema_version`**；二次运行幂等（各 1.17s） |
| C4 | 配置/日志/事件可用 | ✅（模块级） | `load_config()` → `QuantDesk 1.0.0 / provider=akshare / risk=80.0 / auto=False`；`python -m core.logging`、`-m core.events` 自检通过；事件/日志 pytest 48 项通过 |
| C5 | pytest 通过 | ✅ | `python -m pytest -q` → **295 passed / 0 failed / 1.71s** |
| C6 | 重启/热启动正常（≤5s） | ✅（代理指标） | AppTest 首次 2.02s、二次 **0.03s**；`init_db` 二次运行幂等；`start.bat` 静态检查通过（解释器回退/缺 app 提示/pause） |
| 补充 | 环境自检（M0-08） | ✅ | `python scripts/check_env.py` → **exit=0**，核心依赖 9/9，可选 ccxt 缺失仅提示 |
| 补充 | 第三方与许可登记（M4-04） | ✅ | `docs/THIRD_PARTY.md`：GPL/AGPL 仅重写、来源 URL/用途/判定齐全 |

### 9.1 M0 遗留项（不阻塞退出，列入 M1 首轮）

| 编号 | 遗留项 | 责任人 | 建议 |
|---|---|---|---|
| M0-L1 | `app.py` 启动时未调用 `setup_logging()` / `init_db()`：导致“用户首次启动自动建库”“操作后 `runtime/logs/app-*.log` 生成”两项**演示级**动作需先跑 `scripts/init_db.py`，日志文件暂为空 | Dev3 / Integrator | M1 首轮在 `app.py` 启动处加 2 行幂等 bootstrap，并加 1 条 AppTest/冒烟断言；完成后回填本清单 |
| M0-L2 | `config.yaml` 缺 `risk.allow_auto_orders` 键（实现默认 false，功能不受阻） | Dev1 | 按 SPEC §7 补键 + 注释（CF-9 键名已登记） |
| M0-L3 | 逐页 AppTest 切换未在本次复核脚本中跑通（Streamlit 1.37 `radio` 图标格式化限制），采用替代证据 | QA | M1 阶段补 `tests/test_app_pages.py`（按索引切换）固化为回归 |

---

## 10. M0 退出结论与 M1 开工顺序

### 10.1 结论

**M0 可以退出（有条件通过）。** 6/6 退出条件达成；10 项 M0 任务全部交付；P0 全部修复并回归；QA 295 passed。遗留 M0-L1/L2/L3 均为“不影响骨架交付”的收口项，已明确责任人与时间点（M1 首轮），建议 Lead 正式宣布 M0 退出并进入 M1。

> **M1 后续进展（2026-10-25，修订版）**：M1 功能验收完成（真实**不复权** akshare 数据、4 策略报告、379 测试全绿），详见 **`docs/REVIEW_M1.md` v1.1**——同仓位被动对照下仅 ma_cross（+1.87pp）与 ml_ranker（+0.45pp）略好，全部小幅负收益、**无显著 alpha**；ML 真实 WF 6 折 OOS AUC **0.4456（弱于随机）**；M1 结论 = 功能通过 / **策略有效性未证明**，样本外验证移交 M6-08（R8 第一任务）。

### 10.2 M1 开工顺序（真实数据 → 回测报告 → UI 接线）

| 顺序 | 任务 | 负责 | 说明/前置 |
|---|---|---|---|
| 1 | **M1-02 真实数据接入**（akshare Provider：日线/指数/日历/基本面 + 限流重试 + 主备降级） | Dev1 | M0 已退出、`DataProvider` 抽象与 `data/cache.py` 已就绪；这是 M1 的第一个真实数据关口 |
| 2 | M1-03 增量缓存（幂等 upsert + 复权）→ M1-04 数据质量校验（DQ001–DQ007） | Dev1 | 与①串行，完成后 M1-13 样例数据可固化 |
| 3 | **M1-11 回测引擎接线** + M1-12 指标（已交付）→ 首次完整回测报告 | Dev2 | 以 Lead 已手工验证的 Mock 基线（ROADMAP §3.3：ma_cross +0.49%/-6.23%、rsi_reversal +0.19%、multi_factor +16.88%/夏普 0.62）作为验收对照 |
| 4 | M4-03 回测结果持久化 → **UI 接线：行情页（M4-01）→ 回测页（M4-02）→ 策略页（M3-05）** | Dev2/Dev3 | 先“能看数、能跑回测、能看报告”，再顺序接总览/持仓/手动交易（M2-08/09/10） |
| 5 | QA：M1-14/15/16 回归 + e2e 骨架（M4-06 提前） | QA | 每个接线点补 1 条端到端断言；M0-L3 一并处理 |
| 6 | M0-L1/L2 收口（启动 bootstrap + config 键） | Dev3/Dev1 | 与①并行，1 小时内可完成 |

> 风险提示：①M1-02 受 akshare 限流/改版影响，保持 Mock 降级链可用；②回测结果与 Lead 基线差异过大时，先核对数据区间/复权/参数再定位代码；③UI 接线不得绕过 `OrderService`（风控唯一入口）。

---

## 11. 复现命令（只读复核）

```bat
rem 在 quantdesk 目录下执行
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe scripts\check_env.py
.venv\Scripts\python.exe scripts\init_db.py
.venv\Scripts\python.exe -m core.models
.venv\Scripts\python.exe -m core.events
.venv\Scripts\python.exe -m core.logging
.venv\Scripts\python.exe -c "from core.config import load_config; c=load_config(); print(c.app.name, c.risk.max_total_position_pct)"
.venv\Scripts\python.exe -m streamlit run app.py --server.headless true --server.port 8511
```

P0 复跑要点（本报告 §4 已实跑）：

```python
# P0-1：卖出无持仓 → 期望 fills=0, rejected=['RK015']
SimBroker().match_with_report([sell_order], {"600519.SH": bar}, day, positions=[])
# P0-2：默认构造 AUTO 单 → 期望 allow=False, reason_code='RK020'
RiskGate().check(auto_order, acc, positions, market)
# D-01：Bearer 完整掩码 → 期望 'Authorization: ***'
mask_text("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig")
```

---

## 12. 评审结论

1. **M0 达到退出条件**：10 项任务全部交付，pytest 295/295，Streamlit 可启动、9 页就绪、DB 22 业务表建成、配置/日志/事件可用。
2. **三个 P0 与 D-01 全部修复并回归**：卖出 fail-closed、AUTO 单 fail-closed、Bearer 掩码、测试骨架，均有可复跑的用例名。
3. **口径一致性显著改善**：6 项裁决已落到 SPEC v1.2 / TASKS v1.2；金额仅买入、负回撤、config 唯一来源、AUTO 授权、数据缺失 fail-closed 全部有回归保护。
4. **建议 Lead 宣布 M0 退出**，并按 §10.2 顺序启动 M1：真实数据（M1-02）→ 增量/校验 → 回测报告（以 Lead 基线对照）→ UI 接线。
5. 待收口：M0-L1/L2/L3（M1 首轮，责任到人）、P1-2（broker BR 枚举统一）、P2 五项（可排期）。

---

## 13. 目标升级（Phase 2）范围变更评审（2026-10-25 追加）

> 背景：用户把项目目标升级为“**以赚钱为导向**”，提出推荐自选池、自选池可添加、找赚钱办法（多因子/ML/公开方法）、为手动/自动下单留接口、关注资金流入/异动盘，并给出“远期（年化 10–20% 价值型）+ 近期（异动前兆/拉升起爆点）”两条线。
> 处置：按 Lead 指示，只改文档、不写代码；新增 **Phase 2 功能 F10–F13 + 里程碑 M6**，MVP（F1–F9/M0–M5）边界与验收不变。

### 13.1 变更清单（已落文档）

| 文档 | 版本 | 变更 |
|---|---|---|
| `SPEC.md` | **v1.3** | 新增 §8A：F10 选股与推荐（远期/近期两榜，AC-F10-01…05，含“推荐理由可解释”）、F11 异动雷达（AC-F11-01…04）、F12 自选池管理（AC-F12-01…05）、F13 策略研究（AC-F13-01…05，含“回测必须样本外”）；新增 §5.4 五张表、§6.8 接口、§7 `recommend/research` 配置、§11.8 合规与 UBA、G7–G9、M6 验收门槛、OQ-6/7/8、D-10/11/12；新增警告码 `RQ_WARN_NO_BENCHMARK`；UI 页 9→11 |
| `ROADMAP.md` | **v1.2** | 新增 **M6 策略研究增值**（目标/入口条件/12 项交付物/用户视角验收脚本 10 条/R8 ≈8 人日/风险/退出标准）；总览 75 项；**M6 CPU-only、不引入深度学习框架**（ML 用可选 sklearn，深度模型留远程可选） |
| `TASKS.md` | **v1.4** | 新增 **M6-01…M6-12**（文件归属：`core/models.py`、`data/watchlist.py`、`strategy/recommend.py`、`strategy/ml_ranker.py`、`strategy/builtin/{value_quality,breakout_abnormal}.py`、`backtest/validation.py`、`ui/pages/{watchlist,recommend}.py`、3 个测试文件）；任务 63→75、R8、M6 退出条件、Phase 2 契约扩展待批、文件归属矩阵、M6 风险与合规红线 |
| `GLOSSARY.md` | **v1.1** | 新增 §3A「Phase 2 术语」29 项（两榜/可解释性/异动指标/雷达/自选池/样本外/walk-forward/稳健性/UBA/ML 等）；更新样本外/过拟合/ML/深度学习口径；OQ 扩至 8、R 扩至 R8 |

### 13.2 评审意见（风险与门槛）

| 项 | 结论/建议 |
|---|---|
| **范围与 BRIEF 冲突** | ⚠️ `BRIEF.md` 仍是“MVP F1–F9、第一版不做 walk-forward”的原口径，与本轮 Phase 2 扩展存在文档间冲突。**建议 Lead 更新 BRIEF**（新增 Phase 2 范围/目标取向/合规口径），否则后续新成员/智能体可能按 BRIEF 拒绝 F10–F13。PM 未改 BRIEF（Lead 所有）。 |
| **合规** | ✅ 已在 SPEC §11.8 固化：不承诺收益、推荐强制展示回测区间+样本外+风险提示+UBA、禁用承诺性表述、推荐默认不自动下单。**建议 UI 实现时把 UBA 作为验收硬条件**（AC-F10-03）。 |
| **过拟合** | ✅ 样本外强制（walk-forward ≥3 折 + purge/embargo）、参数稳健性、禁止自动寻优；ML 时间序列切分不得打乱。**风险仍高**（M6 核心难点），建议 M6-08 先行、任何“可用”标记必须过 M6-08。 |
| **算力/依赖** | ✅ CPU-only；sklearn 为**可选依赖**（BSD-3，缺失降级），不引入 PyTorch/TensorFlow/JAX；GTX 1650 不是前置条件。**建议 OQ-7 尽快拍板**，否则 M6-07 无法排期。 |
| **数据可得性** | ⚠️ 资金净流入/龙虎榜/分钟数据依赖第三方源，A 股公开接口不稳定。建议 F11 首批只用**日频可得规则**（量比/换手/振幅/涨幅/缺口），资金流类规则“数据可得才启用”，缺失明确标注。 |
| **工期** | ⚠️ M6 ≈8 人日（12 项），但推荐可解释 + 样本外 + 2 个新页面实际可能超；建议 **M6 入口硬门槛 = M1 验收通过**，并分两批（F10/F12 先行，F11/F13 随后），ML 作为可选增强。 |
| **新增警告码** | ✅ `RQ_WARN_NO_BENCHMARK`（无基准：基准指标 None、不算超额收益、UI/报告提示）已登记 SPEC §9.2（Dev2 已实现、Lead 已批准）；M6 推荐/研究沿用。 |
| **不自动下单** | ✅ 推荐默认不自动下单（D-10/OQ-6）；若未来放开需二次确认+审计+安全评审。 |
| **M0/M1 不受影响** | ✅ 本轮为**新增 Phase 2**，M0 退出结论与 M1 开工顺序不变；M6 必须在 M1 验收后启动。 |

### 13.3 M6 开工前置检查（2026-10-25 已由 Lead 逐项批准）

1. ☑ Lead 更新 `BRIEF.md`（Phase 2 范围）→ **已完成，BRIEF v0.2（F1–F13）**；2. ☑ Lead 批准 Phase 2 契约扩展（5 表/接口/配置/事件/警告码）→ **已批准 2026-10-25**；3. ☑ OQ-6/7/8 拍板 → **推荐禁止自动下单 / sklearn 可选依赖 / OOS 年化≥10% 且回撤≤15% 为达标口径**；4. ☐ M1 研究闭环验收通过（含 walk-forward 框架可用）——**仍为 M6 唯一未满足入口**；5. ☐ M6-01 表结构任务派单（Dev1）——待 Lead 派单。

### 13.4 批准落地（2026-10-25）

| 项 | 结论 | 落地文档 |
|---|---|---|
| Phase 2 契约扩展 | ✅ 已批准：5 张新表（扩展新增、**不改已有 22 表**）、§6.8 接口、`recommend.*`/`research.*` 配置、2 个事件主题、`RQ_WARN_NO_BENCHMARK` | SPEC v1.4、TASKS v1.5 |
| OQ-6 推荐自动下单 | ✅ 禁止：推荐只能“带入手动交易页/需用户二次确认”；自动化任务不得消费 recommend 输出直接下单 | SPEC §14、BRIEF §2 |
| OQ-7 sklearn | ✅ 允许为可选依赖（缺失时 ML 不可用、文档标注、UI 提示安装）；禁止 torch/tensorflow | SPEC §8.13、BRIEF §4 |
| OQ-8 达标口径 | ✅ 近 3 年 OOS 年化 ≥10% 且最大回撤 ≤15% 才标“达到研究目标（研究参考、不承诺收益）”；未达标只能“研究阶段”且禁止接自动化 | SPEC §8.13 规则 4、§14 |
| BRIEF 更新 | ✅ v0.2：F1–F13、Phase 2、CPU-only/DL 禁止、不承诺收益/UBA、M6 | `quantdesk/BRIEF.md` |
| DOC_INDEX | ✅ 已同步各文档版本 + 新增「11 页页面状态表」 | DOC_INDEX v1.4 |

> **M6 剩余入口**：仅剩“M1 研究闭环验收通过”与“M6-01 派单”；建议 M1 验收后立即启动 R8。

---

## 14. 变更记录

| 版本 | 日期 | 变更 | 评审人 |
|---|---|---|---|
| v1.0 | 2026-10-25 | 初版：R1 交付 10 项任务评审；M0 里程碑完成度；P0×3、P1×7、P2×8；AC 对照；Lead 裁决口径登记与派单建议 | pm-quant |
| v1.1 | 2026-10-25 | **M0 退出复核版**：①P0×3 + D-01 复核为“已修复（含回归用例名）”；②新增 §9「M0 退出检查清单」（C1–C6 + 遗留 M0-L1/L2/L3）；③新增 §10 退出结论与 M1 开工顺序；④更新 P1/P2 处置状态；⑤登记 6 项已定口径与责任人；⑥交付物状态更新（M0 十项全部交付） | pm-quant |
| v1.2 | 2026-10-25 | **目标升级（Phase 2）范围变更评审**：①新增 §13（变更清单：SPEC v1.3 F10–F13/M6、ROADMAP v1.2 M6、TASKS v1.4 12 项、GLOSSARY v1.1 术语）；②风险与门槛（BRIEF 冲突需 Lead 更新、合规/UBA、过拟合、数据可得性、工期、CPU-only）；③`RQ_WARN_NO_BENCHMARK` 登记；④M6 开工前置检查 5 项；⑤M0/M1 结论不变 | pm-quant |
| v1.3 | 2026-10-25 | **Phase 2 批准落地**：①§13.3 前置检查更新（BRIEF v0.2 完成、契约扩展批准、OQ-6/7/8 拍板；仅剩 M1 验收与 M6-01 派单）；②新增 §13.4 批准落地表（契约扩展/OQ-6 禁自动下单/OQ-7 sklearn 可选/OQ-8 达标口径/BRIEF/DOC_INDEX）；③版本同步 SPEC v1.4、TASKS v1.5 | pm-quant |
| v1.4 | 2026-10-25 | **M1 验收指针**：§10.1 增加 M1 后续进展（M1 功能验收通过、策略有效性未证明、样本外移交 M6-08，详见 `REVIEW_M1.md`）；版本同步 SPEC v1.5、TASKS v1.6、DOC_INDEX v1.5 | pm-quant |
| v1.5 | 2026-10-25 | **M1 修订指针更新**：§10.1 更新为不复权基线结论（仅 ma_cross/ml_ranker 略好、无显著 alpha、ML OOS AUC 0.4456 弱于随机）；版本同步 SPEC v1.6、TASKS v1.7、DOC_INDEX v1.6、REVIEW_M1 v1.1 | pm-quant |
