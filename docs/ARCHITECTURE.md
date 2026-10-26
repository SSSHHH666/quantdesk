# QuantDesk 系统架构设计（ARCHITECTURE）

> 版本：v1.0
> 日期：2026-10-25
> 作者：PM（pm-quant）
> 定位：**解释性架构文档**。本文不新增任何接口、字段、表、配置键或事件主题；与 `SPEC.md` 冲突时**以 SPEC 为准**，与 `BRIEF.md` 冲突时**以 BRIEF 为准**。

---

## 0. 文档权威顺序与适用读者

| 顺序 | 文档 | 作用 | 冲突时 |
|---|---|---|---|
| 1 | `quantdesk/BRIEF.md` | 用户意图转译、MVP 范围、技术栈、目录与接口契约 | **最高** |
| 2 | `docs/SPEC.md` | 需求细化、A 股口径、22 张表、接口签名、验收标准、风控原因码 | 高于本文 |
| 3 | `docs/TASKS.md` | 任务分解、依赖、文件归属、契约冻结清单（CF-1…CF-13） | 高于本文 |
| 4 | `docs/ARCHITECTURE.md`（本文） | 为什么这样分层、数据怎么流动、故障怎么处理、怎么扩展 | 解释层，不覆盖契约 |
| 5 | `docs/BORROW_MAP.md` | 借鉴来源与实现模式（**设计参考**，含非 MVP 内容） | 低于 1–3 |

**读者**：Dev1/Dev2/Dev3（开工前必读 §2/§3/§4/§7/§9）、QA（§4/§8/§9，用于设计端到端与异常用例）、Integrator（§3/§8）、后来的智能体（§0/§2/§7 快速上手）、Lead（§1/§6/§9/§12 评审用）。

**当前阶段说明**：截至 2026-10-25，M0 骨架进行中；R1 首批（`core/models.py`、`core/events.py`、`strategy/base.py`、`backtest/broker_sim.py`、`trading/broker.py`、`trading/risk.py`、`requirements.txt`、`config.yaml`、`scripts/*`）正在开发。本文描述的架构是**目标架构**，部分文件尚未创建，属正常现象。

---

## 1. 系统总览

**QuantDesk 是一个本地单机、单用户、日频为主的量化交易工作台。**

一句话数据流：

```
数据自动拉取落库 → 策略产生信号 → 风控闸门裁决 → 回测验证 → 模拟盘执行 → 定时自动化 → 可视化与通知
```

### 1.1 五条设计原则（所有实现取舍以此裁决）

| 原则 | 含义 | 落地方式 |
|---|---|---|
| **P1 回测 = 实盘同源** | 回测与模拟盘共用**同一套 A 股规则、费用模型、策略接口与指标口径**；模拟盘与未来实盘共用**同一套风控函数、订单服务与持仓记账**；差异只在 Broker 适配器与数据来源 | A 股规则与费用只在 `backtest/broker_sim.py` 实现一次（CF-10），`PaperBroker` 复用它；策略/风控/订单服务里**禁止出现 `if backtest:` 分支** |
| **P2 风控唯一入口** | 任何订单（手动、策略、定时任务）都必须经过同一个纯函数风控闸门，不可绕过 | 只有 `OrderService.place()` 持有 Broker 引用；`RiskGate.check()` 纯函数、fail-closed（CF-5/CF-6） |
| **P3 文件级隔离** | 并行开发不互相覆盖 | 按 BRIEF §5 归属；`ui/pages/*` 一页一文件；`app.py` 动态发现页面（D-9） |
| **P4 离线可演示** | 没网、没 akshare、没 TA-Lib、没 ccxt 也能跑通全流程 | MockProvider + 本地缓存 + pandas 指标兜底 + 适配器骨架优雅降级 |
| **P5 可追溯、可停止** | 每个自动动作都能查到“谁、何时、为什么”，随时能一键停 | `trace_id` 贯穿 + 结构化日志 + 审计表 + 自动化默认关闭 + 一键停止 |

### 1.2 明确不做（避免架构膨胀）

真实资金接入、L2 逐笔/tick 撮合、多用户与权限、云部署、移动端、LLM 研报、组合优化库、walk-forward/超参搜索、分钟级高频、**深度学习/GPU 训练**。详见 `SPEC.md` §1.5。

---

## 2. 分层架构

### 2.1 分层图

```
┌──────────────────────────────────────────────────────────────────────┐
│  表现层  ui/pages/*.py（9 页，Streamlit）  app.py（导航 + 动态发现）   │
│  总览 / 行情 / 手动交易 / 策略 / 回测 / 持仓 / 自动化 / 设置 / 日志    │
└───────────────▲──────────────────────────────────────────────────────┘
                │ 只调用服务层，不写 SQL、不调 Broker
┌───────────────┴──────────────────────────────────────────────────────┐
│  应用服务层                                                           │
│  backtest/engine.py（研究）   automation/scheduler.py·jobs.py（编排） │
│  trading/order_manager.py（订单唯一入口）                             │
└───────────────▲──────────────────────────────────────────────────────┘
                │
┌───────────────┴──────────────────────────────────────────────────────┐
│  领域层                                                               │
│  strategy/base.py（策略接口/注册表/指标门面）                         │
│  backtest/broker_sim.py（A股撮合规则 + 费用模型，唯一实现）           │
│  trading/risk.py（风控闸门）  trading/portfolio.py（账户/持仓/T+1）   │
│  trading/broker.py（BrokerAdapter + PaperBroker + 骨架）              │
│  automation/notify.py（通知渠道）                                     │
└───────────────▲──────────────────────────────────────────────────────┘
                │
┌───────────────┴──────────────────────────────────────────────────────┐
│  基础设施层  core/config.py  core/models.py  core/db.py                │
│              core/events.py  core/logging.py                          │
└───────────────▲──────────────────────────────────────────────────────┘
                │
┌───────────────┴──────────────────────────────────────────────────────┐
│  数据接入层  data/provider.py（akshare / Mock / 主备降级）             │
│              data/cache.py（增量落库 / 复权 / 数据质量校验）           │
└───────────────▲──────────────────────────────────────────────────────┘
                │
        ┌───────┴─────────────────────────┐
        │ akshare（网络）  Mock（离线）     │
        │ SQLite：runtime/quantdesk.db     │
        └──────────────────────────────────┘
```

### 2.2 模块职责与依赖规则

| 层 / 模块 | 目录文件 | 职责 | 允许依赖 | **禁止依赖** |
|---|---|---|---|---|
| 基础设施 | `core/config.py` `core/models.py` `core/db.py` `core/events.py` `core/logging.py` | 配置、领域模型与 ORM、连接与建表/迁移/备份、事件总线、日志与审计 | 仅第三方库（pydantic/SQLAlchemy 等） | **任何业务模块**（不得 import data/strategy/trading/backtest/ui） |
| 数据接入 | `data/provider.py` `data/cache.py` | 取数、主备降级、增量落库、复权、数据质量校验 | `core/*` | strategy/trading/backtest/ui |
| 策略 | `strategy/base.py` `strategy/builtin/*.py` | 策略接口、注册表、参数 schema、指标门面、3 个内置策略 | `core/*` | **不得访问 DB、不得联网、不得下单** |
| 回测 | `backtest/engine.py` `broker_sim.py` `metrics.py` | 事件驱动回测、A 股撮合与费用、绩效指标 | `core/*`、`strategy/*` | **不得依赖 `trading/*`**（避免循环） |
| 交易执行 | `trading/broker.py` `order_manager.py` `risk.py` `portfolio.py` | 订单生命周期、风控、账户/持仓、Broker 适配 | `core/*`；**允许复用** `backtest/broker_sim.py` 的规则与费用（D-3） | 不得依赖 `ui/*`、`automation/*` |
| 自动化 | `automation/scheduler.py` `jobs.py` `notify.py` | 调度、盘前/盘中/盘后任务、通知 | `trading/*`、`strategy/*`、`data/*`、`core/*` | 不得直接改 DB 表结构、不得绕过 OrderService |
| 表现 | `app.py` `ui/pages/*.py` | 9 个页面：展示、表单、交互 | 所有下层服务 | **不得直接调 `BrokerAdapter.submit_order`**、不得写 SQL |
| 运维 | `scripts/*` `docs/*` | 启动、环境自检、初始化、备份恢复、文档 | 任何模块（只读/编排） | 不得内嵌业务规则 |

**依赖方向的可执行检查**（QA 加入测试）：

1. `core/*` 不得 import 任何业务模块（可 `grep` 断言）。
2. `backtest/*` 不得 import `trading/*`。
3. `ui/*` 不得出现 `submit_order(`（只允许在 `trading/order_manager.py` 与测试中出现，AC-F6-01）。

### 2.3 跨层协作的三个“唯一”

| 唯一 | 是谁 | 为什么 |
|---|---|---|
| 下单唯一入口 | `trading/order_manager.py::OrderService.place()` | 保证风控不可绕过（P2） |
| A 股规则与费用唯一实现 | `backtest/broker_sim.py::AStockRules` / `FeeModel` | 保证回测实盘同源（P1） |
| 订单/成交/持仓状态唯一事实来源 | SQLite 表 + `PortfolioService` 维护的内存态 | 保证 UI 与自动化读到同一份数据 |

---

## 3. 运行时形态与部署

### 3.1 部署形态：本地单机 + SQLite + Streamlit

```
Windows 本机
┌───────────────────────────────────────────────────────────┐
│  python.exe -m streamlit run app.py                       │
│  └─ Streamlit 前端（浏览器 localhost:8501）                │
│     └─ 同一 Python 进程内：                                │
│        · 服务层对象（OrderService / Scheduler / Engine…）  │
│        · APScheduler BackgroundScheduler（后台线程）       │
│        · 事件总线（同步分发，订阅者落库/刷新）              │
│        · SQLAlchemy Engine → SQLite（WAL）                │
└───────────────────────────────────────────────────────────┘
                       │
              runtime/ 目录（不入库）
              ├─ quantdesk.db       主库
              ├─ logs/app-YYYYMMDD.log  JSON Lines 日志
              ├─ backups/*.db        每日备份（保留 7 份）
              └─ exports/*.html/csv  导出文件
```

| 项目 | 约定 |
|---|---|
| 入口 | `streamlit run app.py`；用户双击 `scripts/start.bat` |
| 端口 | 默认 8501，只监听 localhost（单用户） |
| 进程数 | **1 个进程**（Streamlit 主进程 + APScheduler 线程 + 事件分发线程），不引入消息队列/独立 worker |
| 数据库 | SQLite（WAL 模式），单文件 `runtime/quantdesk.db`，通过 SQLAlchemy 2.0 访问 |
| 时区 | 统一 `Asia/Shanghai`（配置项 `app.timezone`） |
| 用户体系 | 无（单用户，无登录/权限） |
| 云/容器 | 不支持也不要求（MVP 明确不做） |

### 3.2 硬件要求（含 GPU 说明，重要）

| 项 | 最低 | 推荐 | 说明 |
|---|---|---|---|
| CPU | 双核 | 4 核 | 回测是纯 Python 循环 + pandas，瓶颈在 CPU 单核 |
| 内存 | 4 GB | 8 GB | 300 标的 × 10 年日线（约 75 万行）内存可控 |
| 磁盘 | 1 GB 空闲 | 5 GB | 依赖 + 数据库 + 备份 + 导出 |
| 显卡 | **不需要** | — | **MVP 全程 CPU 计算，不要求 GPU** |
| 网络 | 可选（可完全离线） | — | 拉 akshare 数据时需要；演示可用 Mock |

> **关于本机 GTX 1650（4 GB 显存）的明确结论**：
> 1. **MVP 不需要 GPU**，也不需要任何 GPU 运行时（CUDA/cuDNN）配置；
> 2. **不引入任何深度学习/机器学习框架**（PyTorch / TensorFlow / JAX / scikit-learn 均不在依赖内）；
> 3. 多因子策略只使用 **pandas + numpy + 基础统计量**（动量、波动率、z-score、排序、加权）；
> 4. 若将来要做深度模型（LSTM/Transformer 等），优先级为“另立里程碑 + 可选**远程 GPU 训练**”，训练产物以权重文件形式回到本地推理，本地 GTX 1650 也不作为前置条件（详见 `ROADMAP.md` §6）。
> 5. 依据：`BRIEF.md` §3 的 F1–F9 全部可用 pandas/统计实现；引入 GPU 会显著增加安装失败率（用户代码能力弱），与“交付必须能直接运行”冲突。

### 3.3 启动与停止流程

**启动（用户双击 `start.bat`）**

```
1. 选择解释器：优先 quantdesk\.venv\Scripts\python.exe → 回退 D:\Anaconda3\python.exe
2. （M5-05 起）首次运行：建 venv → 装依赖 → 初始化 DB
3. 加载 config.yaml → pydantic 校验（非法值给中文提示）
4. 初始化 DB：连接（WAL）→ 建表/迁移 → 启动对账（订单/持仓/资金一致性）
5. 初始化日志（JSON Lines）与事件总线
6. 实例化服务：PortfolioService → RiskGate → Broker（按配置）→ OrderService
7. 若 automation.enabled=true：启动 APScheduler 并注册 3 个 job（默认 false，不自动启动）
8. 启动 Streamlit，浏览器打开 http://localhost:8501
```

**停止**：关闭命令行窗口 / `Ctrl+C`。停止时**不取消已提交订单**（订单状态以 DB 为准）；下次启动通过启动对账修复。

**关键启动自检**（失败要给出中文可操作提示，不能白屏）：DB 可读写、22 张表存在、依赖可用（akshare/APScheduler/TA-Lib/ccxt 可选）、数据源健康、账户对账。

### 3.4 并发与事务边界

| 场景 | 策略 |
|---|---|
| Streamlit 会话（多个浏览器标签） | 每个会话独立对象，但共享同一 Engine/Session 工厂；写操作加轻量互斥（`st.session_state` 标记 + DB 事务） |
| UI 下单 与 调度器下单 同时发生 | 都走 `OrderService.place()`；订单表 `client_order_id` 唯一约束 + 事务，后到者按最新持仓重新风控；SQLite 单写者由事务串行化 |
| 批量 job 下单 | **每单一个事务**（失败隔离），不做一个大事务 |
| 权益快照 | `equity_curve` 以 `(account_id, trade_date)` 为唯一键 upsert（幂等） |
| 行情写入 | `daily_bars` 以 `(symbol, trade_date)` upsert，批量提交（每标的/每批一个事务） |
| 长回测 | 不回写业务表直到成功；运行中只写 `backtest_runs.status=RUNNING`，结束一次性写 equity/trades（失败不写半截） |
| 进程崩溃 | 重启后：未完成订单置 `EXPIRED` + 通知；持仓/现金按 `trades`/`cash_flows` 重算并校验 |

---

## 4. 关键数据流

### 4.1 研究闭环（回测，M1）

```
① 用户在回测页提交参数
      ↓
② BacktestEngine.run(strategy_key, params, symbols, start, end, initial_cash, fee, slippage, benchmark)
      ↓
③ 加载数据：BarCache.load(symbols, start-预热期, end, adjust=hfq) → DataFrame
      ↓
④ 逐交易日循环（trading_calendar，含 T+1/T+2 预热）：
      a. 撮合昨日挂单（SimBroker.match：T+1 开盘价 ± 滑点，涨跌停不可成交，整手/费用）
      b. 计算持仓市值与权益
      c. strategy.on_bar(ctx) → list[Signal]
      d. 信号 → 次日待执行订单（受整手/现金约束）
      e. 写权益点、写成交明细
      ↓
⑤ BacktestResult(metrics, equity, trades, warnings, run_id)
      ↓
⑥ 落库：backtest_runs / backtest_equity / backtest_trades
      ↓
⑦ 回测页展示：KPI、权益/回撤曲线、月度热力图、交易明细、警告（RQ_WARN_*）、与历史回测对比
```

**只读原则**：回测**不经过** `OrderService`/`RiskGate`（研究用途，不产生真实/模拟订单、不写 `orders/positions` 表）；它通过同一套 `AStockRules`/`FeeModel` 保证口径一致。这与“回测=实盘同源”不矛盾——**规则同源，订单执行路径按用途区分**（见 §1.1 P1 与 SPEC §4.10 口径差异表）。

### 4.2 交易闭环（手动下单，M2）

```
① 手动交易页：选标的/方向/价格/数量
      ↓
② OrderService.preview(req)  ← 只读：风控结论 + 费用明细 + 成交后仓位占比
      ↓（用户确认二次确认框后）
③ OrderService.place(req)：
   a. 参数与标的状态校验（整手/涨跌停价/停牌/黑名单）
   b. RiskGate.check(order, account, positions, market)  ← 唯一风控入口
        不通过 → orders 落库 REJECTED(RKxxx) + risk.blocked 事件 + 审计 + 返回中文原因
   c. 通过 → broker.submit_order(order)（MVP 为 PaperBroker）
   d. 成交 → PortfolioService.apply_fill()：更新 positions（available_quantity 遵守 T+1）、
      accounts.cash、cash_flows、trades
   e. equity_curve 当日快照 upsert
   f. 发事件 order.submitted / order.filled 或 order.rejected
   g. 写审计（actor=manual，含 trace_id）
      ↓
④ UI 刷新：今日委托、持仓、账户、总览
```

### 4.3 一次自动交易的完整时序（重点：从定时任务到落库）

> 场景：用户在自动化页开启调度，某交易日 10:00 盘中扫描时，`ma_cross` 策略实例对 `600519.SH` 产生买入信号，最终成交并落库。

```
【08:50 盘前】
 1. APScheduler 触发 job_prepare（job.started 事件，trace_id=T1）
 2. 刷新交易日历，确认今天是交易日（否则 SKIPPED 结束）
 3. BarCache 增量更新自选池行情（akshare → 校验 → daily_bars upsert）
 4. PortfolioService.settle_new_day()：T+1 日初结算
       positions.available_quantity = quantity；today_buy_quantity = 0
 5. 记录当日开盘总资产 baseline（供熔断判定）、加载 risk_config → RiskGate
 6. 写 job_runs(SUCCESS, stats) → job.finished → 审计

【10:00 盘中】
 7. APScheduler 触发 job_scan（trace_id=T2）
 8. 检查数据新鲜度：快照时间是否超阈值（超 → 放弃本次扫描 + 通知，**绝不基于过期数据下单**）
 9. DataProvider.get_snapshot(自选池) → MarketSnapshot(latest_prices, price_times, suspended, delisting, daily_open_asset)
10. 遍历启用的 strategy_instances：
       a. BarCache.load(该实例标的池, 近 N 日) → Context
       b. strategy.on_bar(ctx) → list[Signal]（如 Signal(symbol=600519.SH, side=BUY, strength=0.8, reason="MA5 上穿 MA20")）
       c. 信号去重（同实例/同标的/同方向只处理一次）→ 写 strategy_signals(status=GENERATED, trace_id=T2)
11. 对每个信号：组装 OrderRequest
       client_order_id = hash(instance_id, symbol, trade_date, side)   # 幂等键
12. OrderService.place(req)：
       a. RiskGate.check()：查黑名单/停牌/上市天数 → 行情新鲜度 → 重复订单 →
          价格偏离 → 单笔金额/频率 → 熔断/账户状态 → 资金/持仓 → 单票/总仓位
       b. 【假设通过】→ PaperBroker.submit_order() → 即时成交（快照价 ± 滑点）
       c. PortfolioService.apply_fill()：
            positions.quantity +100，available_quantity 不变（T+1，今日不可卖），avg_cost 更新
            accounts.cash -= 成交金额 + 成交费用
            trades +1；cash_flows +1（BUY）
            equity_curve 当日快照更新
       d. orders 状态 → FILLED；写风控检查明细 risk_checks_json
       e. 发事件：order.submitted → order.filled
       f. 审计（actor=scheduler，trace_id=T2）
       g. 通知（可选）：成交提醒（按 notify 配置）
13. 若风控拒绝（例：单票仓位将达 23.4% > 20%）：
       orders 落库 REJECTED(reason=RK007)；strategy_signals.status=RISK_BLOCKED；
       发 risk.blocked；写审计；通知（自动单被拒，防刷屏合并发送）
14. 汇总本次扫描统计（信号数/拦截数/下单数/成交数）→ job_runs(SUCCESS, stats)

【15:00 收盘】
15. （无操作；A 股收盘集合竞价 14:57–15:00 不建模）

【15:30 盘后】
16. APScheduler 触发 job_summarize（trace_id=T3）
17. DataProvider 拉当日收盘行情 → 更新 daily_bars
18. 按收盘价重估持仓 → PortfolioService.snapshot_equity()（upsert 当日 equity_curve）
19. 计算当日盈亏、生成日报文本（持仓、成交、拒单、风控状态）
20. Notifier.send()（桌面/邮件/Webhook，按配置；失败重试 1 次后只记日志）
21. 写 job_runs(SUCCESS) + 审计 → job.finished
22. 清理过期数据（日志 30 天、回测记录保留最近 200 次）

【异常分支】
 · 第 8 步数据过期：job_scan 记 FAILED + 通知 + 不发任何信号（fail-closed）
 · 第 11 步策略抛异常：跳过该实例，其余继续，统计里记失败数
 · 第 12 步 PaperBroker 异常：订单 REJECTED(BR001)，账户回滚到提交前（事务）
 · 第 16 步程序崩溃：重启后启动对账 → 未完成订单 EXPIRED + 通知
```

**落库清单（一次自动成交涉及的表）**

| 表 | 写入内容 | 时机 |
|---|---|---|
| `daily_bars` | 当日/增量行情 | job_prepare、job_summarize |
| `strategy_signals` | 信号（含 GENERATED/RISK_BLOCKED/ORDERED） | job_scan |
| `orders` | 订单全生命周期 | OrderService.place 内 |
| `trades` | 成交明细与费用 | apply_fill |
| `positions` | 数量/可用/成本 | apply_fill + settle_new_day |
| `accounts` | 现金/总资产 | apply_fill |
| `cash_flows` | 资金流水 | apply_fill |
| `equity_curve` | 当日权益快照 | apply_fill / job_summarize |
| `job_runs` | 三个 job 的执行记录 | job 开始/结束 |
| `audit_logs` | 下单、启停、风控拦截、配置变更 | 全程 |
| `notifications` | 通知发送结果 | notify |

### 4.4 数据更新流

```
akshare（或 Mock）
   ↓ 限流（request_interval_ms）+ 重试（max_retries 指数退避）+ 失败切备源
AkshareProvider.get_bars / get_snapshot / get_symbols / get_trading_calendar / get_fundamentals
   ↓ 字段归一（列契约）与单位统一（股 / 元）
BarCache.update()：查已有最大日期 → 只拉增量 → upsert daily_bars（幂等）
   ↓
BarCache.validate()：DQ001–DQ007 检查 → 写 data_quality_issues → 返回报告
   ↓
UI（行情/日志页）与回测/策略读取
```

---

## 5. 事件主题流转

### 5.1 主题与订阅关系

| 主题 | 发布者 | 主要订阅者 | 触发时机 | 订阅者失败处理 |
|---|---|---|---|---|
| `market.bar` | `data/cache.py` | UI 刷新、日志 | 行情增量落库成功后 | 记日志，不影响发布者 |
| `signal` | 策略 / `automation/jobs.py` | `strategy_signals` 落库、UI 策略页 | 策略产出信号后 | 记日志 |
| `order.submitted` | `OrderService` | 审计、UI 委托表 | 订单提交给 Broker 后 | 记日志 |
| `order.filled` | `OrderService` / Broker 回报 | 持仓/资金更新、UI、通知 | 成交后 | 记日志 |
| `order.rejected` | `OrderService` | 审计、UI、通知（自动单） | 风控拦截或券商拒单 | 记日志 |
| `order.cancelled` | `OrderService` | 审计、UI | 撤单成功 | 记日志 |
| `risk.blocked` | `OrderService` | 审计、自动化页统计、通知 | 风控拒绝时 | 记日志 |
| `job.started` | `automation/scheduler.py` | `job_runs` 落库、UI 自动化页 | job 开始 | 记日志 |
| `job.finished` | `automation/scheduler.py` | `job_runs` 更新、UI | job 成功结束 | 记日志 |
| `job.failed` | `automation/scheduler.py` | `job_runs` 更新、通知、UI | job 异常结束 | 记日志 |
| `notify.sent` | `automation/notify.py` | `notifications` 落库、UI 日志页 | 通知发送完成 | 记日志 |

**分发规则**（`core/events.py`，CF-8）：同步分发；单个订阅者抛异常被捕获并记日志，**不影响其它订阅者与发布者**；主题名与 payload 关键字段冻结（见 SPEC §3.3），不得改名。

### 5.2 一次成交的事件流转

```
                       ┌──────────────┐
   OrderService.place ─┤ 风控通过      │─→ publish(order.submitted)
                       └──────┬───────┘
                              │ broker.submit_order
                       ┌──────▼───────┐
                       │ PaperBroker  │─→ 成交回报 → apply_fill（先写 DB/内存，再发事件）
                       └──────┬───────┘
                              ├─→ publish(order.filled) ─→ 持仓/资金已更新 → UI/通知订阅
                              │
   OrderService.place ────────┴─（风控拒绝）→ publish(risk.blocked) + publish(order.rejected)
                                                     └→ 审计 + 自动化统计 + 通知
```

**顺序纪律（重要，参考 nautilus 的 cache-then-publish 思想，重写实现）**：任何订阅者依赖的状态（持仓/资金/订单）必须**先写入数据库与内存态并成功**，再发布事件；禁止先发事件后写库，否则订阅者可能读到旧状态。

---

## 6. 状态机

### 6.1 订单状态（`orders.status`，CF-7）

```
                 ┌─────────┐
  预览/提交 ────→ │ PENDING │（草稿，仅内部）
                 └────┬────┘
                      │ OrderService.place 参数校验通过
                 ┌────▼──────┐   券商拒单/风控拒单   ┌──────────┐
                 │ SUBMITTED ├──────────────────────→│ REJECTED │
                 └────┬──────┘                       └──────────┘
                      │ 部分成交
                 ┌────▼──────────────┐
                 │ PARTIALLY_FILLED  │──全部成交──→ FILLED
                 └────┬──────────────┘
                      │ 撤单 / 超时未成交（顺延到期）
                 ┌────▼──────┐     ┌─────────┐
                 │ CANCELLED │     │ EXPIRED │
                 └───────────┘     └─────────┘
```

| 当前状态 | 允许的下一状态 | 触发者 |
|---|---|---|
| PENDING | SUBMITTED / REJECTED | OrderService |
| SUBMITTED | PARTIALLY_FILLED / FILLED / CANCELLED / REJECTED / EXPIRED | Broker 回报 / 撤单 / 启动对账 |
| PARTIALLY_FILLED | FILLED / CANCELLED / EXPIRED | Broker 回报 / 撤单 / 对账 |
| FILLED / CANCELLED / REJECTED / EXPIRED | （终态，不可变） | — |

非法流转（如 FILLED→CANCELLED）必须被拒绝并返回明确错误；所有流转写审计。

### 6.2 调度任务状态（`job_runs.status`）

| 状态 | 含义 |
|---|---|
| SUCCESS | 正常完成（允许内部有“跳过某策略”的局部失败，但统计里记录） |
| FAILED | 任务级失败（如数据源不可用、异常抛出）；不阻塞下一次调度 |
| SKIPPED | 非交易日 / 依赖不满足 / 错过执行窗口过久，主动跳过并记录原因 |

### 6.3 风控熔断状态

```
正常 ──（当日亏损 ≥ 3%，相对当日开盘总资产）──→ 熔断中
熔断中：禁止买入（RK009），允许卖出；UI 红条 + 通知
次日盘前：baseline 重置 → 正常
手动解锁：设置页二次确认 → 写审计 → 正常（当日仍记录）
```

---

## 7. 扩展点（新增功能的标准步骤）

> 所有扩展都**不改引擎核心**，只新增文件/类 + 注册。步骤与验收均来自 `SPEC.md`、`TASKS.md` 的既有契约，不新增接口。

### 7.1 新增一个策略（最常见）

| 步骤 | 操作 | 文件 |
|---|---|---|
| 1 | 在 `strategy/builtin/` 新建 `xxx.py` | 新文件 |
| 2 | 继承 `Strategy`，声明 `key`/`display_name`，实现 `params_schema()` 与 `on_bar(ctx)` | 新文件 |
| 3 | 加 `@register("xxx")` 装饰器（`strategy/base.py` 导入时自动注册） | 新文件 |
| 4 | 若需新指标：加到指标门面（优先 TA-Lib，缺失时 pandas 兜底，接口一致） | `strategy/base.py`（需与 Dev2 协调） |
| 5 | 自测：`list_strategies()` 能列出；回测页可选到；固定行情下信号可复现 | — |

**不必改**：`backtest/engine.py`、`trading/*`、`ui/*`（策略页动态渲染表单）。**验收**：AC-F2-01/02/03；新增策略文件后，其它文件 diff 为空。

### 7.2 新增一个数据源

| 步骤 | 操作 | 文件 |
|---|---|---|
| 1 | 继承 `DataProvider`，实现 6 个方法（`get_bars` 返回列契约固定） | `data/provider.py` |
| 2 | 在模块内 `ProviderRegistry.register("name", Cls)` 注册 | `data/provider.py` |
| 3 | 配置 `data.provider` 或 `data.fallbacks` 引用新名字 | `config.yaml` |
| 4 | 若字段/单位不同：在 Provider 内做归一（股/元、复权标记），**不得改缓存或回测** | `data/provider.py` |
| 5 | 自测：`health_check()`、断网时自动降级、单位校验通过 | — |

**不必改**：`data/cache.py`、回测、策略、UI。**注意**：`data/provider.py` 属 Dev1 文件，新增数据源需与 Dev1 协调排期（文件级归属）。

### 7.3 新增一个券商/交易所适配器

| 步骤 | 操作 | 文件 |
|---|---|---|
| 1 | 继承 `BrokerAdapter`，实现 4 个必需方法（`submit_order`/`cancel_order`/`get_positions`/`get_account`） | `trading/broker.py` |
| 2 | 只做三件事：Domain→Venue 转换、Venue→Domain 转换、错误码→`BRxxx` | 同上 |
| 3 | 在 `broker_registry` 注册；配置 `trading.broker` 选择 | 同上 |
| 4 | 未配置/依赖缺失时 `is_available()=false`，UI 显示“未配置” | 同上 |
| 5 | 自测：切换配置不改业务代码；未配置时不崩溃 | — |

**不必改**：`OrderService`、`RiskGate`、`PortfolioService`、UI（P1 的收益）。**验收**：AC-F9-01…04。**红线**：真实资金对接属于后续里程碑，须用户提供柜台与账户，并另做安全评审。

### 7.4 新增一个通知渠道

| 步骤 | 操作 | 文件 |
|---|---|---|
| 1 | 在 `Notifier` 内新增渠道 sender（统一 `send(event_type, title, body) -> bool`） | `automation/notify.py` |
| 2 | 配置 `notify.channels.<name>` 开关与参数；密钥只从环境变量读 | `config.yaml` |
| 3 | 设置页渲染该渠道的开关与“发送测试”（渠道列表来自配置，不硬编码） | `ui/pages/settings.py` |
| 4 | 自测：关闭时只写日志；失败不影响其它渠道；密码不入日志/DB | — |

**不必改**：调度器与业务逻辑（它们只调 `Notifier.send`）。**验收**：AC-F8-03/04。

### 7.5 （附）新增一个 UI 页面

| 步骤 | 操作 |
|---|---|
| 1 | 在 `ui/pages/` 新建 `xxx.py`，暴露 `PAGE_TITLE` 与 `render()` |
| 2 | 重启/刷新 Streamlit → `app.py` 动态发现，自动出现在导航（D-9） |
| 3 | 页面只调服务层，不写 SQL、不调 Broker；共享工具用 `_common.py` |

**不必改**：`app.py`（这是 9 个页面能并行开发的关键）。

---

## 8. 失败与降级策略

### 8.1 故障矩阵

| 故障 | 检测方式 | 系统行为（降级） | 恢复 | 用户可见 |
|---|---|---|---|---|
| 无网络 / akshare 未安装 | Provider 抛异常 / ImportError | 自动切 MockProvider；已有缓存继续可读 | 下次任务自动重试主源 | 顶部黄条“离线演示模式” |
| akshare 限流/改版 | 空表/异常/超时 | 重试 3 次 → 切备源 → 保留旧数据并标 `DQ006` 过期 | 主源恢复后自动切回 | 日志页数据校验报告 + 提示 |
| TA-Lib 缺失 | 指标门面 ImportError | pandas 同名实现兜底，结果容差 ≤1e-6 | 用户可选安装 | 设置页显示“使用 pandas 兜底” |
| ccxt 缺失 / testnet 不可用 | `is_available()` / 网络异常 | 保持 PaperBroker；下单拒 `BR001` 并提示 | 配置后可切 | 设置页显示“未配置” |
| QMT 未接入 | 调用即抛 | `BR004` 中文错误，无任何资金/网络行为 | 后续版本 | UI 明确“未接入，仅占位” |
| 数据库被锁 | 写入超时/异常 | 重试 3 次（毫秒级退避）→ 失败则报错不丢数据 | 释放后自动继续 | 中文错误 + 建议关闭其它窗口 |
| 数据库损坏 | 启动自检失败 | **不自动覆盖**；提示从 `runtime/backups/` 恢复 | 设置页恢复备份 | 红条 + 恢复入口 |
| 程序崩溃 | 重启启动对账 | 未完成订单置 `EXPIRED`；持仓/现金按流水重算校验 | 自动 | 总览提示“已恢复，X 笔订单过期” |
| 调度错过执行 | APScheduler misfire | `misfire_grace_time=300s`；超时则 `SKIPPED` 并通知；**不补跑历史交易日** | 下次正常触发 | 自动化页记录原因 |
| 行情过期（盘中） | 快照时间 > 阈值 | 风控 `RK006` 拦截所有订单；job_scan 记 FAILED + 通知 | 数据恢复后 | 拒绝原因码 + 建议 |
| 风控组件异常 | 异常捕获 | **fail-closed**：拒绝订单 `RK018`，记 ERROR | 修复后重试 | 明确错误，不放行 |
| 磁盘满 | 写入异常 | 停止写入并通知，保留已落库数据 | 清理空间 | 中文提示 + 备份清理入口 |
| 8501 端口占用 | Streamlit 启动失败 | `start.bat` 提示换端口命令 | 用户换端口 | 脚本内已给命令 |
| 单策略抛异常 | try/except 隔离 | 跳过该策略，继续其它策略 | 下次运行重试 | 自动化统计里有失败数 |

### 8.2 降级原则（三条）

1. **数据可旧不可假**：宁可展示“过期数据 + 警告”，也不伪造/拼凑行情；过期数据不用于下单（`RK006`）。
2. **安全默认失败（fail-closed）**：风控内部异常、数据缺失、状态不一致时，一律**拒绝交易动作**，而不是放行。
3. **局部失败不扩散**：单个策略/单个标的/单个渠道失败，不影响整批任务与其它模块；失败必须落库（`job_runs`/`data_quality_issues`/`notifications`）并可查。

---

## 9. 安全边界

### 9.1 风控不可绕过：三道防线

```
第一道（结构）  只有 OrderService 持有 Broker 引用；
                UI / 策略 / automation 拿不到 broker 实例，也没有其它下单方法。
第二道（自动测试）AC-F6-01：
                · grep 全仓 submit_order( 只允许出现在 trading/order_manager.py 与测试
                · 单测用 mock broker 断言 RiskGate.check 在 submit_order 之前被调用
                · 20 个 RK 原因码每个至少 1 条用例
第三道（评审）  Lead 代码评审必查：新增路径是否有绕过 OrderService 的下单；
                契约冻结项（CF-4/CF-5/CF-6）改动需审批。
```

### 9.2 密钥与隐私边界

| 资产 | 规则 |
|---|---|
| 券商 API key / SMTP 密码 / Webhook token | **只从环境变量读取**（`QD_` 前缀）；不写 `config.yaml`、不入库、不进日志、不进导出文件；UI 掩码显示 |
| A 股数据 | 仅个人研究，遵守上游条款；不高频并发抓取；不对外分发数据文件 |
| 用户交易数据（订单/持仓/资金） | 只存在本机 SQLite；不上传任何服务器 |
| 网络监听 | Streamlit 只监听 localhost，不暴露公网 |
| `.env` | 由用户自建，加入 `.gitignore`，不进仓库 |
| 日志脱敏 | `core/logging.py` 提供过滤器，过滤 `password/token/secret/key` 字段 |

### 9.3 实盘安全边界（MVP）

1. MVP **不接真实资金**：`PaperBroker` 为唯一完整实现；`QmtBroker` 为骨架（抛 `BR004`）；`CcxtBroker` 仅 testnet 可选。
2. **自动化默认关闭**（`automation.enabled: false`），用户必须显式开启；关闭时不注册任何 job。
3. 破坏性操作（下单、清仓、重置账户、改风控、启停调度、恢复备份）必须二次确认，并写审计（actor + before/after）。
4. 审计表只读（UI 无删除入口），保留 1 年；日志保留 30 天。
5. 未来接实盘前必须：用户书面确认 + 单独里程碑 + 风控参数复审 + 小额试跑 + 一键停止演练。

### 9.4 权限与文件边界（团队协作安全）

- 文件级归属见 `TASKS.md` §5；跨模块修改需 Lead 批准并通知原 owner。
- 契约冻结项（CF-1…CF-13）不得擅自改签名；变更走审批流程。
- `runtime/` 为运行期产物，不提交、不手工编辑；数据库操作统一走 `core/db.py`。

---

## 10. 性能与容量设计

| 关注点 | 设计 |
|---|---|
| 数据量 | 默认只处理自选池（20–50 只）；300 标的 × 10 年 ≈ 75 万行日线，SQLite 完全可承受 |
| 写入 | `daily_bars` 批量 upsert（每标的/每批一个事务）；WAL 提升读写并发 |
| 查询 | 关键索引：`daily_bars(trade_date)`、`orders(account_id,status,created_at)`、`orders(trace_id)`、`trades(order_id)`、`audit_logs(ts)`（见 SPEC §5.2） |
| 回测 | O(交易日 × 标的)，纯 pandas 向量化计算指标；不把历史全量 DataFrame 复制给策略（`ctx` 只给必要窗口）；20 标的 × 5 年 ≤ 15s（AC-F3-08） |
| Streamlit | `st.cache_data(ttl=60)` 缓存只读查询；`st.cache_resource` 缓存 DB Engine；写操作后主动清缓存 |
| 内存 | 不常驻全市场数据；按需加载窗口；日志/表格分页（默认 500 行） |
| 启动 | 热启动 ≤ 5s（不扫描全库、不预热数据）；冷启动 ≤ 15s（建表 + 自检） |
| 备份 | SQLite 在线备份 API（不锁库），每日一次，保留 7 份 |

---

## 11. 架构决策记录（摘要）

> 完整决策表见 `SPEC.md` 附录 B（D-1…D-9）。此处只列与架构关系最大的几条。

| 编号 | 决策 | 架构影响 |
|---|---|---|
| D-3 | A 股规则与费用只在 `backtest/broker_sim.py` 实现，`trading/broker.py` 复用 | 允许 `trading → backtest` 单向依赖，换取口径唯一 |
| D-4 | 回测成交价 = T+1 开盘价；模拟盘 = 当日快照价 | 回测引擎与 PaperBroker 的时间基准不同，须在 UI/报告注明 |
| D-6 | 风控 fail-closed | `RiskGate` 异常路径必须拒绝，不得 try/except 后放行 |
| D-7 | 自动化默认关闭 | 启动流程不注册 job，除非配置显式开启 |
| D-9 | `app.py` 动态发现页面 | 9 个页面可并行开发；新增页面不改入口 |

---

## 12. 文档一致性说明（BORROW_MAP 与 MVP 契约的差异）

`BORROW_MAP.md` 是**设计参考**（38 个借鉴点、42 条机读记录），其中部分内容是**后续版本的思路**，**不属于 MVP 契约**，实现时不得据此修改 SPEC/TASKS 冻结的接口。差异清单如下（Lead 评审时请确认）：

| BORROW_MAP 内容 | 与 MVP 的关系 | 处置 |
|---|---|---|
| A2 `BrokerAdapter` 对齐 vnpy gateway 的 `connect/subscribe/query_*` 等扩展方法 | MVP 只冻结 4 个必需方法（CF-4）；扩展方法可留可选 | 按 SPEC §6.3；不实现完整 vnpy 方法集 |
| A11 策略生命周期钩子（`confirm_signal/custom_stoploss/...`） | MVP 策略接口只有 `on_bar` + `params_schema`（CF-3） | 留作后续可选钩子，不进入 MVP 契约 |
| B1 `lookahead` 未来函数检测、B14 walk-forward、B15 DSR/PBO | SPEC 明确不做（§1.5）；MVP 只用“警告项”提示可信度 | 后续版本（`ROADMAP.md` §6） |
| B7/B8/P6 因子 Zoo（`strategy/factors/` 目录、GTJA191/Alpha101） | MVP 多因子策略用内置简单因子（动量/波动率），不建因子库 | 后续版本 |
| P7 参数优化（`backtest/optimizer.py`）、P8 `backtest/validation.py` | MVP 不做超参搜索与 walk-forward | 后续版本 |
| B13 quantstats HTML 报告 | MVP 用自实现 HTML 导出（`backtest/metrics.py`，M5-02），不新增依赖 | 按 TASKS M5-02 |
| `strategy/base.py` 的 `name` 属性 | MVPCF-3 用 `key` | 以实现为准：`key` |

> 结论：**BORROW_MAP 决定“借鉴什么思想”，SPEC/TASKS 决定“实现什么契约”**。两者冲突时前者让位。

---

## 13. 变更记录

| 版本 | 日期 | 变更 | 变更人 |
|---|---|---|---|
| v1.0 | 2026-10-25 | 初版：分层与依赖、部署与硬件（含 GPU 结论）、数据流与时序、事件流转、状态机、扩展点、故障降级、安全边界、性能设计、BORROW_MAP 差异说明 | pm-quant |
