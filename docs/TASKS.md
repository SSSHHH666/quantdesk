# QuantDesk 任务分解与派单计划（TASKS）

> 版本：v1.10（见文末变更记录）
> 日期：2026-10-26
> 作者：PM（pm-quant）
> 依据：`BRIEF.md` **v0.3**（F1–F15）、`docs/SPEC.md` **v1.7**（功能与验收口径）
> **v1.10 变更**：①**用户决定恢复 M6（2026-10-26）**，目标=短线选股/追求可验证统计边际；②M6-01…M6-17 状态更新（17 项已完成打勾，M6-13 🟡 进行中）；③新增恢复后 P0 子任务 **M6-18 扩样本数据（Dev1）/ M6-19 标签矩阵（Dev2）/ M6-20 特征扩充（Dev2）/ M6-21 GBDT 排序（Dev2）/ M6-22 研究 QA（QA）**；④任务 80→85，M6 P0 改为恢复串行顺序（数据→标签/特征→模型→OOS→是否接入）；⑤接续提示词 `docs/M6_HANDOFF_PROMPT.md`。
> 用途：Lead 按本表派单；Dev/QA/Integrator 按本表领取任务；任何**范围、文件归属、契约**变更须经 Lead 审批并由 PM 更新本表。

---

## 0. 使用说明

### 0.1 任务表字段

| 字段 | 含义 |
|---|---|
| 编号 | `M<里程碑>-<序号>`，全局唯一；团队任务板标题建议以此为前缀 |
| 模块 | **取“文件”列路径的一级目录**（`core`/`data`/`strategy`/`backtest`/`trading`/`automation`/`ui`/`tests`/`scripts`/`docs`），与 BRIEF 第 5 节模块契约一致；跨模块任务（如集成修复）在“文件”列单独注明 |
| 文件 | 本任务**允许写入**的文件（严格按 BRIEF 第 5 节目录；不得改其它文件） |
| 依赖 | `硬`= 前序任务代码必须存在才能开工；`软`= 契约已冻结即可并行开发，集成时对齐 |
| 角色 | Dev1（基础设施+数据）/ Dev2（策略+回测）/ Dev3（交易+自动化+UI）/ QA / Integrator |
| 复杂度 | S≈0.25 天、M≈0.5 天、L≈0.5–1 天（**L 允许分两次提交，但仍属同一任务编号**，保证文件不冲突） |
| 契约 | ⭐ 表示该任务产出/修改**冻结契约**（见第 2 节），完成后不得擅自改签名 |

### 0.2 任务粒度与完成定义

1. 每个任务 1–3 个文件、可独立完成、可独立验证；超过 1 天的必须拆（Lead 与 PM 商定后新增编号）。
2. “完成”= 代码可运行 + 对应用例通过 + 错误路径有中文提示 + 不越界改文件（详见 `SPEC.md` §12）。
3. 每个任务结束在 `send_message` 里报：任务编号、改动文件、自测命令与结果、遗留问题。
4. 文件归属是**硬约束**：`ui/pages/*` 各页面互相独立；同一文件的多任务必须串行（见依赖列）。

### 0.3 里程碑进入/退出条件

| 里程碑 | 进入条件 | 退出条件（可演示） |
|---|---|---|
| M0 骨架 | — | `streamlit run app.py` 启动，9 页占位可切换；DB 建表成功；配置/日志/事件可用；pytest 骨架通过 |
| M1 研究闭环 | M0 退出 | 3 策略 × 自选池跑出完整回测报告；A 股规则与费用精确用例全绿；数据幂等与校验全绿 |
| M2 交易闭环 | M1 退出 | 手动买/卖/撤单全通；T+1 拦截正确；20 个风控原因码全覆盖；持仓与资金曲线与手算一致 |
| M3 自动化 | M2 退出 | 调度器一键启停；3 个 job 可手动触发；信号→风控→订单 trace 完整；通知至少一种可用；设置页可改风控参数并即时生效 |
| M4 交付 | M3 退出 | 断网 Mock 端到端冒烟通过；`scripts/start.bat` 一键启动；RUNBOOK/THIRD_PARTY 完成；P0/P1 清零 |
| M5 打磨 | M4 退出 | 9 页视觉统一；回测报告可导出；示例数据+演示脚本可跑；用户手册完成；G1–G6 达标 |
| **M6 策略研究增值（Phase 2）** | **M1 研究闭环退出 + M2 模拟盘可用 + Lead 批准 Phase 2 契约扩展** | **F10–F13 全部可演示**：远期/近期两榜每条可解释（≥2 理由）且展示回测区间+样本外+UBA；异动雷达阈值可配；自选池分组/导入导出可用；walk-forward 样本外强制、参数稳健性风险灯；`RQ_WARN_NO_BENCHMARK` 正确展示；推荐默认不自动下单；AC-F10…F13 全绿 |

---

## 1. 总览

### 1.1 任务数量与分布

| 里程碑 | 主题 | 任务数 | Dev1 | Dev2 | Dev3 | QA | Integrator |
|---|---|---:|---:|---:|---:|---:|---:|
| M0 | 骨架 | 10 | 6 | 0 | 1 | 1 | 2 |
| M1 | 研究闭环 | 16 | 4 | 8 | 0 | 3 | 1 |
| M2 | 交易闭环 | 12 | 0 | 0 | 10 | 2 | 0 |
| M3 | 自动化 | 10 | 0 | 0 | 8 | 2 | 0 |
| M4 | 交付 | 8 | 0 | 1 | 2 | 1 | 4 |
| M5 | 打磨 | 7 | 0 | 1 | 2 | 1 | 3 |
| **M6（Phase 2）** | **策略研究增值 + 全市场筛选 + 恢复后研究（数据/标签/模型/OOS）** | **22** | **3** | **12** | **3** | **3** | **1** |
| **合计** | — | **85** | **13** | **22** | **26** | **13** | **11** |

复杂度分布：S 11 项、M 52 项、L 22 项；估算总工作量 ≈ 51–56 人日（MVP ≈ 36–40 + M6 ≈ 18.5；L 按 0.7 人日、M 0.5、S 0.3 计），3 名 Dev 并行 + QA/Integrator 穿插，MVP 预计 **3–4 周**，M6 恢复后预计 **2–3 周**（P0 串行：数据→标签/特征→模型→OOS）。

### 1.2 首批可并行开工任务（R1，派单 11 项，均无硬依赖）

> 说明：全表**无硬依赖**的共有 13 项——除下表 11 项外，还有 `M0-10`（QA 测试夹具，软依赖 M0-04）与 `M1-01`（数据源抽象，软依赖 M0-03）。这两项技术上可立即开工，但 M0-10 需要 M0-04 的临时库约定、M1-01 与 Dev1 的 M0-03 抢同一人力，故建议排入 R2；若 Lead 想让 QA 提前介入，可先派 M0-10。

> Lead 可直接按此派单；同一角色的多项按“优先级”顺序执行。

| 编号 | 任务 | 角色 | 优先级 | 说明 |
|---|---|---|---|---|
| M0-03 | 领域模型与全量 ORM 表（`core/models.py`） | Dev1 | **P0 最高** | 后续 80% 任务的软依赖；要求第一轮先交付“交易域 + pydantic 模型”，解锁 M0-04/M1-05/M2-01/M2-04 |
| M0-01 | 依赖清单与默认配置（`requirements.txt`、`config.yaml`） | Dev1 | P1 | 键名必须与 SPEC §7 一致 |
| M0-05 | 结构化日志与审计（`core/logging.py`） | Dev1 | P1 | — |
| M0-06 | 事件总线（`core/events.py`） | Dev1 | P1 | — |
| M1-05 | 策略基类/注册表/参数 schema（`strategy/base.py`） | Dev2 | **P0** | 契约冻结项 CF-3；依赖 M0-03 为软依赖（字段以 SPEC §6.7 为准，可先开发） |
| M1-10 | A 股撮合规则与费用模型（`backtest/broker_sim.py`） | Dev2 | P0 | 契约冻结项 CF-10；**唯一实现**，PaperBroker 复用 |
| M1-12 | 绩效指标（`backtest/metrics.py`） | Dev2 | P1 | 口径按 SPEC §8.3 |
| M2-01 | BrokerAdapter 抽象与注册表（`trading/broker.py`） | Dev3 | P0 | 契约冻结项 CF-4 |
| M2-04 | 风控闸门 RiskGate（`trading/risk.py`） | Dev3 | P0 | 契约冻结项 CF-5；纯函数，可脱离其它模块开发 |
| M0-08 | 启动脚本与环境自检（`scripts/start.bat`、`scripts/check_env.py`） | Integrator | P1 | 不阻塞任何人 |
| M4-04 | 第三方与许可登记（`docs/THIRD_PARTY.md`） | Integrator | P2 | 依赖 Scout 的 `docs/borrow_map.json`（软），可先搭表结构 |

**R2 起的关键解锁顺序（建议）**

```
M0-03 → M0-04/M0-09/M0-10 → M1-01 → M1-03 → M1-04/M1-02 → …
M1-05 → M1-06 → M1-07/08/09（三策略可并行）；M1-05 + M1-10 → M1-11 → M4-03
M2-01 + M2-04 + M2-05 → M2-06 → M2-07/M2-08 → M2-02（PaperBroker 等 M1-10）→ M2-03
M2-06 + M1-03 → M3-02 → M3-03 → M3-06/M3-09
```

关键路径：`M0-03 → M0-04 → M2-05 → M2-06 → M3-02 → M4-06 → M4-07`。**M0-03 是全局瓶颈，Dev1 必须最先交付并立即通知 Lead。**

**M6（Phase 2）首批与关键路径**（入口：M1 验收通过；**契约扩展已由 Lead 批准 2026-10-25**）：

> **当前 M6 P0（2026-10-26 恢复后串行）**：~~M6-01/M6-08/M6-13/M6-14/M6-15（均已收口）~~ → **恢复研究串行：M6-18（扩样本数据，Dev1）→ M6-19+M6-20（标签/特征，Dev2）→ M6-21（GBDT 排序，Dev2）→ M6-22（研究 QA，QA）→ 是否接入（用户+Lead 决策）**；任一步不达标不得进入下一步。

```
P0: M6-01（5 张表+迁移） + M6-08（walk-forward） + M6-13（短线模型 v2）
    + M6-14（全市场筛选）+ M6-15（筛选器有效性验证）
```
M6-01 → M6-02（自选池数据层）→ M6-09（自选池页）
M6-01 → M6-03（推荐引擎）→ M6-06（异动雷达）→ M6-10（推荐页）
M6-04/M6-05（两条线的策略）可与 M6-03 并行
M6-07（ML）已获 OQ-7 批准（sklearn 可选依赖）；M6-13 复用 M6-07/M6-08
```

M6 关键路径：`M6-01 → M6-03 → M6-08 → M6-10`；**短线目标关键路径：`M6-13 ← M6-08 + M6-07`**。**M6-01/M6-08/M6-13 是 Phase 2 当前 P0 瓶颈。**

---

## 2. 契约冻结项（Change Freeze：MVP 13 项 + Phase 2 扩展待批）

> 冻结含义：**方法名、参数顺序、返回字段名、枚举值、表名/关键字段、配置键名、事件主题、原因码**不得擅自改动。实现细节可自由优化。
> 变更流程：提出方 → Lead 审批 → PM 更新 `SPEC.md`/`TASKS.md` 版本号 → 通知所有受影响角色 → 同步改测试。未走流程的改动在集成评审中一律退回。
> **Phase 2 扩展（M6，**已由 Lead 批准 2026-10-25**，按 CF 冻结执行）**：新增 5 张表（`watchlist_groups`/`watchlist_items`/`recommend_runs`/`recommend_items`/`research_runs`，SPEC §5.4，**扩展新增、不改已有 22 表**）、4 组服务接口（`data/watchlist.py`、`strategy/recommend.py`、`strategy/ml_ranker.py`、`backtest/validation.py`，SPEC §6.8）、配置键 `recommend.*`/`research.*`（SPEC §7）、事件主题 `recommend.updated`/`watchlist.changed`、警告码 `RQ_WARN_NO_BENCHMARK`。**已冻结**：名称/签名/字段不得擅自改动；MVP 13 项冻结项不变。

| 编号 | 冻结内容 | 契约文件 | 冻结任务 | 主要影响方 |
|---|---|---|---|---|
| CF-1 | `DataProvider` 6 个方法签名与返回列（含 `get_bars` 列顺序） | `data/provider.py` | M1-01 | Dev1/Dev2/Dev3/QA |
| CF-2 | `BarCache.update/load/validate/latest_date` 语义与返回值 | `data/cache.py` | M1-03 | Dev1/自动化和 UI |
| CF-3 | `Strategy` 基类、`@register`、`params_schema()`、`Context`、`Signal` 字段 | `strategy/base.py` | M1-05 | Dev2/Dev3/QA |
| CF-4 | `BrokerAdapter` 4 个方法签名与 `OrderResult` 字段 | `trading/broker.py` | M2-01 | Dev3/QA |
| CF-5 | `RiskGate.check(order, account, positions, market) -> RiskDecision` + 判定顺序 + RK001–RK020 | `trading/risk.py` | M2-04 | Dev3/QA |
| CF-6 | `OrderService.place/preview/cancel` 为**唯一**下单入口 | `trading/order_manager.py` | M2-06 | Dev3/QA |
| CF-7 | 22 张表名、关键字段、枚举值（SPEC §5）、订单状态机 | `core/models.py` | M0-03 | 全员 |
| CF-8 | 事件主题名与 payload 关键字段（SPEC §3.3） | `core/events.py` + 各发布者 | M0-06 | 全员 |
| CF-9 | `config.yaml` 键名与默认值（SPEC §7） | `config.yaml` | M0-01 | 全员 |
| CF-10 | A 股撮合规则与 `FeeModel` **只实现一次**（`backtest/broker_sim.py`），PaperBroker 复用 | `backtest/broker_sim.py` | M1-10 | Dev2/Dev3/QA |
| CF-11 | 日志字段、`trace_id` 贯穿约定、审计动作名 | `core/logging.py` | M0-05 | 全员 |
| CF-12 | UI 交互规范：二次确认、中文错误卡片（原因码+建议）、金额/涨跌颜色格式 | `ui/pages/_common.py` | M0-07 | Dev3 |
| CF-13 | 测试约定：临时库 fixture 名、pytest marker（`unit/integration/e2e`）、样本数据工厂 | `tests/conftest.py` | M0-10 | QA/全员 |

---

## 3. 任务表

### 3.1 M0 骨架（10 项）

> 目标：能启动、能建库、能记录、能给后续所有任务一个干净的地基。

| 编号 | 任务 | 文件 | 依赖 | 角色 | 复杂度 | 契约 | 验收标准 |
|---|---|---|---|---|---|---|---|
| M0-01 | 依赖清单与默认配置 | `requirements.txt`、`config.yaml` | 无 | Dev1 | S | ⭐CF-9 | ① 按清单可在项目 `.venv` 安装（akshare/APScheduler 可标注“待装”）；② `config.yaml` 键与 SPEC §7 完全一致、含注释；③ 可选依赖（ccxt/TA-Lib）缺失有说明 |
| M0-02 | 配置加载与校验 | `core/config.py` | M0-01 硬 | Dev1 | S | ⭐CF-9 | ① 读取 YAML + 环境变量覆盖（`QD_` 前缀）；② pydantic 校验，非法值中文报错并指出字段；③ 单测覆盖默认/覆盖/非法三种路径 |
| M0-03 | 领域模型与全量 ORM 表 | `core/models.py` | 无 | Dev1 | L | ⭐CF-7 | ① 22 张表按 SPEC §5 建（PK/唯一/索引齐全）；② 交易域 pydantic 模型（Side/Order/OrderRequest/OrderResult/Fill/RiskDecision/Account/Position/Bar 等）齐全；③ **第一轮先提交交易域 + 模型**并通知 Lead；④ 单测：建表成功、枚举值、必填校验 |
| M0-04 | DB 引擎/会话/建表/迁移/备份 | `core/db.py` | M0-03 硬 | Dev1 | M | ⭐CF-7 | ① 引擎与会话工厂、WAL、外键开启；② `create_all` 幂等 + 轻量迁移位（版本表）；③ 在线备份函数与保留策略；④ 启动自检（可读写/表存在/损坏检测）；⑤ 单测用临时文件库 |
| M0-05 | 结构化日志与审计写入 | `core/logging.py` | 无 | Dev1 | M | ⭐CF-11 | ① JSON Lines 按日落到 `runtime/logs/`，控制台人读格式；② 每条含 ts/level/logger/trace_id/message；③ `write_audit(action, target, before, after, actor, trace_id)` 写 `audit_logs`；④ 密码/token 过滤；⑤ 单测 |
| M0-06 | 事件总线 | `core/events.py` | 无 | Dev1 | M | ⭐CF-8 | ① subscribe/publish/unsubscribe；② 同步分发、单订阅者异常隔离并记日志；③ 主题常量集中定义（SPEC §3.3）；④ 单测：多订阅者、异常隔离、取消订阅 |
| M0-07 | Streamlit 入口与页面动态发现 + 共用工具 | `app.py`、`ui/pages/_common.py` | M0-02、M0-04 硬 | Dev3 | M | ⭐CF-12 | ① `streamlit run app.py` 启动，侧边栏含应用名/账户/数据源/调度器/离线提示；② **动态发现** `ui/pages/*.py`（约定 `PAGE_TITLE` + `render()`），9 个导航项齐全；③ 未实现页面自动显示“建设中”占位（后续页面任务只建自己的页面文件，不改 `app.py`）；④ `_common.py` 提供 `render_error_card`/`money`/`pct`/`status_bar`；⑤ 冷启动 ≤ 15s |
| M0-08 | 启动脚本与环境自检 | `scripts/start.bat`、`scripts/check_env.py` | 无 | Integrator | S | — | ① 双击 `start.bat` 可启动（优先 `.venv\Scripts\python.exe`，回退 `D:\Anaconda3\python.exe`）；② `check_env.py` 输出 Python 版本、关键依赖版本、缺失列表（中文）；③ 缺依赖时给出安装命令 |
| M0-09 | 初始化与迁移脚本 | `scripts/init_db.py` | M0-04 硬 | Integrator | S | — | ① 一键建库建表；② 已存在时幂等（不破坏数据）；③ `--reset` 需输入确认词并先自动备份；④ 输出中文结果摘要 |
| M0-10 | 测试骨架与夹具 | `tests/conftest.py` | M0-04 软 | QA | M | ⭐CF-13 | ① 临时 SQLite fixture（每用例隔离）；② 样本数据工厂（bars/账户/持仓/订单）；③ pytest marker `unit/integration/e2e`；④ `pytest -q` 可运行（0 用例通过也算通过但需有示例） |

### 3.2 M1 研究闭环（16 项）

> 目标：数据能自动进库、3 个策略能跑、回测结果可信且可对比。

| 编号 | 任务 | 文件 | 依赖 | 角色 | 复杂度 | 契约 | 验收标准 |
|---|---|---|---|---|---|---|---|
| M1-01 | 数据源抽象、注册表与 MockProvider | `data/provider.py` | M0-03 软 | Dev1 | M | ⭐CF-1 | ① `DataProvider` 抽象 6 方法；② `ProviderRegistry` 注册/获取/主备 resolve；③ `MockProvider` 确定性示例数据（≥10 标的 × 3 年，固定随机种子）；④ `health_check` 返回 OK/DEGRADED/DOWN；⑤ 无网可跑；单测覆盖 |
| M1-02 | akshare 主源接入与降级 | `data/provider.py` | M1-01 硬 | Dev1 | L | ⭐CF-1 | ① `AkshareProvider` 实现日线/指数/交易日历/基本面/快照；② 限流间隔 + 重试（指数退避）；③ 异常返回空表而非崩溃；④ 主源失败自动降级 Mock 并写日志告警；⑤ 字段中文映射与单位（股/元）正确 |
| M1-03 | 增量缓存与复权 | `data/cache.py` | M1-01 硬、M0-04 硬 | Dev1 | L | ⭐CF-2 | ① `update` 增量（按已有最大日期续拉）、upsert 幂等；② `load` 支持 hfq/qfq/none 复权；③ 落 `adj_factor` 与 `adjust_events`；④ 返回 UpdateReport(added/updated/skipped)；⑤ AC-F1-01 |
| M1-04 | 数据质量校验报告 | `data/cache.py` | M1-03 硬 | Dev1 | M | ⭐CF-2 | ① 检测 DQ001–DQ007 全部写入 `data_quality_issues`；② `ValidationReport` 含严重度与建议；③ AC-F1-02；④ 不影响主流程（只报告不阻断） |
| M1-05 | 策略基类/注册表/参数 schema | `strategy/base.py` | 无（M0-03 软） | Dev2 | M | ⭐CF-3 | ① `Strategy`/`@register`/`get_strategy`/`list_strategies`；② `params_schema()` 规范（type/default/min/max/step/label/help）；③ `Context`/`Signal` 字段与 SPEC §6.2 一致；④ 策略禁止 DB/网络（代码审查）；⑤ AC-F2-01/03 |
| M1-06 | 指标兼容层（TA-Lib / pandas 兜底） | `strategy/base.py` | M1-05 硬 | Dev2 | M | ⭐CF-3 | ① 提供 `sma/ema/rsi/macd/std/atr` 等同名函数；② 优先 TA-Lib，ImportError 时用 pandas 实现；③ 两种实现结果容差 ≤1e-6；④ AC-F2-04；⑤ 暴露当前使用的是哪套实现（设置页读取） |
| M1-07 | 双均线策略 | `strategy/builtin/ma_cross.py` | M1-06 硬 | Dev2 | S | — | ① `@register("ma_cross")`；② 金叉买/死叉卖、`position_pct` 控制仓位；③ 数据不足时跳过并有 INFO；④ 信号可复现；⑤ 单测 1 正例 1 反例 |
| M1-08 | RSI 反转策略 | `strategy/builtin/rsi_reversal.py` | M1-06 硬 | Dev2 | S | — | ① `@register("rsi_reversal")`；② 超卖买/超买卖、可配阈值与周期；③ 单测；④ 与文档参数一致 |
| M1-09 | 多因子打分策略 | `strategy/builtin/multi_factor.py` | M1-06 硬 | Dev2 | M | — | ① `@register("multi_factor")`；② 动量 z-score − 波动率 z-score 排序取 top_n 等权；③ 按 `rebalance_days` 调仓；④ 同 bar 同标的不重复信号；⑤ 单测 |
| M1-10 | A 股撮合规则与费用模型 | `backtest/broker_sim.py` | M1-05 软 | Dev2 | L | ⭐CF-10 | ① `AStockRules.price_limits`（含 ST/创/科/主板幅度、四舍五入到分）；② `validate_lot`（100 股整手/科创板 200 起）；③ T+1 可卖校验；④ 停牌/一字板/涨跌停不可成交判定；⑤ `FeeModel.calculate` 五项费用四舍五入到分；⑥ `SimBroker.match`；⑦ AC-F3-01/03/04 |
| M1-11 | 事件驱动回测引擎 | `backtest/engine.py` | M1-06 硬、M1-10 硬 | Dev2 | L | ⭐CF-10 | ① 逐交易日循环：T+1 开盘撮合 → 估值 → `on_bar` → 生成次日订单 → 权益；② 支持多标的、现金/持仓状态；③ 输出 `BacktestResult(metrics/equity/trades/warnings/run_id)`；④ 警告项（未复权/样本短/交易少/停牌多）；⑤ AC-F3-06/07/08；⑥ **价格口径（Lead 裁决 2026-10-25，SPEC §4.6/§8.3）**：撮合/手数/资金核算一律用不复权真实价，复权仅用于绩效展示与分红处理；hfq 合成价不得用于手数与资金核算；**配置键 `backtest.price_adjust`（默认 `none`；`hfq`=合成价全流程+自动告警+关闭最小手数校验，仅供研究对照）**；⑦ 三个新警告码：`RQ_WARN_MOCK_DATA`/`RQ_WARN_PRICE_JUMP`/`RQ_WARN_POSITION_MIN_LOT`（警告非阻断） |
| M1-12 | 绩效指标计算 | `backtest/metrics.py` | M0-03 软 | Dev2 | M | — | ① 指标键齐全（total/annual return、**max_drawdown（负值）**、**max_drawdown_abs（正值）**、sharpe、win_rate、profit_factor、trade_count、volatility、calmar）；② 除零与无交易安全（`calmar` 除零返回 None 且 `notes` 中文说明）；③ 口径与 SPEC §8.3（v1.1）一致；④ AC-F3-05 |
| M1-13 | 离线示例数据集 | `scripts/fetch_sample_data.py` | M1-03 硬 | Integrator | M | — | ① 生成/落地固定样例数据到 `runtime/sample/`（≥10 标的 × 3 年，含除权事件与停牌样本）；② 无网可执行；③ 中文输出摘要；④ 供演示与 QA 复用 |
| M1-14 | 数据层测试 | `tests/test_data_layer.py` | M1-03、M1-04 | QA | M | — | ① AC-F1-01…04 全部覆盖；② 幂等、降级、校验、空标的；③ 不依赖网络（monkeypatch） |
| M1-15 | A 股规则与回测测试 | `tests/test_broker_sim.py` | M1-10、M1-11 | QA | M | — | ① AC-F3-01…04；② 费用精确数值（容差 0.01）；③ 涨跌停/一字板/整手/科创板 200 股；④ T+1 卖出被拒；⑤ **卖出无持仓/未传 positions fail-closed（P0-1 回归）** |
| M1-16 | 指标测试 | `tests/test_metrics.py` | M1-12 | QA | S | — | ① 手工构造权益序列，逐项核对（容差 1e-6），断言 `max_drawdown` 为**负数**、`max_drawdown_abs` 为**正值**、`calmar=annual/\|mdd\|`；② 无交易/零回撤/单日数据边界（零回撤时 `max_drawdown=0.0`、`calmar=None`、`notes` 有中文说明）；③ 胜率与盈亏比口径验证 |

### 3.3 M2 交易闭环（12 项）

> 目标：手动能买卖、账户算得对、风控管得住、所有下单只有一条路。

| 编号 | 任务 | 文件 | 依赖 | 角色 | 复杂度 | 契约 | 验收标准 |
|---|---|---|---|---|---|---|---|
| M2-01 | BrokerAdapter 抽象与注册表 | `trading/broker.py` | 无（M0-03 软） | Dev3 | M | ⭐CF-4 | ① `BrokerAdapter` 4 方法 + 可选 `get_order/is_available`；② `broker_registry` 注册/获取；③ 未配置 broker 中文提示；④ 单测用假适配器验证契约 |
| M2-02 | PaperBroker 完整实现 | `trading/broker.py` | M2-01、M1-10、M2-05 硬 | Dev3 | L | ⭐CF-4 | ① 账户/持仓内存态 + 通过 `PortfolioService` 落库；② 即时成交（快照价±滑点）、费用复用 `FeeModel`；③ `cancel_order` 正确；④ 与回测口径一致（SPEC §4.10）；⑤ AC-F9-01 |
| M2-03 | CcxtBroker(testnet) 与 QmtBroker 骨架 | `trading/broker.py` | M2-01 硬 | Dev3 | M | ⭐CF-4 | ① 未安装 ccxt 或未配 key 时 `is_available()=False`，UI 提示不崩溃；② CcxtBroker 最小可用集（下单/撤单/持仓/账户）在 testnet 可跑（允许因无 key 跳过并说明）；③ QmtBroker 全方法抛 `BR004` 中文错误；④ 无任何真实资金/网络副作用（单测断言） |
| M2-04 | 风控闸门 RiskGate | `trading/risk.py` | 无（M0-03 软） | Dev3 | L | ⭐CF-5 | ① 10 步判定顺序固定，返回首个失败码；② RK001–RK020 全部实现；③ 纯函数（无 IO/随机）；④ `checks` 明细含实际值/阈值；⑤ fail-closed；⑥ **金额类校验（RK012：`min_order_amount` / `max_single_order_value_pct`）仅约束买入，卖出豁免**；⑦ **自动交易授权 `allow_auto_orders`（默认 false → AUTO 单 RK020）与关键数据缺失 fail-closed（RK005/RK006/RK015）**；⑧ AC-F6-01…11 的单元级验证 |
| M2-05 | 账户/持仓/T+1/资金曲线 | `trading/portfolio.py` | M0-03、M0-04 硬 | Dev3 | L | ⭐CF-7 | ① `apply_fill` 精确更新持仓/可用/成本/现金/流水；② `settle_new_day` 做 T+1 日初结算；③ `snapshot_equity` 幂等；④ `reconcile` 启动对账（持仓=成交净额、现金=流水）；⑤ 金额误差 0（分为单位） |
| M2-06 | 订单服务（唯一入口） | `trading/order_manager.py` | M2-01、M2-04、M2-05 硬 | Dev3 | L | ⭐CF-6 | ① `place` 内部**必调** RiskGate（可通过 mock 断言）；② `preview` 只读返回风控结论+费用+成交后仓位；③ `cancel`；④ 状态机流转与非法流转拒绝；⑤ 写 `orders/trades/cash_flows/equity_curve` + 审计；⑥ 发事件（submitted/filled/rejected/risk.blocked）；⑦ AC-F6-01 |
| M2-07 | 订单幂等与崩溃恢复对账 | `trading/order_manager.py` | M2-06 硬 | Dev3 | M | — | ① `client_order_id` 唯一约束 + 重复提交返回同一结果；② 启动时未完成订单置 `EXPIRED` 并通知；③ 对账失败给出中文建议；④ AC-F4-04 |
| M2-08 | 手动交易页 | `ui/pages/manual_trade.py` | M2-06 硬 | Dev3 | M | ⭐CF-12 | ① 标的搜索/方向/价格/数量控件，价格显示涨跌停区间；② 风控预检面板（金额/费用明细/仓位占比/结论+建议）；③ 二次确认；④ 今日委托表可撤单；⑤ 错误中文卡片含 RK 码；⑥ AC-F7-02③ |
| M2-09 | 持仓页 | `ui/pages/positions.py` | M2-05 硬 | Dev3 | M | ⭐CF-12 | ① 账户摘要 + 持仓表（可用/T+1 冻结/成本/市值/浮盈/占比/状态）；② 资金曲线；③ 一键清仓（二次确认、逐单走风控）；④ 导出 CSV；⑤ 空态与对账不一致提示 |
| M2-10 | 总览页 | `ui/pages/overview.py` | M2-05、M1-12 | Dev3 | M | ⭐CF-12 | ① 4 项状态条；② 6 个 KPI 卡；③ 权益曲线+回撤；④ 持仓 Top5/资产饼图；⑤ 今日信号与订单表；⑥ 最近告警；⑦ 点击跳转联动 |
| M2-11 | 风控测试 | `tests/test_risk.py` | M2-04 硬 | QA | M | — | ① RK001–RK020 每码 ≥1 用例；② 判定顺序用例（多条件同时不满足时返回首个码）；③ 纯函数（两次调用一致、无 DB 写）；④ fail-closed；⑤ 仓位/熔断边界（19.9%/20%/20.1%）；⑥ **AC-F6-09 金额类仅约束买入**：卖出金额超单笔上限仍通过、同额买入被 RK012 拒绝；⑦ **AC-F6-10/11**：默认 AUTO 单 RK020 / 显式授权放行 / 关键数据缺失 RK005/RK006/RK015；⑧ P0-2 回归用例（默认构造与显式配置两种 Gate 行为一致） |
| M2-12 | 模拟盘订单测试 | `tests/test_paper_broker.py` | M2-02、M2-06 | QA | M | — | ① AC-F4-01…06；② 现金/持仓/流水/权益曲线一致性；③ 撤单与不可撤；④ 重置账户；⑤ 并发重复提交只成交一次 |

### 3.4 M3 自动化（10 项）

> 目标：一键启停、定时执行、信号→风控→订单全链路可追溯、通知可用。

| 编号 | 任务 | 文件 | 依赖 | 角色 | 复杂度 | 契约 | 验收标准 |
|---|---|---|---|---|---|---|---|
| M3-01 | 调度器封装 | `automation/scheduler.py` | M0-06 | Dev3 | M | ⭐CF-8 | ① `start/stop/is_running/status/trigger_now`；② 重复 start 幂等；③ 时区 `Asia/Shanghai`、`misfire_grace_time`；④ 启停写审计；⑤ AC-F5-01 |
| M3-02 | 盘前/盘中/盘后 job | `automation/jobs.py` | M3-01、M2-06、M1-03 | Dev3 | L | ⭐CF-8 | ① 三个 job 各自可独立调用并返回 `JobResult`；② 交易日判定（非交易日 SKIPPED 不报错）；③ 盘前日初结算+基线；④ 盘中信号→订单；⑤ 盘后盈亏+曲线+日报；⑥ 单策略异常不影响其它；⑦ 写 `job_runs`；⑧ AC-F5-04/05 |
| M3-03 | 信号→风控→订单管线串联 | `automation/jobs.py` | M3-02、M2-04 | Dev3 | M | ⭐CF-8 | ① 每信号生成 trace_id 并贯穿；② 幂等键 `hash(instance,symbol,date,side)`；③ 风控拦截写 `strategy_signals.status=RISK_BLOCKED` + `risk.blocked`；④ 批量处理不因单标的失败中断；⑤ AC-F5-02/03 |
| M3-04 | 通知渠道 | `automation/notify.py` | M0-05 | Dev3 | M | ⭐CF-8 | ① DESKTOP/EMAIL/WEBHOOK 三渠道，开关与配置可关；② 失败重试 1 次后记 `notifications`，不影响主流程；③ 密码从环境变量读；④ 发送记录落库；⑤ AC-F8-03/04 |
| M3-05 | 策略页 | `ui/pages/strategies.py` | M1-05、M2-06 | Dev3 | M | ⭐CF-12 | ① 实例列表 + 新建（按 `params_schema` 动态表单）；② 参数校验中文错误；③ 模式（关闭/仅信号/模拟盘自动）；④ 启停写审计；⑤ “预览信号”最近 60 日只展示不下单；⑥ 名称唯一。**页面文件由 `app.py` 动态发现（D-9），文件名以 `PAGE_TITLE="策略"` 为准（`strategies.py`；按 `PAGE_TITLE` 去重）** |
| M3-06 | 自动化页 | `ui/pages/automation.py` | M3-01、M3-02 | Dev3 | M | ⭐CF-12 | ① 总开关（二次确认）；② 3 个任务卡（时间、下次运行、上次结果、立即运行）；③ 今日管线统计；④ 最近 10 次 job_runs；⑤ 测试通知按钮；⑥ AC-F5-01 |
| M3-07 | 设置页 | `ui/pages/settings.py` | M2-04、M3-04、M0-04 | Dev3 | L | ⭐CF-12 | ① 6 个 Tab（数据源/风控/通知/存储备份/环境/关于）；② 风控参数保存即时生效 + 审计 before/after + 恢复默认；③ 备份/恢复（恢复前自动备份 + 确认词）；④ 依赖检查与 TA-Lib/ccxt 状态；⑤ AC-F6-08 |
| M3-08 | 日志页 | `ui/pages/logs.py` | M0-05、M1-04 | Dev3 | M | ⭐CF-12 | ① 4 个 Tab（运行日志/审计/数据校验/通知）；② 级别+时间+关键词+trace_id 过滤；③ 导出 CSV；④ 空态与路径提示；⑤ AC-F8-01/05 |
| M3-09 | 自动化管线测试 | `tests/test_automation_pipeline.py` | M3-02、M3-03 | QA | M | — | ① AC-F5-02…05；② Mock 时间/时钟，不真等待；③ 不启动真实调度线程（用 trigger_now 替代）；④ 幂等与异常隔离 |
| M3-10 | 通知测试 | `tests/test_notify.py` | M3-04 | QA | S | — | ① 开关生效；② 单渠道失败不影响其它；③ 密码/token 不出现在日志与 DB；④ 配置错误中文提示 |

### 3.5 M4 交付（8 项）

> 目标：断网也能演示、一键能启动、文档齐全、集成缺陷清零。

| 编号 | 任务 | 文件 | 依赖 | 角色 | 复杂度 | 契约 | 验收标准 |
|---|---|---|---|---|---|---|---|
| M4-01 | 行情页 | `ui/pages/market.py` | M1-03、M1-06 | Dev3 | M | ⭐CF-12 | ① K 线 + MA/成交量 + 指标副图；② 复权切换（前/后/不复权）；③ 数据质量标记（停牌/缺失/ST）；④ 加入自选；⑤ “去回测/去下单”带入；⑥ AC-F7-02① |
| M4-02 | 回测页 | `ui/pages/backtest.py` | M1-11、M1-12 | Dev3 | L | ⭐CF-12 | ① 参数区（策略/标的/区间/资金/费率/滑点/基准）；② 运行与进度；③ KPI 卡 + 权益/回撤曲线 + 月度热力图；④ 交易明细表+CSV；⑤ 历史回测与 2 组对比；⑥ 警告区（RQ_WARN_*）；⑦ AC-F3-07、AC-F7-02② |
| M4-03 | 回测结果持久化与对比查询 | `backtest/engine.py` | M1-11、M0-04 | Dev2 | M | ⭐CF-5 | ① 回测完成写 `backtest_runs/equity/trades`；② 可按 run_id 读回完整结果；③ 提供对比查询（指标差值与曲线对齐）；④ 失败不写半截数据 |
| M4-04 | 第三方依赖与借鉴代码登记 | `docs/THIRD_PARTY.md` | M0-01 软、`docs/borrow_map.json` 软 | Integrator | S | — | ① 每个依赖：名称/版本/许可/来源 URL/用途/复用或重写；② GPL/AGPL/非商用项明确写“仅重写思路”；③ 与 BORROW_MAP 一致；④ 免责声明 |
| M4-05 | 备份/恢复脚本与运维手册 | `scripts/backup_db.py`、`scripts/restore_db.py`、`docs/RUNBOOK.md` | M0-04、M3-01 | Integrator | M | — | ① 备份/恢复可实操演练（恢复后数据完整）；② RUNBOOK 覆盖：安装、启动、常见错误（数据源/TA-Lib/端口/DB 锁）、备份恢复、日志位置、停止自动化；③ 中文分步。**当前状态：文档部分（`docs/RUNBOOK.md` v1.2）已由交付文档维护交付；`backup_db.py`/`restore_db.py` 脚本未交付 → 保持“未交付”，负责人 Integrator，安排后续轮次** |
| M4-06 | 离线端到端冒烟 | `tests/e2e_smoke.py` | M2-*、M3-* | QA | L | ⭐CF-13 | ① 一条命令跑通：Mock 数据→增量写入→回测→手动下单→持仓→job 手动触发→日志/审计；② 断网（禁网络）可跑；③ AC-F1-03、AC-F7-02；④ 失败定位到步骤 |
| M4-07 | 集成装配与缺陷修复 | 跨模块（**需 Lead 批准并记录**；优先修 `app.py`/`scripts/`/服务层胶水） | M4-06 | Integrator | L | — | ① 启动→回测→下单→自动化全链路无 P0/P1；② 每次跨文件改动需原 owner 知会 + Lead 批准；③ 修复后回归 `pytest -q` 全绿；④ 输出缺陷清单与修复记录 |
| M4-08 | 依赖锁定与安装验证 | `requirements.txt`（锁定段）、`scripts/check_env.py` | M0-01、M0-08 | Integrator | M | ⭐CF-9 | ① 全新 `.venv` 按锁定版本安装成功；② 可选依赖缺失时系统仍可启动并提示；③ `check_env.py` 通过；④ 记录本机 Python 3.12.7 兼容性结论 |

### 3.6 M5 打磨（7 项）

> 目标：好看、好懂、能导出、能演示、能交付。

| 编号 | 任务 | 文件 | 依赖 | 角色 | 复杂度 | 契约 | 验收标准 |
|---|---|---|---|---|---|---|---|
| M5-01 | UI 视觉统一与图表美化 | `ui/pages/_common.py`、`app.py` | M2-08…M4-02 | Dev3 | M | ⭐CF-12 | ① 统一配色/字体/间距；② 金额千分位、涨红跌绿；③ 图表模板（KPI/曲线/热力图）复用；④ 9 页视觉一致；⑤ 无功能回归 |
| M5-02 | HTML 绩效报告导出 | `backtest/metrics.py` | M1-12 | Dev2 | M | — | ① 生成自包含 HTML（内联 CSS/图表，无 CDN，离线可看）；② 含指标、权益/回撤曲线、交易明细、警告与免责；③ 中文；④ 单测校验生成文件存在且含关键指标 |
| M5-03 | 回测页导出入口 | `ui/pages/backtest.py` | M4-02、M5-02 | Dev3 | S | ⭐CF-12 | ① 一键导出到 `runtime/exports/`；② 展示路径与“打开所在文件夹”；③ 失败中文提示 |
| M5-04 | 一键演示脚本 | `scripts/demo.py` | M1-13、M2-02、M3-02 | Integrator | M | — | ① 一条命令：Mock 数据→回测→打印指标→手动下单→打印持仓→触发盘后 job；② 无网可跑；③ 中文输出，适合演示给用户 |
| M5-05 | 首次运行引导 | `scripts/start.bat` | M0-08、M4-08 | Integrator | M | — | ① 首次运行自动建 `.venv`、装依赖、初始化 DB、启动；② 失败给出中文原因与下一步；③ 二次运行走快速路径（≤5s 启动） |
| M5-06 | 用户手册 | `docs/USER_GUIDE.md` | 全部功能 | Integrator | M | — | ① 从安装到 4 个核心任务的分步说明（含界面元素说明）；② FAQ（数据源失败/TA-Lib/端口占用/自动化误开）；③ 免责与许可入口；④ 与 SPEC 一致。**当前状态：未交付 → 负责人 Integrator（交付文档维护配合），安排后续轮次** |
| M5-07 | 回归与验收清单 | `docs/ACCEPTANCE_CHECKLIST.md`、`tests/test_regression.py` | 全部 | QA | M | — | ① G1–G6 与 SPEC 全部 AC 逐项结果（通过/失败/证据）；② 回归用例覆盖 P0 场景；③ `pytest -q` 全绿；④ 输出最终缺陷统计。**当前状态：验收清单文档（`docs/ACCEPTANCE_CHECKLIST.md` v1.2）已由交付文档维护交付；`tests/test_regression.py` 待 QA 后续轮次** |

### 3.7 M6 策略研究增值（Phase 2，22 项：已完成 17 + 恢复后研究 5）

> 目标：按用户“以赚钱为导向”的目标实现 F10–F13（选股推荐两榜、异动雷达、自选池、策略研究/ML/样本外）；**用户核心目标（2026-10-26 恢复）：短线选股，追求可验证的统计边际**（v1 负结果见 `REVIEW_M1.md` §9 / `SHORT_TERM_MODEL_V1.md`）。
> **前置硬门槛**：M1 研究闭环验收通过（3 策略稳定出报告 + walk-forward 框架可用）且 M2 模拟盘可用；**契约扩展（5 张表/接口/配置键/事件/警告码）已由 Lead 批准（2026-10-25）**；OQ-6/7/8 已拍板（推荐禁止自动下单 / sklearn 可选 / OOS 年化≥10% 且回撤≤15% 为达标口径）。
> **2026-10-26 用户决定恢复 M6**：先修数据/标签/特征，再谈模型；**P0 严格串行**：M6-18 数据扩样 → M6-19 标签矩阵 + M6-20 特征扩充 → M6-21 GBDT 排序 → M6-22 严格 OOS 验证 → 是否接入（用户+Lead 决策）。接续提示词见 `docs/M6_HANDOFF_PROMPT.md`。
> **纪律**：只提供工具与信号；推荐/筛选默认不自动下单；全程 CPU-only，不引入深度学习框架；**未过样本外不得进入推荐或自动化**（合规红线）。

**M6 任务状态（2026-10-26 恢复时）**：
- ✅ **已完成（17 项）**：M6-01～M6-12（表结构/自选池/推荐/策略/雷达/ML/walk-forward/两页面/QA/合规收口）、M6-14～M6-17（筛选引擎/验证/页面/QA）——代码、页面、测试已交付（详见 `REVIEW_FINAL.md` §1/§7）。
- 🟡 **进行中/研究待续（1 项）**：M6-13 短线模型 v2——`strategy/short_term_v2.py` 实验框架已实现（标签×特征×模型矩阵），但**无可用配置**（`SHORT_TERM_MODEL_V1.md` §2）；随 M6-18…M6-22 继续。
- 🆕 **新增（5 项，2026-10-26 恢复后 P0）**：M6-18 扩样本数据（Dev1）、M6-19 标签矩阵（Dev2）、M6-20 特征扩充（Dev2）、M6-21 GBDT 排序（Dev2）、M6-22 研究 QA（QA）。

| 编号 | 任务 | 文件 | 依赖 | 角色 | 复杂度 | 契约 | 验收标准 |
|---|---|---|---|---|---|---|---|
| M6-01 | Phase 2 表结构扩展（`watchlist_groups`/`watchlist_items`/`recommend_runs`/`recommend_items`/`research_runs` + 迁移 `schema_version`） | `core/models.py`、`core/db.py` | M0-04 | Dev1 | M | ⭐CF-7 扩展（已批准 2026-10-25） | ① 5 张表按 SPEC §5.4 建（PK/唯一/索引齐全）；② 迁移幂等且 `schema_version` 递增；③ 既有 22 张表零影响（回归）；④ 单测建表 + 读写往返 |
| M6-02 | 自选池数据层（分组/CRUD/导入导出/代码归一化） | `data/watchlist.py` | M6-01 | Dev1 | M | 新增服务契约（已批准 2026-10-25） | ① 分组增删改 + 同标的多分组；② 代码归一化（`600519`/`600519.SH`/中文名）；③ CSV/文本导入**预览差异**+逐行错误+确认写入；④ 导出回读一致；⑤ `watchlist.changed` 事件 + 审计；⑥ AC-F12-01…05 |
| M6-03 | 推荐引擎（远期/近期两榜 + 可解释理由 + 落库 + 样本外字段） | `strategy/recommend.py` | M6-01、M1-11、M1-12 | Dev2 | L | 新增服务契约（已批准 2026-10-25） | ① 两套榜可独立生成，权重/方法版本化落库；② 每条推荐 ≥2 条理由（因子名+值/分位+贡献值）；③ 榜单附回测区间 + 样本外指标；④ 数据不足剔除并说明；⑤ AC-F10-01…05（与 M6-10 联合验收） |
| M6-04 | 远期价值型策略（估值/质量/成长/低波动） | `strategy/builtin/value_quality.py` | M1-06 | Dev2 | M | CF-3 | ① `@register("value_quality")` + 参数 schema；② 四类因子可配、缺数据跳过；③ 信号可复现；④ 单测 1 正例 1 反例 |
| M6-05 | 近期异动突破策略（量比/换手/振幅/突破/资金） | `strategy/builtin/breakout_abnormal.py` | M1-06 | Dev2 | M | CF-3 | ① `@register("breakout_abnormal")` + 参数 schema；② 规则可配、缺字段跳过并标注；③ 与 F11 雷达分一致；④ 单测 |
| M6-06 | 异动雷达（规则/阈值/去重/雷达分，盘后扫描） | `strategy/recommend.py` | M6-03 | Dev2 | M | 新增服务契约（已批准 2026-10-25） | ① 默认规则与阈值可配；② 事件含命中规则+实际值+阈值；③ 3 日去重（可配）；④ 缺数据规则跳过不伪造；⑤ AC-F11-01…04 |
| M6-07 | ML 排序（CPU / scikit-learn 可选 / 可解释 / 时间序列切分） | `strategy/ml_ranker.py` | M6-03（OQ-7 已批准） | Dev2 | L | 新增服务契约（已批准 2026-10-25） | ① CPU-only、无 DL 框架；② sklearn 缺失时降级并中文提示；③ 时间序列切分禁止打乱；④ `explain()` 输出因子贡献；⑤ 单测（含降级路径）；**⑥ 负结果前提（M1，2026-10-25）：真实数据 walk-forward 6 折 OOS AUC 0.4456（弱于随机）、Mock 0.5106，当前特征/标签无可用 alpha → 本任务须先做标签改造（未来 5/10 日超额收益分位）、截面中性化、purge/embargo 与泄漏复核；OOS AUC 未稳定优于随机（建议 ≥0.55）前不得接入推荐/自动化** |
| M6-08 | walk-forward 样本外验证（purge/embargo/稳健性/OOS 指标） | `backtest/validation.py` | M1-11 | Dev2 | L | 新增服务契约（已批准 2026-10-25） | ① 默认 ≥3 折、按交易日 purge/embargo；② 输出每折 + OOS 曲线 + OOS 汇总；③ 参数 ±20% 稳健性风险灯；④ 可复现（种子/依赖记录）；⑤ AC-F13-01…05；**⑥ 定位：R8/M6 第一任务（Lead 2026-10-25 裁决）；未过样本外的策略/榜单只能标“研究阶段”且禁止进入推荐/自动化** |
| M6-09 | 自选池页（分组/增删改/导入预览/导出） | `ui/pages/watchlist.py` | M6-02 | Dev3 | M | ⭐CF-12 | ① 分组侧栏 + 标的表 + 状态标记；② 导入预览与逐行错误；③ 导出 CSV；④ 非法输入中文提示；⑤ AC-F12-01…03 |
| M6-10 | 推荐页（两榜 + 异动雷达 Tab + UBA + 样本外展示 + 不自动下单） | `ui/pages/recommend.py` | M6-03、M6-06、M6-08 | Dev3 | L | ⭐CF-12 | ① 两榜切换 + 雷达 Tab；② 每条 ≥2 理由 + 因子拆解；③ 回测区间/样本外/风险提示/UBA 强制展示；④ 无“一键自动买入”；⑤ 研究阶段标记；⑥ AC-F10-01…05、AC-F11-01/02、AC-F13-02 |
| M6-11 | QA：推荐可解释/样本外强制/自选池 CRUD/雷达/ML 降级 | `tests/test_recommend.py`、`tests/test_watchlist.py`、`tests/test_validation.py` | M6-02…M6-08 | QA | M | — | ① AC-F10…F13 全覆盖；② 推荐不产生订单（断言）；③ 未通过样本外禁止标“可用”；④ 自选池导入边界；⑤ ML 缺失降级；⑥ `pytest -q` 全绿 |
| M6-12 | 依赖与合规收口（sklearn 可选依赖登记、UBA 文案核对、验收清单更新） | `docs/THIRD_PARTY.md`、`requirements.txt`、`docs/ACCEPTANCE_CHECKLIST.md` | M6-07、M6-09、M6-10 | Integrator | S | — | ① sklearn 作为**可选**依赖登记（许可 BSD-3、版本、用途）；② UBA 文案与 SPEC §11.8 一致（推荐/交易/报告/关于）；③ 验收清单增补 M6 条目；④ 全新 venv 缺失 sklearn 仍可启动 |
| **M6-13（P0）** | **短线模型 v2 —— 用户核心目标“短线可盈利”**（v1 负结果：48 折 OOS 命中率 24.83%≈基础概率、扣费期望 -0.22%、MDD -32.41%、`usable=False`） | `strategy/short_term.py`、`strategy/factors.py`、`backtest/validation.py`、`docs/SHORT_TERM_MODEL_V2.md` | M6-08、M6-07 | Dev2 | L | —（复用已批准接口） | ① **扩样本 30–50 只**（2021-10~至今，真实数据）；② **换标签**：未来 5/10 日**截面分位**或三分类（涨/平/跌）+ 横向中性化；③ 加**市场状态/板块特征**（数据可得则用，缺则降级并标注）；④ 模型：GBDT/排序学习（CPU-only、sklearn 可选）；⑤ 严格 **48 折 walk-forward + purge/embargo + 泄漏复核**；⑥ `usable` 自动判定；**⑦ 门槛：OOS 命中率显著高于基础概率 + 扣费期望>0 + 回撤可接受；未达标不得接入推荐/自动化，只能“研究阶段”**；⑧ 只登记结论，实验细节写 `docs/SHORT_TERM_MODEL_V2.md` |
| **M6-14（P0）** | **全市场筛选引擎（F14）**：全 A（约 5000）分层漏斗——基础剔除→流动性→量价信号打分→过热剔除→TopN | `strategy/screener.py`（复用 `data/provider.py`/`data/cache.py`，不新增表） | M1-03、M6-08 | Dev2 | L | —（复用已批准接口；`SCREEN` kind 为扩展枚举） | ① 分层漏斗顺序与阈值可配（`screen.*`）；② **`as_of` 截断无未来数据**；③ 输出含**全市场总数/过滤后/扫描数/数据时间**；④ **可复现**（同输入同输出，容差 1e-9）；⑤ **CSV 导出**；⑥ **200–300 只扫描 ≤5 分钟**；⑦ 写 `recommend_runs(kind=SCREEN)`；⑧ AC-F14-01…05 |
| **M6-15（P0）** | **筛选器有效性验证（F15）**：5 分组回测 + RankIC/ICIR + 换手成本 + 样本外判定 | `strategy/screener.py`、`backtest/validation.py`、`docs/SCREENER_VALIDATION.md` | M6-14、M6-08 | Dev2 | L | —（复用已批准接口） | ① 按得分分 5 组，统计未来 5/10/20 日收益/超额/单调性；② RankIC/ICIR/t 值；③ Top 组换手率与**扣费后**收益（FeeModel 口径）；④ walk-forward 样本外；⑤ 判定线默认 **RankIC>0.03 且 Top10 日超额>0（扣费后）**，写 `research_runs(kind=SCREEN_VALIDATION)` + `usable`；⑥ 未通过只标“研究阶段/无效”；⑦ AC-F15-01…05 |
| M6-16 | 全市场筛选页（F14/F15 展示与导出） | `ui/pages/screener.py` | M6-14、M6-15 | Dev3 | M | ⭐CF-12 | ① 显示全市场总数/过滤后/扫描数/数据时间/耗时；② TopN 表 + 因子值 + 风险标记；③ 导出 CSV；④ 固定“研究阶段·不构成买入建议·不承诺收益”+UBA；⑤ F15 分组/RankIC/判定徽标（未过线=研究阶段/无效）；⑥ 无“一键买入/自动跟单”；⑦ AC-F14-02/05、AC-F15-03 |
| M6-17 | QA：筛选可复现性/无未来数据/性能/导出测试 | `tests/test_screener.py` | M6-14…M6-16 | QA | M | — | ① 同 as_of 两次扫描结果一致（AC-F14-01）；② **注入未来行反向用例**：扫描不得读取 as_of 之后数据（AC-F14-04）；③ 200–300 只 ≤5 分钟（AC-F14-03）；④ CSV 导出可回读一致（AC-F14-02）；⑤ 未通过验证时 UI/接口无“有效/推荐”表述（AC-F15-03）；⑥ `pytest -q` 全绿 |
| **M6-18（P0·恢复）** | **扩样本数据**：3 只 → **≥30–50 只**（优先沪深 300/中证 500 成分，多板块、含除权事件），真实日线 ≥3–5 年；DQ 全过、缓存混源 fail-closed、产出数据覆盖报告 | `data/provider.py`、`data/cache.py`、`scripts/fetch_data.py`、`scripts/build_universe.py`、`docs/DATA_SOURCES.md` | M1-03 | **Dev1** | L | — | ① 股票池 ≥30 只且覆盖多板块；② 数据区间 ≥3 年、日线完整（DQ001–DQ007 全过）；③ 缓存不混源（fail-closed）；④ `runtime/exports/data_coverage.md` 记录；⑤ 拉取脚本可复现、限流退避 |
| **M6-19（P0·恢复）** | **标签矩阵**：未来 5/10 日**截面超额收益分位**（默认 5 组）+ 三分类（涨/平/跌）+ 绝对收益（对照）；严格 shift 防未来函数 | `strategy/factors.py`（新增标签模块/函数） | M6-18 | **Dev2** | M | — | ① 标签与特征严格 as_of 截断；② 多种标签可切换且可复现；③ 标签分布统计输出；④ 单测覆盖 shift/泄漏边界 |
| **M6-20（P0·恢复）** | **特征扩充**：市场状态（指数制度/波动率）、板块相对强度、量价结构（放量/背离/涨停邻近度）、流动性；缺失字段降级并标注 | `strategy/factors.py` | M6-18 | **Dev2** | M | — | ① 特征清单文件化、可测试；② 每特征无未来函数（截断测试）；③ 缺失数据跳过不伪造；④ 特征与标签按时间切分可复现 |
| **M6-21（P0·恢复）** | **GBDT 排序模型**：CPU-only、sklearn 可选；先跑无模型基线，再加 GBDT/排序学习；48 折 walk-forward + purge/embargo + 参数稳健性 | `strategy/ml_ranker.py`、`strategy/short_term_v2.py` | M6-19、M6-20 | **Dev2** | L | —（OQ-7 已批准） | ① 基线（无模型）与 GBDT 对照；② 48 折 OOS 指标（RankIC/命中率/扣费期望）；③ sklearn 缺失降级中文提示；④ 参数稳健性风险灯；⑤ `usable` 自动判定 |
| **M6-22（P0·恢复）** | **研究 QA**：可复现、无未来数据、OOS 门槛校验、结论诚实性检查（未过线不得宣称有效） | `tests/test_short_term.py`、`docs/SHORT_TERM_MODEL_V2.md` | M6-21 | **QA** | M | — | ① 同参数同数据两次实验一致（容差）；② 注入未来行反向用例必须被拒/无影响；③ 门槛校验脚本（RankIC>0.03 且 Top10 日扣费超额>0 且命中率显著>基础概率）；④ 未过线时报告/UI 仅“研究阶段/无效”；⑤ `pytest -q` 全绿 |

---

## 4. 角色负载与并行排期建议

### 4.1 负载表

| 角色 | 任务数 | S/M/L | 估算人日 | 备注 |
|---|---:|---|---:|---|
| Dev1 | 13 | 2/6/5 | ~8.7 | **前 3 天是全局瓶颈**：M0-03 必须最先交付；M6 阶段负责表结构扩展 + 自选池数据层 + **M6-18 扩样本数据（恢复后 P0 第一项）** |
| Dev2 | 22 | 2/12/8 | ~18 | MVP：策略/回测；**M6：推荐引擎、两条线策略、异动雷达、ML 排序、walk-forward、短线模型 v2、全市场筛选+验证、标签矩阵/特征/GBDT（最重）** |
| Dev3 | 26 | 1/18/7 | ~15.5 | **最重**，见 4.2 缓解方案；MVP：交易/风控/自动化/9 页 UI；M6：3 个新页面 |
| QA | 13 | 2/10/1 | ~7.5 | R2 起介入；M6 需新增推荐/自选池/样本外/筛选可复现与无未来数据测试 + **M6-22 研究 QA** |
| Integrator | 11 | 3/7/1 | ~6.5 | M6 负责 sklearn 可选依赖与 UBA 文案/验收清单收口 |

> M6 任务分布：已完成 17 项（M6-01…M6-12、M6-14…M6-17）；进行中 1 项（M6-13）；恢复后新增 5 项（M6-18…M6-22，Dev1 1 / Dev2 3 / QA 1）。

### 4.2 Dev3 负载缓解（需 Lead 裁决）

Dev3 承担 23 项（36%）为最大风险。可选方案（按推荐顺序）：

1. **UI 页面移交（推荐）**：`ui/pages/*` 页面文件互相独立、只调用服务层。待 Dev1（M1 结束）与 Dev2（M1 结束）空闲时，Lead 可把 `M2-08`、`M2-09`、`M4-01` 等页面分派给 Dev1/Dev2，Dev3 只负责 `_common.py` 与页面骨架。**需 Lead 批准**（BRIEF 第 5 节将 UI 归 Dev3，属建议性归属，页面级隔离可安全移交）。
2. **M3-02 拆成两次提交**（盘前/盘后先，盘中后），仍是同一任务编号。
3. **降低 M5 打磨深度**：M5-01 只做统一主题与关键图表，不做逐页精修。
4. 最差情况：把 M5-03 合并进 M4-02，减少一次文件往返。

### 4.3 建议排期（轮次，非硬约束，以依赖为准）

| 轮次 | 目标 | 主要任务（按角色） |
|---|---|---|
| R1 | 地基与契约 | Dev1: M0-03→M0-01/M0-05/M0-06；Dev2: M1-05/M1-10/M1-12；Dev3: M2-01/M2-04；Integrator: M0-08/M4-04 |
| R2 | 基础设施收口 + 数据起步 | Dev1: M0-02/M0-04/M1-01；Dev2: M1-06/M1-07/M1-08/M1-09；Dev3: M2-05；QA: M0-10；Integrator: M0-09 |
| R3 | 数据贯通 + 交易内核 | Dev1: M1-03/M1-02；Dev2: M1-11；Dev3: M2-06/M2-03；QA: M1-14/M1-15/M1-16；Integrator: M1-13 |
| R4 | 研究闭环验收 + 交易体验 | Dev1: M1-04；Dev2: M4-03；Dev3: M2-02/M2-07/M2-08/M2-09/M2-10；QA: M2-11/M2-12；Dev3+QA 准备 M1 验收 |
| R5 | 自动化 | Dev3: M3-01/M3-02/M3-03/M3-04/M3-05/M3-06/M3-07/M3-08；QA: M3-09/M3-10；Dev1/Dev2 支援 UI 页面（见 4.2） |
| R6 | 交付 | Dev3: M4-01/M4-02；Dev2: M5-02；Integrator: M4-05/M4-07/M4-08；QA: M4-06 |
| R7 | 打磨验收 | Dev3: M5-01/M5-03；Integrator: M5-04/M5-05/M5-06；QA: M5-07 |
| **R8（Phase 2）** | **策略研究增值 + 全市场筛选（M6，已恢复）** | **恢复后 P0（串行）**：M6-18（Dev1 扩样本）→ M6-19（Dev2 标签矩阵）→ M6-20（Dev2 特征）→ M6-21（Dev2 GBDT）→ M6-22（QA 研究验证）；**已完成**：M6-01…M6-12、M6-14…M6-17（代码/页面/测试已交付）；**进行中**：M6-13 短线 v2（随 P0 续） |

---

## 5. 文件归属矩阵（写入权限）

| 目录/文件 | 所有者 | 可写者 | 说明 |
|---|---|---|---|
| `app.py` | Dev3 | Dev3（M5-01 可动样式） | 唯一入口，改动需通知 Lead |
| `core/*.py` | Dev1 | Dev1 | 其它角色只读 |
| `data/*.py` | Dev1 | Dev1 | 其它角色只读 |
| `strategy/base.py`、`strategy/builtin/*` | Dev2 | Dev2 | 新增策略=新增文件，不改 base |
| `backtest/*.py` | Dev2 | Dev2 | — |
| `trading/*.py` | Dev3 | Dev3 | — |
| `automation/*.py` | Dev3 | Dev3 | — |
| `ui/pages/*.py` | Dev3（可经 Lead 批准移交） | 各页面任务 owner | **一个页面文件同一时间只允许一个任务写** |
| `ui/pages/_common.py` | Dev3 | Dev3 | 共享工具，改动需通知页面 owner |
| `tests/*` | QA | QA（Dev 可加自己模块的用例文件，不与他人重名） | 用例文件按模块命名，避免冲突 |
| `scripts/*` | Integrator | Integrator | Dev 可提供建议不改文件 |
| `docs/*` | PM / Scout / Integrator（按文件） | 对应 owner | `SPEC.md`/`TASKS.md`=PM；`BORROW_MAP*`=Scout；`THIRD_PARTY/RUNBOOK/USER_GUIDE`=Integrator |
| `config.yaml`、`requirements.txt` | Dev1（M4-08 起 Integrator 可改锁定段） | Dev1/Integrator | 变更键名=契约变更 |
| `data/watchlist.py`（Phase 2） | Dev1 | Dev1 | M6-02 交付；自选池数据层 |
| `strategy/recommend.py`（Phase 2） | Dev2 | Dev2 | M6-03/M6-06 顺序写（同一文件串行） |
| `strategy/ml_ranker.py`（Phase 2） | Dev2 | Dev2 | M6-07；CPU-only，无 DL 框架 |
| `strategy/short_term.py`、`strategy/factors.py`（Phase 2） | Dev2 | Dev2 | M6-13 短线模型 v2；复用 ML/验证接口，不新增契约 |
| `docs/SHORT_TERM_MODEL_V2.md`（Phase 2） | Dev2 | Dev2 | M6-13 实验记录；评审结论由 PM 登记到 REVIEW_M1 |
| `strategy/screener.py`（F14/F15） | Dev2 | Dev2 | M6-14/M6-15；全市场筛选与验证（复用 Phase 2 表，不新增表） |
| `ui/pages/screener.py`（F14/F15） | Dev3 | Dev3 | M6-16；第 12 页，由 `app.py` 动态发现 |
| `tests/test_screener.py`（F14/F15） | QA | QA | M6-17；可复现/无未来数据/性能/导出 |
| `docs/SCREENER_VALIDATION.md`（F14/F15） | Dev2 | Dev2 | M6-15 验证报告；评审结论由 PM 登记 |
| `strategy/builtin/value_quality.py`、`breakout_abnormal.py`（Phase 2） | Dev2 | Dev2 | 新增策略文件=不改 base |
| `backtest/validation.py`（Phase 2） | Dev2 | Dev2 | M6-08；walk-forward 样本外 |
| `ui/pages/watchlist.py`、`ui/pages/recommend.py`（Phase 2） | Dev3 | Dev3 | 两页独立，互不覆盖；由 `app.py` 动态发现 |
| `runtime/` | 运行期产物（DB/日志/备份/导出） | 所有模块运行时写 | **不提交**（.gitignore） |

**禁止**：跨目录改文件；在别人文件里“顺手修一行”；直接改他人测试断言来让测试变绿；绕过 `OrderService` 调 `BrokerAdapter.submit_order`；把密钥写进配置/日志/注释。

---

## 6. 变更与协作流程

1. **契约变更**（第 2 节 13 项 MVP 冻结 + M6 新增扩展项）：提出方写变更说明（原因/影响/迁移）→ Lead 审批 → PM 更新 SPEC/TASKS 版本 → 通知全员 → 同步改测试。**Phase 2 的 5 张表/接口/配置键为“扩展项”，须逐项批准后实施。**
2. **范围变更**：一律走 `SPEC.md` §14“开放问题/后续版本”，用户或 Lead 拍板后另立里程碑；Dev 不得自行扩功能。用户目标升级（2026-10-25）已新增 M6（F10–F13），不得在 M6 内再私自加功能。
3. **依赖阻塞**：被阻塞者 `send_message` 给依赖 owner + Lead，说明阻塞点与最小需求；PM 视情况调整任务顺序（不改依赖关系）。
4. **缺陷流转**：QA 记录（编号/复现步骤/期望/实际/严重度）→ 对应 owner 修 → QA 回归。
5. **每日同步**：每人一句话（完成/进行中/阻塞），Lead 汇总；PM 负责把阻塞升级。
6. **集成修复**：Integrator 修跨模块缺陷须 Lead 批准；单次改动 >2 个模块文件或触及冻结契约时，必须由原 owner 参与评审。
7. **M6 合规红线**：任何任务不得实现“承诺收益/保证盈利/推荐一键自动下单”的表述或入口；UBA 声明与样本外展示为验收硬条件。

---

## 7. 风险与调整预案

| 风险 | 触发信号 | 预案 |
|---|---|---|
| Dev1 前期成瓶颈（M0-03） | R1 第二天 M0-03 未完成交易域首轮提交 | Lead 协调 Dev1 只做 M0-03，把 M0-05/M0-06 临时移交 Dev3（core 文件独立，页面工作后移） |
| Dev3 过载 | R4 结束仍剩 >8 项 | 执行 4.2 方案 1（UI 页面移交） |
| akshare 当天不可用 | M1-02 联调失败 | MockProvider 先行（M1-01 已含），akshare 接入转为“尽力而为”，验收改用 Mock + 缓存 |
| TA-Lib 装不上 | M1-06 验证失败 | 直接走 pandas 兜底，M1-06 验收按兜底实现判定，TA-Lib 列为可选优化 |
| 契约被擅自改动 | 集成时字段/签名不一致 | 立即冻结相关提交，Lead 裁决；按 6.1 流程补变更；责任人补测试 |
| 测试数据不具备代表性 | 回测结果异常 | 用 M1-13 固定样例数据 + QA 构造边界用例（涨跌停/T+1/除权） |
| 集成阶段大量返工 | M4-06 失败项 >5 | 提前执行 M4-06（QA 可在 R5 末尾先写骨架），按 trace_id 定位归属模块 |
| 时间不足 | R6 仍有关键功能缺失 | 保 P0（F1/F3/F4/F6/F7 核心页），M5 打磨整体降级；范围削减须 Lead + 用户确认 |
| **M6 过拟合/推荐误导** | 榜单样本外失效、用户误信 | 样本外强制 + 研究阶段标记 + UBA 强制展示 + 推荐默认不自动下单；QA 专项用例（AC-F13-02/AC-F10-03） |
| **M6 数据缺口**（分钟/资金流/龙虎榜） | 异动雷达规则失效 | 规则级降级并标注“数据不可用”；不伪造；先做日频可得的规则（量比/换手/振幅/涨幅） |
| **M6 工期膨胀** | 拖垮 MVP 收尾 | M6 以 M1 验收为硬门槛；先 F10/F12 再 F11/F13；ML 作为可选增强；契约扩展逐项批准 |
| **短线模型 v2 预期管理** | 用户期待“短线可盈利”但 v1 已证明无 alpha | 先扩样本/换标签/加特征再谈模型；`usable=False` 时 UI 明示“研究阶段”；**绝不承诺收益**；未过 OOS 禁止进入推荐/自动化（M6-13 门槛） |
| **全市场筛选成本/未来函数（F14/F15）** | 5000 只扫描超时、限流、误用未来数据 | 分层漏斗先做低成本过滤 + 缓存/分批；as_of 强制截断 + QA 注入未来行反向用例；未过 F15 验证不得宣称有效（M6-14/M6-15/M6-17） |

---

## 8. 变更记录

| 版本 | 日期 | 变更 | 变更人 |
|---|---|---|---|
| v1.0 | 2026-10-25 | 初版：63 项任务（M0–M5）、13 项契约冻结、首批 11 项可并行、角色负载与排期建议、文件归属矩阵、变更流程 | pm-quant |
| v1.1 | 2026-10-25 | ①修正契约标记：M1-11 → ⭐CF-10、M1-12 → 无冻结（CF-5 仅属 RiskGate）；②同步 SPEC v1.1 口径：M1-12/M1-16 指标（max_drawdown 负值 + max_drawdown_abs 正值 + calmar 除零）、M2-04/M2-11 金额类仅约束买入（AC-F6-09） | pm-quant |
| v1.2 | 2026-10-25 | ①M3-05 页面文件 → `ui/pages/strategies.py`（`app.py` 动态发现、按 `PAGE_TITLE` 去重）；②M5-07 验收清单路径 → `docs/ACCEPTANCE_CHECKLIST.md`（已有 v1.2 交付）；③M4-05/M5-06 保持“未交付”，标注负责人 Integrator 并安排后续轮次（M4-05 文档部分 RUNBOOK v1.2 已交付，脚本待做） | pm-quant |
| v1.3 | 2026-10-25 | ①同步 SPEC v1.2 口径（Dev3）：M2-04/M2-11 增加自动交易授权 `allow_auto_orders` 与数据缺失 fail-closed（AC-F6-10/11）；②测试文件路径对齐实际交付：M1-15 → `tests/test_broker_sim.py`、M2-11 → `tests/test_risk.py`；③M1-15 增加 P0-1 卖出 fail-closed 回归项 | pm-quant |
| v1.4 | 2026-10-25 | **用户目标升级（赚钱导向）**：①新增 **M6 策略研究增值（Phase 2）12 项任务**（M6-01…M6-12），文件归属 `core/models.py`、`data/watchlist.py`、`strategy/recommend.py`、`strategy/ml_ranker.py`、`strategy/builtin/{value_quality,breakout_abnormal}.py`、`backtest/validation.py`、`ui/pages/{watchlist,recommend}.py`、测试 3 件；②总览/负载/排期更新（75 项、R8）；③新增 M6 退出条件与 Phase 2 风险；④契约章增加“Phase 2 扩展待批”说明（5 表/接口/配置/事件/警告码）；⑤文件归属矩阵新增 7 个 Phase 2 文件；⑥M6 合规红线（不承诺收益、默认不自动下单） | pm-quant |
| v1.5 | 2026-10-25 | **Lead 批准落地**：①Phase 2 契约扩展由“待批”改为“**已批准并冻结**”（5 表扩展新增/不改已有 22 表；§6.8 接口；`recommend.*`/`research.*` 配置；2 个事件；`RQ_WARN_NO_BENCHMARK`）；②M6-01…M6-12 契约列、M6 前置说明、关键路径同步解除待批；③OQ-6（推荐禁止自动下单）/OQ-7（sklearn 可选）/OQ-8（OOS 年化≥10% 且回撤≤15%）已拍板；④依据同步 BRIEF v0.2、SPEC v1.4 | pm-quant |
| v1.6 | 2026-10-25 | **M1 验收期口径登记**：①M1-11 增加价格口径（撮合/手数/资金核算用**不复权真实价**；复权仅绩效展示；hfq 不得用于手数与资金核算）与 3 个新警告码（`RQ_WARN_MOCK_DATA`/`RQ_WARN_PRICE_JUMP`/`RQ_WARN_POSITION_MIN_LOT`，警告非阻断）；②依据同步 SPEC v1.5；③M1 功能验收通过（见 `docs/REVIEW_M1.md`），策略有效性未证明、样本外验证移交 M6-08 | pm-quant |
| v1.7 | 2026-10-25 | **M1 修订 + 新配置键**：①登记 Lead 批准 `backtest.price_adjust`（默认 `none`；`hfq`=合成价全流程+自动告警+关闭最小手数校验），写入 M1-11；②M1 按不复权真实数据修订（功能通过/有效性未证明，同仓位被动对照仅 ma_cross 与 ml_ranker 略好、全部小幅负收益、无显著 alpha）；③ML 负结果（真实 WF 6 折 OOS AUC 0.4456）写入 M6-07 前置与改造方向；④M6-08 明确为 R8 第一任务、未过样本外不得进入推荐/自动化 | pm-quant |
| v1.8 | 2026-10-26 | **短线模型 v1 负结果登记 + v2 P0**：①登记 v1 结果（3 标的/2021-10~2026-10/真实数据/48 折 WF；OOS 命中率 24.83%≈基础概率、扣费期望 -0.22%、MDD -32.41%、`usable=False`）与结论“无短线 alpha，禁止接入推荐/自动化”；②新增 **M6-13 短线模型 v2**（扩样本 30–50 只、换标签截面分位/三分类、市场状态/板块特征、GBDT/排序学习、严格 purge；门槛 OOS 命中率显著>基础概率 + 扣费期望>0 + `usable`）并列为 **M6 当前 P0**；③登记**用户核心目标：短线可盈利**（保留不承诺收益/未过 OOS 不得自动化的合规红线）；④任务 75→76、Dev2 16→17、文件归属与 R8 排期同步 | pm-quant |
| v1.9 | 2026-10-26 | **F14/F15 全市场筛选（M6 P0）**：①登记用户目标升级“**以盈利为目标的选股与自动交易系统**”；②新增 **M6-14 全市场筛选引擎** / **M6-15 筛选器有效性验证**（Dev2）、**M6-16 筛选页**（Dev3）、**M6-17 QA（可复现/无未来数据/性能/导出）**，四件均对应 SPEC §8.14/§8.15 与 AC-F14-01…05 / AC-F15-01…05；③M6 P0 更新为 M6-01/M6-08/M6-13/M6-14/M6-15；④任务 76→80（Dev2 17→19、Dev3 25→26、QA 11→12），负载/R8/文件归属同步；⑤合规红线不变（不承诺收益、未过验证不得宣称有效/接自动化） | pm-quant |
| v1.10 | 2026-10-26 | **用户决定恢复 M6（短线选股/可验证统计边际）**：①M6-01…M6-17 状态更新（✅ 已完成 17 项：M6-01…M6-12、M6-14…M6-17；🟡 进行中：M6-13 短线 v2，框架已实现无可用配置）；②新增恢复后 P0 子任务 **M6-18 扩样本数据（Dev1）/ M6-19 标签矩阵（Dev2）/ M6-20 特征扩充（Dev2）/ M6-21 GBDT 排序（Dev2）/ M6-22 研究 QA（QA）**，P0 串行：数据→标签/特征→模型→严格 OOS→是否接入；③任务 80→85（Dev1 12→13、Dev2 19→22、QA 12→13），负载/R8 同步；④引 `M6_HANDOFF_PROMPT.md`；合规红线不变 | pm-quant |
