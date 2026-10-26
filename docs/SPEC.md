# QuantDesk 产品需求规格说明书（SPEC）

> 版本：v1.7（见附录 C 变更记录）
> 日期：2026-10-25
> 作者：PM（pm-quant）
> 事实来源：`quantdesk/BRIEF.md` **v0.2**（唯一事实来源，已扩展至 F1–F13）。本文件只做**细化、口径明确与验收定义**，不扩大 MVP 范围、不改技术栈、不改目录契约。
> 相关文档：`docs/TASKS.md`（任务分解与排期）、`docs/BORROW_MAP.md`（借鉴代码定位，Scout 产出）、`docs/THIRD_PARTY.md`（第三方与借鉴登记，Integrator 产出）。

---

## 0. 文档说明与范围声明

1. 本 SPEC 覆盖 BRIEF 第 3 节的 F1–F9 全部九项 MVP 功能，以及第 8 节里程碑 M0–M5 的验收口径。
2. **v1.3 范围升级（用户目标升级，Lead 指示 2026-10-25）**：用户把项目目标升级为“以赚钱为导向”，新增 **Phase 2 增值功能 F10–F13**（选股与推荐、异动雷达、自选池管理、策略研究），对应里程碑 **M6**。**MVP（F1–F9）边界与验收不变**；F10–F13 不进入 M0–M5 交付，须在 M1 研究闭环稳定后启动（入口条件见 ROADMAP M6）。
   **v1.4（2026-10-25 Lead 批准）**：Phase 2 契约扩展**已批准并生效**——5 张新表（§5.4，CF-7 按“扩展新增、不改已有 22 表”处理）、§6.8 接口、`recommend.*`/`research.*` 配置键、事件主题 `recommend.updated`/`watchlist.changed`、警告码 `RQ_WARN_NO_BENCHMARK`；OQ-6/7/8 已拍板（见 §14）；BRIEF 已同步为 v0.2。
   **v1.7（2026-10-26 用户目标升级）**：目标从“可运行工作台”升级为“**以盈利为目标的选股与自动交易系统**”；新增 **F14 全市场筛选**、**F15 筛选器有效性验证**（§8.14/§8.15，M6 当前 P0）；数据模型复用 Phase 2 表（`recommend_runs(kind=SCREEN)`、`research_runs(kind=SCREEN_VALIDATION)`），**不新增表**；新增 `screen.*` 配置键与 `strategy/screener.py` 接口。**合规红线不变**：不承诺收益、未过样本外不得接自动化。
3. **不改变**：技术栈（Streamlit 1.37 / SQLite + SQLAlchemy 2.0 / APScheduler / akshare / pandas 2.2 / TA-Lib 或 pandas 兜底 / pydantic 2.8 / pytest）、目录与文件归属（BRIEF 第 5 节）、MVP 边界（BRIEF 第 3 节“第一版明确不做”）。Phase 2 仅在 `strategy/`、`data/`、`ui/pages/`、`backtest/` 增加文件与可选 CPU 依赖，**不引入深度学习框架**（见 §8.13）。
4. 本 SPEC 对 BRIEF 的**解释性补充**（需 Lead 在契约评审时确认，列入 `TASKS.md` 的契约冻结清单）：
   - **S-1**：运行期数据统一落 `quantdesk/runtime/`（`quantdesk.db`、`logs/`、`backups/`、`exports/`）。原因：`quantdesk/data/` 是 Python 包（数据层模块），不能放数据库文件。
   - **S-2**：`ui/pages/_common.py` 作为页面共享的展示/取数工具（金额格式化、风控结果渲染、数据源状态条）。它属于 BRIEF 第 5 节 `ui/pages/*.py` 的通配范围，不新增目录。
   - **S-3（Phase 2）**：新增 `ui/pages/watchlist.py`、`ui/pages/recommend.py` 两页（仍属 `ui/pages/*.py` 通配范围）；新增 `strategy/recommend.py`、`strategy/ml_ranker.py`、`strategy/builtin/{value_quality,breakout_abnormal}.py`、`data/watchlist.py`、`backtest/validation.py`，均落在既有模块目录内，不新增目录。
5. 所有“接口签名”“表结构”“原因码”“配置键”“事件主题”一经冻结（见 TASKS.md 契约冻结清单），任何变更须 Lead 审批并同步 PM。
6. 本 SPEC **不写业务代码**，只给出签名、语义、规则与验收标准。签名中的类型为契约约定，实现细节由各 Dev 自定。

---

## 1. 产品目标与用户画像

### 1.1 一句话定位

QuantDesk 是一个**本地运行、单用户、日频为主**的量化交易工作台：数据自动拉取落库 → 策略产生信号 → 风控闸门裁决 → 回测验证 → 模拟盘执行 → 定时自动化 → 可视化与通知。用户既能点几下按钮手动下单，也能开启策略全自动运行。
**Phase 2（M6，用户目标升级后）**在此之上增加“选股与推荐（远期价值型 / 近期异动型两条线）+ 异动雷达 + 自选池管理 + 策略研究（ML 排序/walk-forward 样本外）”，把系统从“执行工具”扩展为“研究→选股→验证→交易”的赚钱导向工作台（**只提供工具与信号，不承诺收益**）。
**v1.7 目标升级（2026-10-26）**：产品目标正式升级为“**以盈利为目标的选股与自动交易系统**”（用户核心目标：短线可盈利）；新增 F14 全市场筛选与 F15 有效性验证，筛选结果**默认研究阶段、不构成买入建议**，未通过验证不得宣称有效、不得接入自动化。

### 1.2 目标用户（用户画像）

| 维度 | 描述 |
|---|---|
| 身份 | 个人投资者，非机构、非商业用途；自己做决策，自己承担盈亏 |
| 市场 | A 股为主（沪深主板 / 创业板 / 科创板）；后续可能扩展到加密测试网 |
| 设备 | 一台 Windows 家用电脑（本项目验证环境：`D:\Anaconda3\python.exe`，Python 3.12.7），无服务器、无 Docker 习惯 |
| 资金规模 | 假设 10–50 万元人民币（具体由开放问题 OQ-4 确认） |
| 代码能力 | **弱**：会按文档装环境、能改 YAML 参数；不会写策略代码、不会调试多线程/数据库/前端 |
| 使用节奏 | 每日盘后看结果；盘中偶尔手动干预；周末跑回测做研究 |
| 核心痛点 | ① 多个软件来回切（行情、交易、Excel 记账）；② 想自动跑但不会部署与定时；③ 担心自动下单失控、看不懂日志；④ 回测结果不敢信 |
| 对产品的期望 | 点几下就能用；全中文界面；所有自动动作可追溯、可一键停止；不承诺盈利，但结果口径可信 |

### 1.3 核心使用场景

| 编号 | 场景 | 期望结果 |
|---|---|---|
| S1 | 第一次打开软件，想知道今天市场与自己的账户怎么样 | 总览页在 5 秒内展示数据源状态、账户总资产/当日盈亏、持仓概览、今日信号与订单 |
| S2 | 手动买入一只股票 | 行情页看 K 线 → 手动交易页输入代码/数量 → 看到风控预检（费用、仓位占比、是否拦截）→ 提交 → 立即在持仓页看到成交 |
| S3 | 验证一个想法 | 回测页选策略、标的、区间、资金、费率 → 跑出总收益/年化/最大回撤/夏普/胜率/交易明细/权益曲线 → 与历史回测对比 |
| S4 | 让系统自己跑 | 自动化页选策略实例、设参数、点“启动” → 每个交易日按盘前/盘中/盘后定时执行 → 在日志页看到每次信号、风控拦截、订单与成交 |
| S5 | 断网/数据源挂了 | 自动降级到本地缓存与 Mock 数据，页面顶部黄条提示“离线模式”，系统不崩溃、不重复写数据 |
| S6 | 自动交易出问题 | 一键停止自动化；日志页按 trace_id 追到具体订单；风控拦截原因用中文原因码解释并给建议动作 |

### 1.4 产品成功标准（可测量）

| 编号 | 标准 | 验证方式 |
|---|---|---|
| G1 | 从零安装到跑出第一份回测报告 ≤ 15 分钟（照文档操作） | 演示脚本 + 用户走查 |
| G2 | 不需要写任何代码，完成 4 个核心任务：看行情、跑回测、手动下单、开自动化 | 验收清单逐项勾选（QA） |
| G3 | 断网状态下全流程可演示（Mock 数据） | 拔网线/关代理后跑 `tests/e2e_smoke.py` |
| G4 | 所有订单 100% 经过同一风控函数，无旁路 | 代码审查 + 单测断言（见 AC-F6-01） |
| G5 | 自动化连续运行 5 个交易日不崩、不重复下单（幂等） | job_runs 记录 + 订单表去重检查 |
| G6 | 性能：冷启动 ≤ 15s、热启动 ≤ 5s、20 标的 × 5 年回测 ≤ 15s、单页交互 ≤ 2s | 计时脚本/日志时间戳 |
| **G7（Phase 2）** | **推荐榜单 100% 可解释**：每条推荐至少 2 条可读理由（因子/规则 + 贡献值 + 所处分位），无理由不得上榜 | 推荐页逐条抽查（AC-F10-01） |
| **G8（Phase 2）** | **推荐必须样本外**：未通过 walk-forward 样本外验证的策略/榜单，UI 必须标注“研究阶段/不建议实盘”，且禁止一键接入自动交易 | 验收清单 + UI 审查（AC-F13-02） |
| **G9（Phase 2）** | **合规零违规**：全站无“稳赚/保证收益/必涨”等承诺性表述；推荐页/交易页/报告均展示 UBA 风险声明 | 文案抽查（§11.8、AC-F10-03） |
| **G10（F14，Phase 2）** | **全市场筛选可用**：200–300 只扫描 ≤5 分钟、结果可复现、可导出 CSV、无未来数据（as_of 截断）、页面显示“全市场/过滤后/扫描数/数据时间” | AC-F14-01…05 |
| **G11（F15，Phase 2）** | **筛选器未过验证不得宣称有效**：页面/报告无“有效/可跟单”表述（除非样本外 RankIC>0.03 且 Top10 日超额>0 扣费后），否则标“研究阶段/无效” | AC-F15-03 |

### 1.5 产品边界（严格守住）

**MVP 做**：BRIEF 第 3 节 F1–F9 全部内容（M0–M5）。
**MVP 不做**（第一版）：真实资金接入、A 股 L2 逐笔与 tick 级撮合、多用户与权限体系、云部署、移动端、LLM 研报/Agent、组合优化（Riskfolio/PyPortfolioOpt）、walk-forward/超参搜索、分钟级高频、深度学习/GPU 训练。
**Phase 2（M6，用户目标升级后新增，**不修改 MVP 边界**）**：F10 选股与推荐、F11 异动雷达、F12 自选池管理、F13 策略研究（ML 排序、walk-forward、样本外验证）。对应新增数据表/接口/配置为“Phase 2 扩展”，需 Lead 批准后实施。

> 纪律：任何“想加个功能”的需求先进本文第 14 节“开放问题/后续版本建议”，由用户拍板后另立里程碑，**不得由 Dev 在任务中自行扩大**。Phase 2 同样遵守：先文档（SPEC/TASKS），后实现。

---

## 2. 术语与统一口径

### 2.1 交易术语

| 术语 | 口径 |
|---|---|
| A 股 | 沪深京三市人民币普通股票；MVP 默认只交易沪深（北交所是否纳入见 OQ-2） |
| T+1 | 当日买入的股份当日不可卖出，下一交易日可卖；**卖出所得资金当日可继续买入，但 T+1 才可提现** |
| 涨跌停 | 按前收盘价计算的价格上下限，触及上限不可买入、触及下限不可卖出（简化口径见 5.3） |
| 整手 | 买入申报数量必须为 100 股的整数倍（科创板 ≥200 股且此后可按 1 股递增）；卖出允许零股但须一次性卖出 |
| 复权 | 消除除权除息造成的价格跳空；回测默认**后复权**，行情页默认**前复权** |
| 停牌 | 当日无成交、无行情更新；不可下单 |
| 滑点 | 回测中为成交价施加的不利偏移，默认 0.05% |
| 模拟盘（Paper） | 用真实行情驱动、由本地 PaperBroker 模拟成交与结算，不动真钱 |

### 2.2 系统术语

| 术语 | 含义 |
|---|---|
| Signal（信号） | 策略输出的交易意图（symbol/side/strength/reason），**尚未经过风控，不是订单** |
| Order（订单） | 经风控裁决后提交给 Broker 的委托指令，有完整生命周期状态 |
| RiskGate（风控闸门） | 纯函数风控判定组件；所有订单（手动 + 自动）唯一必经入口 |
| OrderService | 订单唯一入口：校验 → 调 RiskGate → 落库 → 提交 Broker → 更新状态 → 发事件 |
| BrokerAdapter | 券商/交易所适配层抽象；MVP 只有 PaperBroker 完整实现 |
| trace_id | 一次调用链（一次 job、一次手动下单）的追踪 ID，贯穿日志、信号、订单 |
| 自选池（universe） | 用户关注的标的集合；自动化与数据拉取默认只处理池内标的 |

---

## 3. 系统架构与数据流

### 3.1 分层（对应 BRIEF 第 5 节目录）

```
[UI: ui/pages/*.py]  ──调用──>  [应用服务: trading/order_manager.py, automation/*, backtest/engine.py]
       │                                    │
       │                           [领域层: trading/risk.py, portfolio.py, strategy/base.py]
       │                                    │
       └──────────────>  [基础设施: core/config.py, core/models.py, core/db.py, core/events.py, core/logging.py]
                                     │
                          [数据层: data/provider.py, data/cache.py] ──> SQLite(runtime/quantdesk.db) + akshare/Mock
```

**依赖方向（硬约束，禁止反向 import）**：

- `core/*` 不 import 任何业务模块（只依赖 pydantic / SQLAlchemy）。
- `data/*` 只依赖 `core/*`。
- `strategy/*` 只依赖 `core/*`（策略不得直接访问数据库或 broker）。
- `backtest/*` 依赖 `core/*` + `strategy/*`；**不依赖 `trading/*`**。
- `trading/*` 依赖 `core/*`，且**允许复用 `backtest/broker_sim.py` 的 A 股撮合规则与费用模型**（决策 D-3，见附录 B）。
- `ui/*` 依赖所有下层，但**禁止直接调用 `BrokerAdapter.submit_order`**，只能调 `OrderService`。
- `automation/*` 依赖 `trading/*`、`strategy/*`、`data/*`，不得直接改 DB。

### 3.2 主数据流

**研究闭环（M1）**

```
akshare/Mock → DataProvider → data/cache.py(增量落库+校验) → daily_bars
   → BacktestEngine 按交易日循环 → strategy.on_bar(ctx) → Signal
   → backtest/broker_sim 撮合(T+1/涨跌停/费用/滑点) → 成交与权益
   → backtest/metrics 计算绩效 → backtest_runs/equity/trades 落库 → 回测页展示与对比
```

**交易闭环（M2）**

```
UI 手动下单 / 自动化 job
   → OrderService.place(order_request)
       → 参数与标的状态校验（黑名单/停牌/整手/涨跌停价）
       → RiskGate.check(order, account, positions, market)  ← 唯一风控入口，无旁路
       → 通过则 BrokerAdapter.submit_order → OrderResult → 更新 orders/positions/cash_flows/equity_curve
       → 事件：order.submitted / order.filled / order.rejected / risk.blocked
   → UI 持仓页即时刷新；日志/审计落库
```

**自动化闭环（M3）**

```
APScheduler（盘前 08:50 / 盘中 10:00 / 盘后 15:30 可配）
   → jobs.prepare()：刷新日历/自选池数据、加载风控配置、账户快照基线
   → jobs.scan()：读最新行情 → 各启用策略 on_bar → Signal 去重 → 批量走 OrderService（受风控）
   → jobs.summarize()：计算当日盈亏、写 equity_curve、生成日报、发通知
   → 每个 job 写 job_runs（状态/耗时/统计）；失败只告警不中断后续
```

### 3.3 事件主题（冻结）

| 主题 | payload 要点 | 发布者 |
|---|---|---|
| `market.bar` | symbol, trade_date, ohlcv, adj_factor | data/cache 更新成功后 |
| `signal` | trace_id, strategy_instance_id, symbol, side, strength, reason, ts | strategy / jobs |
| `order.submitted` | trace_id, order_id, symbol, side, price, qty, source | OrderService |
| `order.filled` | trace_id, order_id, price, qty, fee_breakdown | OrderService / PaperBroker 回报 |
| `order.rejected` | trace_id, order_id, reason_code, message | OrderService |
| `order.cancelled` | trace_id, order_id, cancelled_qty | OrderService |
| `risk.blocked` | trace_id, symbol, side, reason_code, details | OrderService（风控拦截时） |
| `job.started` / `job.finished` / `job.failed` | job_key, trace_id, stats / error | automation/scheduler |
| `notify.sent` | channel, event_type, status | automation/notify |
| `recommend.updated`（**Phase 2 扩展**） | run_id, kind(FAR/NEAR/RADAR), as_of, item_count | strategy/recommend |
| `watchlist.changed`（**Phase 2 扩展**） | group_id, action(ADD/REMOVE/UPDATE/IMPORT), count | data/watchlist |

事件总线要求：进程内、同步分发 + 异常隔离（单个订阅者抛异常不得影响发布者与其它订阅者）；订阅可按主题字符串注册。

---

## 4. A 股交易规则口径（核心，回测与模拟盘共用）

> 本节是**唯一口径来源**。`backtest/broker_sim.py` 实现一次，`trading/broker.py::PaperBroker` 复用，禁止两处各写一遍。

### 4.1 交易日历与交易时段

| 项目 | 规则 |
|---|---|
| 交易日 | 以 `trading_calendar` 表为准（akshare 交易日历落库）；周末与法定节假日休市 |
| 开盘集合竞价 | 09:15–09:25（09:20–09:25 不可撤单） |
| 连续竞价（上午） | 09:30–11:30 |
| 连续竞价（下午） | 13:00–15:00 |
| 收盘集合竞价 | 14:57–15:00（沪深均适用） |
| 日频 MVP 口径 | 只处理日线（`1d`）；交易时段仅用于“是否允许下单”的判定，不模拟盘内微观撮合 |

### 4.2 T+1 规则

1. 持仓拆分为 `quantity`（总持仓）与 `available_quantity`（可卖数量）。
2. 当日买入成交数量计入 `today_buy_quantity`，**不增加** `available_quantity`。
3. 每个交易日盘前（`jobs.prepare`）执行“日初结算”：`available_quantity = quantity`，`today_buy_quantity = 0`。
4. **资金口径**：卖出所得资金当日可用于买入（可用资金 T+0），但“可提现资金”T+1 才增加（MVP 不做提现，仅记录 `cash_flows` 类型区分）。
5. 卖出校验：`order.quantity ≤ available_quantity`，否则拒绝 `RK016`。

### 4.3 涨跌停规则

| 板块 / 状态 | 代码特征 | 涨跌幅限制 |
|---|---|---|
| 沪市主板 | 600/601/603/605 | ±10% |
| 深市主板 | 000/001/002/003 | ±10% |
| ST / *ST（**仅主板**） | 主板代码且简称含 ST | ±5% |

> 注：全面注册制下，**创业板/科创板的 ST 股票涨跌幅仍为 ±20%**（不适用主板 ST 的 ±5%）；`AStockRules.price_limits` 必须先判定板块，再判定 ST。
| 创业板 | 300/301 | ±20% |
| 科创板 | 688/689 | ±20% |
| 北交所（若纳入，见 OQ-2） | 8xxxxx / 920xxx | ±30% |
| 退市整理期 | 任意 | **MVP 直接黑名单**，不交易 |
| 新股上市初期 | 主板注册制上市前 5 个交易日、创/科前 5 个交易日、北交所首日 | 无涨跌幅限制 → **MVP 直接黑名单**（`min_listed_days` 默认 60 个交易日） |

**计算规则**：

- `limit_up = round(prev_close × (1 + limit_pct), 2)`，`limit_down = round(prev_close × (1 − limit_pct), 2)`，四舍五入至 0.01 元（人民币最小报价单位）。
- `prev_close` 为该标的**上一交易日收盘价（不复权）**；除权除息日的“前收盘价”为交易所口径的除权参考价（见 4.6）。
- 委托价格校验：买入限价 `price ≤ limit_up`，卖出限价 `price ≥ limit_down`，否则拒绝 `RK011`。
- 成交判定（保守口径，**回测与模拟盘一致**）：
  - 若当日 `open == high == low` 且等于涨停价（一字板）→ 买入委托当日**不成交**，记 `PRICE_LIMIT_UNMATCHED` 并从委托队列移除（默认不再追单）。
  - 若当日最高价触及涨停价但非一字板：买入委托价 < 涨停价 → 可成交；委托价 = 涨停价 → **按不成交处理**（保守，避免“排板成交”的乐观假设）。
  - 卖出对称处理跌停。
  - 撮合价：默认 T+1 开盘价 ± 滑点；若为限价单且开盘价不满足限价（买：开盘价 > 限价；卖：开盘价 < 限价），则用“日内可成交价”判定：买价取 `min(open, 限价)` 仅在 `low ≤ 限价` 时成交，否则当日不成交并顺延至下一交易日（默认最多顺延 1 日，之后作废，`EXPIRED`）。
- **ST 状态口径**：以数据层写入的“当日 ST 标记”为准；若历史 ST 标记不可得，退化为“按最新状态近似”，回测报告必须给出警告（`RQ_WARN_ST_APPROX`）。

### 4.4 最小交易单位与整手

| 板块 | 买入最小数量 | 递增单位 | 卖出 |
|---|---|---|---|
| 沪深主板 / 创业板 | 100 股 | 100 股 | 可为零股，但零股必须一次性全部卖出 |
| 科创板 | 200 股 | 1 股（≥200 后） | 同上，零股一次性卖出 |
| 北交所（若纳入） | 100 股 | 1 股 | 同上 |

校验规则：

- 买入：`quantity ≥ lot_size` 且（主板/创业板：`quantity % 100 == 0`；科创板：`quantity ≥ 200`），否则 `RK017`。
- 卖出：`quantity ≤ available_quantity`；若 `quantity != available_quantity` 则 `quantity % 100 == 0`；若 `quantity == available_quantity` 允许零股。
- 最小下单金额：默认 1000 元（低于则拒绝 `RK012`，防止最低佣金侵蚀），配置键 `trading.min_order_amount`；**仅对买入生效**。
- **金额类校验仅约束买入**（Lead 裁决，2026-10-25）：`min_order_amount` 与 `max_single_order_value_pct`（`RK012`）只作用于买入委托；**卖出委托不受金额类校验限制**，仅受整手规则、持仓数量（含 T+1 可用）、停牌、涨跌停区间、交易时段等约束。

### 4.5 费用模型

| 费用 | 收取方向 | 默认费率 | 计算口径 |
|---|---|---|---|
| 佣金 | 买 + 卖 | 0.025%（万 2.5） | `max(成交金额 × rate, 5.00 元)`；5 元最低仅对股票生效，费率与最低值均可配 |
| 印花税 | **仅卖出** | 0.05% | `成交金额 × 0.0005`（2023-08-28 起减半）；ETF/基金不适用（MVP 不交易 ETF） |
| 过户费 | 买 + 卖 | 0.001% | `成交金额 × 0.00001`；沪深均双向 |
| 规费（证管 + 经手） | 买 + 卖 | 0.00541% | `成交金额 × 0.0000541`；默认单列展示，可配置合并进佣金 |
| 滑点（非费用，计入成交价） | 买 + / 卖 − | 0.05%（5bp） | 买入成交价 = 基准价 × (1 + 0.0005)，卖出 = 基准价 × (1 − 0.0005)，结果四舍五入至 0.01 元 |

**金额口径**：`成交金额 = 成交价 × 数量`（元）；单笔总费用 = 佣金 + 印花税 + 过户费 + 规费；买入总支出 = 成交金额 + 总费用；卖出净收入 = 成交金额 − 总费用。

**精度与舍入**：每项费用独立计算后**四舍五入到 0.01 元**再累加（与券商对账单口径一致）；测试用固定用例锁定（AC-F3-03）。

### 4.6 除权除息与复权

1. 数据层同时保存：不复权 OHLCV、`adj_factor`（后复权因子）、`adjust_events`（除权除息事件表：`ex_date`、`event_type`、`cash_per_share`、`stock_ratio`、`record_date`）。
2. **价格口径决策（Lead 裁决 2026-10-25，强制）**：
   - **撮合、手数（整手/科创板 200 股等）、资金核算一律使用不复权真实价格（raw OHLC）**；
   - **复权价（后复权 hfq）仅用于收益/基准绩效展示与分红处理**；
   - **hfq 合成价不得用于手数与资金核算**（否则“1 手”的价值与现金记账会失真）；
   - 回测输出中等价展示：交易明细用 raw 价与 raw 金额；绩效曲线可用复权序列计算收益率（避免除权跳空造成虚假盈亏）。
3. 现金分红：在 `ex_date` 记一笔 `cash_flows(type=DIVIDEND)`，金额 = `持有数量 × cash_per_share`。
4. 送股/转增：在 `ex_date` 持仓数量按 `stock_ratio` 增加，平均成本相应摊薄。
5. 若 `adj_factor` 或事件缺失：退化为不复权并在回测结果 `warnings` 中给出 `RQ_WARN_NO_ADJ`，UI 显著提示“结果可能因除权失真”。
6. 行情页默认展示**前复权**（贴近当前价格），可切换后复权/不复权；切换仅影响展示，不影响回测口径。
7. **M4 若需要“双价列”（raw 与 adj_factor 同时传入引擎）**，由 Lead 评估后单列任务，不在当前范围（当前引擎按需在内部合成复权序列）。
8. **配置键 `backtest.price_adjust`（Lead 批准 2026-10-25，默认 `none`）**：`none`＝不复权真实价撮合/算手数（默认、推荐）；`hfq`＝复权合成价全流程，**自动告警并关闭最小手数校验**（仅供研究对照，不代表真实成交与手数）。详见 §7 规则 6。

### 4.7 停牌与不可交易状态

| 状态 | 数据标记 | 系统行为 |
|---|---|---|
| 正常 | `symbols.trade_status = NORMAL` | 正常交易 |
| 停牌 | `daily_bars.is_suspended = 1` 且当日无有效 OHLC | 拒绝下单 `RK003`；回测中持仓按最近收盘价估值，不产生成交 |
| 退市整理 | `trade_status = DELISTING` | 黑名单 `RK002`；回测中若持有，按最后价格强制平仓并记警告 |
| 已退市 | `delist_date` 非空 | 不进入可交易池 `RK001` |
| 长期停牌（>60 交易日） | 由数据校验报告标记 | UI 行内标记，自动化跳过并通知 |

### 4.8 集合竞价的简化处理（明确不追求微观精确）

1. **回测（日频）**：信号在 T 日收盘后产生，T+1 日**开盘价**成交（集合竞价成交价的近似）；提交时间视作 T+1 09:30。
2. 不使用盘中最高/最低价做“理想化成交”，避免未来函数；限价单成交判定只允许用 T+1 当日 OHLC 的保守规则（见 4.3）。
3. **模拟盘（日频）**：自动化 job 在 10:00（可配）读取最新快照价，以“最新价 ± 滑点”模拟成交；不模拟委托排队。
4. **撤单与部分成交**：PaperBroker 默认即时全额成交（简化）；仅在“手动撤单/开盘价不满足限价”场景产生 `CANCELLED`/`EXPIRED`。回测支持部分成交模拟为**可选开关**（默认关，避免复杂化）。
5. **不做**：开盘集合竞价的虚拟撮合、收盘集合竞价的尾盘竞价、盘口深度与排队位置、tick 级延迟建模（BRIEF 明确不做 L2）。

### 4.9 价格笼子 / 价格偏离（近似实现）

- 真实规则（2023 全面注册制）：连续竞价限价申报买入不高于基准价 102%、卖出不低于 98%（创业板/科创板另有“十个最小变动单位”孰高）。
- **MVP 实现**：风控层做“价格偏离校验”——限价与最新价偏离超过 `max_price_deviation_pct`（默认 2%）即拒绝 `RK010`，并提示“接近价格笼子限制/防手误”。UI 在价格输入框直接给出可申报区间。

### 4.10 回测 vs 模拟盘 口径差异表（必须一致的部分与允许差异）

| 项目 | 回测 | 模拟盘 | 是否一致 |
|---|---|---|---|
| T+1 与可用数量 | 一致 | 一致 | ✅ |
| 涨跌停不可成交 | 一致 | 一致 | ✅ |
| 整手校验 | 一致 | 一致 | ✅ |
| 费用计算 | 同一 `FeeModel` | 同一 `FeeModel` | ✅ |
| 成交价基准 | T+1 开盘价 | 当日快照最新价 | ⚠️ 允许差异（已在 UI/报告注明） |
| 部分成交 | 可选（默认关） | 默认不模拟 | ⚠️ 允许差异 |
| 除权除息 | 后复权 + 事件 | 事件表结算 | ✅ |
| 停牌 | 跳过/拒绝 | 拒绝 | ✅ |

---

## 5. 数据模型

### 5.1 通用约定

- 引擎：SQLite（WAL 模式），路径 `runtime/quantdesk.db`；通过 SQLAlchemy 2.0 ORM 定义；建表与轻量迁移在 `core/db.py`。
- 时间：交易日用 `DATE`（`YYYY-MM-DD`）；时间戳用 `DATETIME`（本地时区 `Asia/Shanghai`，统一带时区标注）。
- 金额：`REAL`（元）；价格 `REAL`；数量 `INTEGER`（股）。
- 所有业务表带 `created_at` / `updated_at`（UTC 或统一本地时间，二选一由 Dev1 在 `core/models.py` 冻结，本文按本地时间）。
- 主键：业务实体用 `TEXT` UUID（订单/成交）或 `INTEGER AUTOINCREMENT`（日志/记录类），见各表。
- 索引：按查询场景建（见各表）；`daily_bars` 必须有 `(symbol, trade_date)` 唯一约束保证幂等。
- 数据保留：日志 30 天滚动、审计 1 年、回测记录保留最近 200 次（UI 可清理）、行情保留全量。

### 5.2 数据域划分（22 张表）

**A. 行情与研究域（Dev1 主写）**

| 表 | 关键字段（类型 / 约束） | 说明 |
|---|---|---|
| `symbols` | `symbol` TEXT PK（如 `600519.SH`）、`code`、`name`、`exchange`（SH/SZ/BJ）、`board`（MAIN/GEM/STAR/BSE）、`kind`（STOCK/INDEX）、`lot_size` INT=100、`tick_size` REAL=0.01、`price_limit_pct` REAL、`is_st` BOOL、`list_date` DATE、`delist_date` DATE、`trade_status` TEXT、`updated_at` | 标的主数据；`board`+`is_st` 决定涨跌停幅度 |
| `trading_calendar` | `trade_date` DATE PK、`is_open` BOOL、`prev_trade_date` DATE、`next_trade_date` DATE | 交易日历；`prev/next` 用于 T+1 与顺延 |
| `daily_bars` | `symbol` TEXT、`trade_date` DATE、`open/high/low/close` REAL、`volume` INT（股）、`amount` REAL（元）、`adj_factor` REAL、`turnover_rate` REAL、`is_suspended` BOOL、`source` TEXT、`updated_at`；**PK(symbol, trade_date)**；索引 `(trade_date)` | 日线（股票与指数共用，靠 `symbols.kind` 区分）；唯一键保证重复拉取不重复写 |
| `fundamentals` | `symbol`、`report_date` DATE、`ann_date` DATE、`pe_ttm`、`pb`、`ps_ttm`、`roe`、`total_mv`、`revenue_yoy`、`profit_yoy`、`source`、`updated_at`；**PK(symbol, report_date)** | 基本面快照（多因子策略用）；MVP 只需 6–8 个字段 |
| `adjust_events` | `id` INT PK、`symbol`、`ex_date` DATE、`event_type`（DIVIDEND/SPLIT/RIGHTS）、`cash_per_share` REAL、`stock_ratio` REAL、`record_date` DATE、`source`；唯一 `(symbol, ex_date, event_type)` | 除权除息事件 |
| `data_quality_issues` | `id` INT PK、`run_id` TEXT、`symbol`、`trade_date` DATE、`issue_code` TEXT（DQ001..）、`severity`（ERROR/WARN）、`detail` TEXT、`created_at` | 数据校验报告（F1 验收） |

**B. 策略与回测域（Dev2 主写；表由 Dev1 建）**

| 表 | 关键字段 | 说明 |
|---|---|---|
| `strategy_instances` | `id` TEXT PK、`name` UNIQUE、`strategy_key`（注册表 key）、`params_json` TEXT、`universe_json` TEXT（标的池）、`mode`（OFF/PAPER/AUTO）、`enabled` BOOL、`last_signal_at`、`created_at`、`updated_at` | 策略实例（UI 策略页管理，自动化读取） |
| `strategy_signals` | `id` INT PK、`trace_id`、`strategy_instance_id`、`symbol`、`side`、`strength` REAL、`price_hint` REAL、`reason` TEXT、`signal_time`、`status`（GENERATED/RISK_BLOCKED/ORDERED/IGNORED）、`order_id`、`created_at` | 信号留痕（审计 + 策略页预览） |
| `backtest_runs` | `id` TEXT PK、`strategy_key`、`params_json`、`universe_json`、`start_date`、`end_date`、`initial_cash` REAL、`fee_config_json`、`status`（RUNNING/DONE/FAILED）、`metrics_json`、`warnings_json`、`error_msg`、`created_at`、`finished_at` | 回测记录（结果可对比的数据基础） |
| `backtest_equity` | `run_id`、`trade_date`、`equity`、`cash`、`position_value`、`benchmark_close`、`drawdown`；**PK(run_id, trade_date)** | 回测权益曲线 |
| `backtest_trades` | `id` INT PK、`run_id`、`trade_date`、`symbol`、`side`、`price`、`quantity`、`amount`、`commission`、`stamp_tax`、`transfer_fee`、`reg_fee`、`slippage_cost`、`fee_total`；索引 `(run_id)` | 回测成交明细 |

**C. 交易与执行域（Dev3 主写；表由 Dev1 建）**

| 表 | 关键字段 | 说明 |
|---|---|---|
| `accounts` | `id` TEXT PK、`name` UNIQUE、`broker`（PAPER/CCXT/QMT）、`base_currency`='CNY'、`initial_cash` REAL、`cash` REAL、`frozen_cash` REAL、`total_asset` REAL、`enabled` BOOL、`updated_at` | 模拟账户（MVP 单账户，结构支持多账户） |
| `positions` | `account_id`、`symbol`、`quantity` INT、`available_quantity` INT、`frozen_quantity` INT、`today_buy_quantity` INT、`avg_cost` REAL、`last_price` REAL、`market_value` REAL、`updated_at`；**PK(account_id, symbol)** | 持仓；`available_quantity` 体现 T+1 |
| `orders` | `id` TEXT PK（UUID）、`client_order_id` TEXT UNIQUE（幂等键）、`account_id`、`symbol`、`side`（BUY/SELL）、`order_type`（LIMIT/MARKET）、`price` REAL、`quantity` INT、`filled_quantity` INT、`avg_fill_price` REAL、`status`（PENDING/SUBMITTED/PARTIALLY_FILLED/FILLED/CANCELLED/REJECTED/EXPIRED）、`source`（MANUAL/AUTO）、`strategy_instance_id`、`reject_reason` TEXT（RK/BR 码）、`reject_message`、`risk_checks_json`、`trace_id`、`created_at`、`submitted_at`、`finished_at`、`updated_at`；索引 `(account_id, status, created_at)`、`(trace_id)` | 订单主表；状态机唯一事实来源 |
| `trades` | `id` TEXT PK、`order_id`、`account_id`、`symbol`、`side`、`price`、`quantity`、`amount`、`commission`、`stamp_tax`、`transfer_fee`、`reg_fee`、`fee_total`、`traded_at`、`source`；索引 `(order_id)`、`(account_id, traded_at)` | 成交回报 |
| `cash_flows` | `id` INT PK、`account_id`、`flow_type`（DEPOSIT/WITHDRAW/BUY/SELL/DIVIDEND/FEE/OTHER）、`amount` REAL、`balance_after` REAL、`ref_type`、`ref_id`、`occurred_at`、`note`；索引 `(account_id, occurred_at)` | 资金流水（对账基础） |
| `equity_curve` | `account_id`、`trade_date`、`total_asset` REAL、`cash` REAL、`position_value` REAL、`daily_pnl` REAL、`daily_return` REAL；**PK(account_id, trade_date)** | 实盘/模拟资金曲线 |

**D. 运维与配置域（Dev1 建表；各模块写入）**

| 表 | 关键字段 | 说明 |
|---|---|---|
| `risk_config` | `id` INT PK=1（单行）、`params_json` TEXT、`updated_at`、`updated_by` | 风控参数 DB 覆盖（优先级高于 config.yaml，便于 UI 修改） |
| `app_settings` | `key` TEXT PK、`value_json` TEXT、`updated_at` | 其它 UI 可改设置（通知渠道、自选池、展示偏好） |
| `audit_logs` | `id` INT PK、`ts`、`actor`（`manual`/`scheduler`/`system`）、`action`（如 `order.place`/`risk.config.update`/`job.manual_trigger`）、`target_type`、`target_id`、`before_json`、`after_json`、`result`、`trace_id`；索引 `(ts)`、`(target_type, target_id)` | 审计流水（不可删改） |
| `job_runs` | `id` INT PK、`job_key`、`job_name`、`trace_id`、`scheduled_at`、`started_at`、`finished_at`、`status`（SUCCESS/FAILED/SKIPPED）、`message`、`stats_json` | 调度执行记录 |
| `notifications` | `id` INT PK、`channel`（DESKTOP/EMAIL/WEBHOOK）、`event_type`、`title`、`body`、`status`（SENT/FAILED/DISABLED）、`error`、`sent_at` | 通知发送记录 |

### 5.3 关键一致性约束

1. `orders` 与 `trades` 的成交数量之和必须等于 `filled_quantity`（对账断言）。
2. `positions.quantity` 必须等于该标的 `trades` 的净成交量（虚拟账户口径），由 `portfolio` 维护并在启动时校验。
3. `cash_flows.balance_after` 必须与 `accounts.cash` 单调一致（启动时校验，不一致则先备份再修正并告警）。
4. `daily_bars` 同一 `(symbol, trade_date)` 只允许一条；写入用 upsert（SQLite `ON CONFLICT DO UPDATE`）。
5. 单账户下 `equity_curve` 每交易日一条；重复写覆盖（幂等）。

### 5.4 Phase 2 数据模型（F10–F13，**CF-7 扩展，已由 Lead 批准 2026-10-25**）

> 以下 5 张表为 M6 新增；**按“扩展新增、不改动已有 22 表”的方式处理**（已冻结 22 张表的字段/语义不得变化）。实现随 `core/models.py` + `core/db.py` 迁移（`schema_version` 递增）。

| 表 | 关键字段（类型 / 约束） | 说明 |
|---|---|---|
| `watchlist_groups` | `id` TEXT PK、`name` TEXT UNIQUE（≤20 字）、`description`、`sort_order` INT、`created_at`、`updated_at` | 自选池分组（默认「全部/远期/近期/手动」，可增删改） |
| `watchlist_items` | `id` INT PK、`group_id` TEXT、`symbol` TEXT、`note`、`tags_json`、`source`（MANUAL/IMPORT/RECOMMEND）、`is_active` BOOL、`sort_order` INT、`created_at`、`updated_at`；**UNIQUE(group_id, symbol)**；索引 `(symbol)` | 自选池条目；一个标的可属多个分组（多行） |
| `recommend_runs` | `id` TEXT PK、`kind`（FAR/NEAR/RADAR）、`as_of` DATE、`method` TEXT、`params_json`、`universe_json`、`status`（RUNNING/DONE/FAILED）、`metrics_json`（策略级回测）、`oos_metrics_json`（样本外）、`warnings_json`、`data_cutoff`、`created_at`、`finished_at` | 一次推荐/雷达生成批次（可复现：记录方法/权重/数据截止） |
| `recommend_items` | `id` INT PK、`run_id` TEXT、`rank` INT、`symbol`、`score` REAL、`reason_json`（≥2 条可解释理由 + 贡献值）、`factor_json`（因子拆解）、`suggested_hold_days`、`suggested_position_pct`、`risk_flags`、`backtest_metrics_json`、`oos_metrics_json`；索引 `(run_id, rank)` | 单条推荐/异动事件（含可解释性与样本外字段） |
| `research_runs` | `id` TEXT PK、`strategy_key`、`kind`（ML_RANKING/WALK_FORWARD）、`data_start`/`data_end` DATE、`folds_json`、`params_json`、`seed` INT、`deps_json`、`is_metrics_json`（样本内）、`oos_metrics_json`、`robustness_json`、`status`、`created_at`、`finished_at` | 策略研究记录（样本内/外、折数、随机种子、依赖版本，保证可复现） |

**一致性约束（Phase 2）**：①`recommend_items` 必须能追溯到 `recommend_runs` 的 `data_cutoff`，禁止使用截止日之后的数据；②`research_runs.oos_metrics_json` 为空时，UI 不得展示“可用”状态；③`watchlist_items.symbol` 必须通过代码归一化校验（6 位 + 市场后缀）。
**F14/F15 复用说明（2026-10-26）**：全市场筛选不新增表——扫描结果写 `recommend_runs(kind=SCREEN)` + `recommend_items`；有效性验证写 `research_runs(kind=SCREEN_VALIDATION)`。`SCREEN`/`SCREEN_VALIDATION` 为 `kind` 的新增枚举值（扩展枚举，不新增表结构）。

---

## 6. 接口契约（冻结，变更需 Lead 审批）

> 下列签名是**团队公共契约**。类型名统一放在 `core/models.py`（pydantic v2）；ORM 表同文件。实现方不得擅自改方法名、参数顺序、返回字段名。

### 6.1 数据层（Dev1）

```python
# data/provider.py
class DataProvider(ABC):
    name: str
    def get_bars(self, symbols: list[str], start: date, end: date, freq: str = "1d") -> pd.DataFrame: ...
        # 必含列: date,symbol,open,high,low,close,volume,amount; 可选: adj_factor,turnover_rate,is_suspended
    def get_trading_calendar(self, start: date, end: date) -> list[date]: ...
    def get_symbols(self, kind: str = "STOCK") -> pd.DataFrame: ...
    def get_snapshot(self, symbols: list[str]) -> pd.DataFrame: ...      # 最新价/涨跌幅/快照时间
    def get_fundamentals(self, symbols: list[str], report_date: date | None = None) -> pd.DataFrame: ...
    def health_check(self) -> ProviderHealth: ...                        # status: OK/DEGRADED/DOWN + last_error + latency_ms

class ProviderRegistry:                       # data/provider.py
    def register(self, name: str, cls: type[DataProvider]) -> None: ...
    def get(self, name: str) -> DataProvider: ...
    def resolve(self, primary: str, fallbacks: list[str]) -> DataProvider: ...  # 主备降级
```

```python
# data/cache.py
class BarCache:
    def update(self, symbols: list[str], start: date, end: date, freq: str = "1d") -> UpdateReport: ...
        # 增量：已存在区间不重复拉；返回 added/updated/skipped 行数与 source
    def load(self, symbols: list[str], start: date, end: date, adjust: str = "hfq") -> pd.DataFrame: ...
    def validate(self, symbols: list[str], start: date, end: date) -> ValidationReport: ...
        # 检测 DQ001..DQ007，写 data_quality_issues，返回问题清单
    def latest_date(self, symbols: list[str]) -> date | None: ...
```

### 6.2 策略层（Dev2）

```python
# strategy/base.py
@dataclass(frozen=True)
class Context:
    now: datetime
    bars: dict[str, pd.DataFrame]        # symbol -> 截止当前交易日的历史（含当日）
    positions: dict[str, Position]
    account: Account
    params: dict[str, Any]
    is_backtest: bool

class Strategy(ABC):
    key: ClassVar[str]                   # 注册表唯一键
    display_name: ClassVar[str]
    @classmethod
    def params_schema(cls) -> dict: ...  # JSON-Schema-lite：{field: {type, default, min, max, step, label, help}}
    def on_init(self, ctx: Context) -> None: ...
    def on_bar(self, ctx: Context) -> list[Signal]: ...

def register(key: str) -> Callable[[type[Strategy]], type[Strategy]]: ...
def get_strategy(key: str) -> type[Strategy]: ...
def list_strategies() -> list[dict]: ...   # key/display_name/params_schema，用于 UI 动态表单
```

新增策略 = 在 `strategy/builtin/` 新增 1 个文件 + `@register` 装饰器，**不改任何其它文件**（F2 验收）。

### 6.3 交易执行层（Dev3）

```python
# trading/broker.py
class BrokerAdapter(ABC):
    name: str
    def submit_order(self, order: OrderRequest) -> OrderResult: ...
    def cancel_order(self, order_id: str) -> OrderResult: ...          # BRIEF 约定 cancel_order(id)
    def get_positions(self) -> list[Position]: ...
    def get_account(self) -> Account: ...
    # 可选能力（非 MVP 必需）：get_order(order_id) -> Order | None；is_available() -> bool
```

```python
# trading/risk.py
class RiskGate:
    def __init__(self, config: RiskConfig | dict | None = None) -> None: ...
        # 参数来源（Lead/Dev3 口径 2026-10-25）：config.yaml 的 risk 段是唯一运行时来源；
        # core.models.RiskConfig 仅作 DTO；传 None 时由实现经 core.config 读取配置（缺配置=安全默认）
    def check(self, order: OrderRequest, account: Account,
              positions: list[Position], market: MarketSnapshot) -> RiskDecision: ...
    # 纯函数：不得写库、不得发网络请求、不得有随机性；相同输入必得相同输出
    # 判定顺序固定（见 8.6 / 9.2），返回第一个失败原因码
```

```python
# trading/order_manager.py
class OrderService:
    def place(self, req: OrderRequest) -> OrderResult: ...
        # 唯一入口：参数校验 → RiskGate.check → 落 orders → broker.submit_order → 更新持仓/资金 → 发事件
        # 风控拒绝时：订单落库为 REJECTED，reason_code=RKxxx，并发 risk.blocked
    def preview(self, req: OrderRequest) -> RiskPreview: ...
        # 只读预检：风控结论 + 费用明细 + 成交后仓位占比（UI 用，不落库、不下单）
    def cancel(self, order_id: str) -> OrderResult: ...
```

```python
# trading/portfolio.py
class PortfolioService:
    def get_account(self, account_id: str) -> Account: ...
    def get_positions(self, account_id: str) -> list[Position]: ...
    def apply_fill(self, fill: Fill) -> None: ...          # 更新持仓/可用/成本/现金 + cash_flows
    def settle_new_day(self, account_id: str, trade_date: date) -> None:   # T+1 日初结算
    def snapshot_equity(self, account_id: str, trade_date: date, prices: dict[str, float]) -> EquityPoint: ...
    def reconcile(self, account_id: str) -> ReconcileReport: ...           # 启动对账
```

### 6.4 回测层（Dev2）

```python
# backtest/engine.py
class BacktestEngine:
    def run(self, strategy_key: str, params: dict, symbols: list[str],
            start: date, end: date, initial_cash: float = 100000.0,
            fee_config: FeeConfig | None = None, slippage_bps: float = 5.0,
            benchmark: str = "000300.SH") -> BacktestResult: ...
        # BacktestResult: metrics(dict), equity(DataFrame), trades(DataFrame), warnings(list[str]), run_id

# backtest/broker_sim.py
class AStockRules:            # 唯一实现：涨跌停幅度/整手/T+1/不可成交判定
    @staticmethod
    def price_limits(prev_close: float, board: str, is_st: bool) -> tuple[float, float]: ...
    @staticmethod
    def validate_lot(side: str, quantity: int, available: int, board: str) -> tuple[bool, str | None]: ...
    @staticmethod
    def can_fill(side: str, price: float, bar: Bar, limits: tuple[float, float]) -> bool: ...

class FeeModel:
    def calculate(self, side: str, price: float, quantity: int,
                  fees: FeeConfig) -> FeeBreakdown: ...   # commission/stamp_tax/transfer_fee/reg_fee/total

class SimBroker:              # 回测撮合器
    def match(self, pending_orders: list[Order], bar_by_symbol: dict[str, Bar],
              date: date) -> list[Fill]: ...

# backtest/metrics.py
def compute_metrics(equity: pd.DataFrame, trades: pd.DataFrame,
                    benchmark: pd.Series | None = None) -> dict: ...
    # 必含键: total_return, annual_return, max_drawdown, max_drawdown_abs,
    #         sharpe, win_rate, profit_factor, trade_count, calmar, volatility
    # max_drawdown 为负数（-0.25 表示回撤 25%），max_drawdown_abs 为其绝对值（正数）
```

### 6.5 自动化与通知（Dev3）

```python
# automation/scheduler.py
class SchedulerService:
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def is_running(self) -> bool: ...
    def status(self) -> list[JobStatus]: ...            # job_key/name/next_run_time/last_run/last_status
    def trigger_now(self, job_key: str) -> str: ...     # 返回 trace_id，手动触发

# automation/jobs.py
def job_prepare(trace_id: str) -> JobResult: ...
def job_scan(trace_id: str) -> JobResult: ...
def job_summarize(trace_id: str) -> JobResult: ...

# automation/notify.py
class Notifier:
    def send(self, event_type: str, title: str, body: str,
             channels: list[str] | None = None) -> NotifyReport: ...   # 单渠道失败不影响其它渠道
```

### 6.6 事件总线（Dev1）

```python
# core/events.py
class EventBus:
    def subscribe(self, topic: str, handler: Callable[[Event], None]) -> None: ...
    def publish(self, topic: str, payload: dict, trace_id: str | None = None) -> None: ...
    def unsubscribe(self, topic: str, handler: Callable[[Event], None]) -> None: ...
```

### 6.7 领域模型（`core/models.py` 必须包含的 pydantic 模型）

`Side`、`OrderType`、`OrderStatus`、`Signal`、`OrderRequest`、`Order`、`OrderResult`、`Fill`、`FeeConfig`、`FeeBreakdown`、`RiskConfig`、`RiskDecision`、`RiskPreview`、`MarketSnapshot`、`Account`、`Position`、`Bar`、`Context`（策略上下文可放 `strategy/base.py`，但必须与本文一致）、`ProviderHealth`、`JobStatus`、`JobResult`。

`RiskDecision` 字段：`allow: bool`、`reason_code: str | None`、`message: str`（中文）、`details: dict`、`checks: list[dict]`（每条规则的通过/未通过记录，供 UI 展示“为什么被拒”）。

`MarketSnapshot` 字段：`trade_date`、`is_trading_time`、`latest_prices: dict[str, float]`、`price_times: dict[str, datetime]`、`suspended: set[str]`、`delisting: set[str]`、`daily_open_asset: float`（当日开盘总资产，用于熔断）、`realized_pnl_today: float`。

**`MarketSnapshot` 向后兼容扩展字段（不改冻结签名，Lead/Dev3 口径 2026-10-25）**：

> `core.models.MarketSnapshot` 继承 `QDModel(extra="allow")`，风控判定所需的扩展信息以关键字参数透传；**不传时对应检查标记 `skipped`（缺关键数据时按 fail-closed 拒绝，见 §8.6 异常与降级）**。统一组装入口为 `trading/order_manager.py::assemble_market_snapshot()`（实现约定，非新增冻结契约）。

| 扩展字段 | 类型 | 用途 | 缺失时行为 |
|---|---|---|---|
| `universe` / `tradeable_symbols` | `set[str]` | 可交易池 → `RK001` | 跳过池校验（skipped） |
| `blacklist` / `system_blacklist` | `set[str]` | 系统黑名单补充 → `RK002` | 跳过（skipped） |
| `listed_days` | `dict[str, int]` | 上市交易日数 → `RK004` | 跳过（skipped） |
| `price_limits` / `limit_up_prices` / `limit_down_prices` | `dict[str, tuple/float]` | 涨跌停区间 → `RK011` | 跳过区间校验（skipped） |
| `existing_client_order_ids` | `set[str]` | 幂等键 → `RK019` | 跳过重复校验（skipped） |
| `recent_order_times` | `list[datetime]` | 下单频率 → `RK013` | 跳过频率校验（skipped） |
| `as_of` / `now` / `current_time` | `datetime` | 新鲜度与频率的确定性参照时间 | 退化为 `order.created_at` / 交易日 15:00 |
| `automation_enabled` | `bool` | 自动化总开关 → `RK020` | 取风控参数（默认 `False`，fail-closed） |
| `is_fused` / `fused` | `bool` | 熔断状态显式覆盖 → `RK009` | 按当日基线实时计算 |

### 6.8 Phase 2 接口（F10–F13，**扩展，已由 Lead 批准 2026-10-25**）

```python
# data/watchlist.py
class WatchlistService:
    def groups(self) -> list[WatchlistGroup]: ...
    def create_group(self, name: str, description: str = "") -> WatchlistGroup: ...
    def rename_group(self, group_id: str, new_name: str) -> WatchlistGroup: ...
    def delete_group(self, group_id: str) -> None: ...                    # 组内条目迁移到默认组或拒绝
    def list_items(self, group_id: str | None = None) -> list[WatchlistItem]: ...
    def add(self, symbol: str, group_id: str, note: str = "", tags: list[str] | None = None,
            source: str = "MANUAL") -> WatchlistItem: ...
    def remove(self, item_id: int) -> None: ...
    def update(self, item_id: int, note: str | None = None, tags: list[str] | None = None,
               sort_order: int | None = None) -> WatchlistItem: ...
    def import_csv(self, text: str, default_group: str) -> ImportReport: ...   # 预览差异 + 逐行错误
    def export_csv(self, group_id: str | None = None) -> str: ...
    def normalize_symbol(self, raw: str) -> str: ...                     # 600519 / 600519.SH / 中文名 → 标准代码

# strategy/recommend.py
class RecommendService:
    def build(self, kind: str, as_of: date, universe: list[str] | None = None,
              params: dict | None = None) -> RecommendResult: ...        # kind: FAR / NEAR
        # RecommendResult: run_id, kind, items[RecommendItem], warnings, backtest_metrics, oos_metrics
    def scan_abnormal(self, as_of: date, universe: list[str] | None = None,
                      params: dict | None = None) -> RadarReport: ...     # F11 异动雷达

# strategy/ml_ranker.py
class MLRanker:
    def fit(self, panel: dict, labels: pd.Series, params: dict | None = None) -> "MLRanker": ...
    def rank(self, panel: dict) -> pd.Series: ...                        # 截面排序分
    def explain(self, symbol: str) -> list[dict]: ...                    # 可解释理由（含贡献值）
    # CPU-only；scikit-learn 为可选依赖（Lead 批准后加入 requirements）；不引入深度学习框架

# backtest/validation.py
def walk_forward(strategy_key: str, params: dict, symbols: list[str], start: date, end: date,
                 folds: int = 3, purge_days: int = 5, embargo_days: int = 1,
                 initial_cash: float = 100000.0) -> ValidationResult: ...
    # ValidationResult: folds[FoldResult], oos_equity(DataFrame), oos_metrics(dict),
    #                   is_metrics(dict), robustness(dict), warnings(list[str])
```

> 上述接口为 **Phase 2 扩展**：不得改动 §6.1–§6.7 的 MVP 冻结签名；`strategy/base.py` 不新增抽象方法（推荐/研究为**服务层**，不污染策略插件契约）。

**F14/F15 新增接口（2026-10-26，M6 P0）**：

```python
# strategy/screener.py
class ScreenService:
    def run(self, as_of: date, universe: list[str] | None = None,
            params: dict | None = None) -> ScreenResult: ...
        # 分层漏斗：基础剔除→流动性→量价打分→过热剔除→TopN
        # ScreenResult: run_id, as_of, data_time, market_total, after_filter, scanned,
        #               items[ScreenItem], warnings(list[str])
    def validate(self, params: dict | None = None, start: date | None = None,
                 end: date | None = None) -> ScreenValidationResult: ...
        # 5 分组回测（未来 5/10/20 日收益/超额）、RankIC/ICIR/t、换手与扣费后收益、
        # 样本外判定 usable + 判定线（默认 RankIC>0.03 且 Top10 日超额>0）
    def export_csv(self, run_id: str) -> str: ...
    def load_run(self, run_id: str) -> ScreenResult: ...      # 历史结果可读回（可复现）
```

> 说明：`ScreenService` 为**服务层**，不新增 `Strategy` 抽象方法；所有计算必须按 `as_of` 截断（禁止未来数据），`universe=None` 表示全市场（分批扫描）。

---

## 7. 配置契约（`config.yaml`，冻结键名）

```yaml
app:
  name: QuantDesk
  version: "1.0.0"
  timezone: Asia/Shanghai
  data_dir: ./runtime
  log_level: INFO
database:
  url: sqlite:///./runtime/quantdesk.db
  echo: false
  backup_keep: 7
data:
  provider: akshare            # akshare | mock
  fallbacks: [mock]            # 主源失败自动降级
  universe: ["600519.SH", "000001.SZ", "300750.SZ"]   # 自选池（默认少量示例）
  history_years: 5
  adjust: hfq
  request_interval_ms: 300     # akshare 限流
  max_retries: 3
  stale_threshold_hours: 24
fees:
  commission_rate: 0.00025
  min_commission: 5.0
  stamp_tax_rate: 0.0005
  transfer_fee_rate: 0.00001
  reg_fee_rate: 0.0000541
  include_reg_fee_in_commission: false
  slippage_bps: 5
trading:
  account_name: 模拟账户
  broker: paper                # paper | ccxt | qmt
  initial_cash: 100000
  min_order_amount: 1000       # 单笔最小金额（仅买入；低于则 RK012）
  allow_market_order: false    # MVP 默认禁止市价单
risk:
  enabled: true
  max_position_per_symbol_pct: 20
  max_total_position_pct: 80
  max_daily_loss_pct: 3
  max_price_deviation_pct: 2
  max_single_order_value_pct: 10   # 单笔金额上限（仅买入；卖出豁免）
  max_orders_per_minute: 10
  min_listed_days: 60
  blacklist: []                # 用户自定义黑名单（ST/退市/新股由规则自动加）
  allow_auto_orders: false     # 自动交易授权（默认 false → AUTO 订单 RK020 fail-closed；
                               # 显式授权方式：本项设 true，或整份配置中 automation.enabled=true 自动映射为授权）
backtest:
  default_initial_cash: 100000
  benchmark: "000300.SH"
  price_adjust: none           # 回测价格口径（Lead 批准 2026-10-25）：
                               #   none = 不复权真实价撮合/算手数（默认，推荐）
                               #   hfq  = 复权合成价；自动告警并**关闭最小手数校验**（仅供研究对照，不用于实盘口径）
automation:
  enabled: false               # 默认关闭，需用户显式开启（安全默认）
  jobs:
    prepare:  "08:50"
    scan:     "10:00"
    summarize:"15:30"
notify:
  enabled: false
  channels:
    desktop: true
    email: false
    webhook: false
  email:
    smtp_host: ""
    smtp_port: 465
    username: ""
    password_env: QD_SMTP_PASSWORD     # 密码只从环境变量读，禁止写本文件
    to: ""
  webhook:
    url: ""

# ---- Phase 2（F10–F13，M6；CF-9 扩展，已由 Lead 批准 2026-10-25）：默认关闭 ----
recommend:
  enabled: false                     # 推荐榜单总开关（Phase 2 默认关闭）
  far_top_n: 20                      # 远期榜条数
  near_top_n: 20                     # 近期榜条数
  far_refresh_days: 20               # 远期榜刷新周期（交易日，约月度）
  near_refresh: daily                # 近期榜刷新（daily）
  min_history_days: 250              # 最短历史（不足不推荐）
  min_listed_days: 60                # 最短上市交易日（不足不推荐）
  exclude_st: true                   # 是否剔除 ST/退市/新股
  radar_enabled: true                # 异动雷达开关
  radar_volume_ratio: 2.0            # 量比阈值
  radar_turnover_pct: 5.0            # 换手率阈值（%）
  radar_amplitude_pct: 7.0           # 振幅阈值（%）
  radar_dedup_days: 3                # 同标的同类型去重天数
research:
  ml_enabled: false                  # ML 排序（需安装 scikit-learn；CPU-only）
  walk_forward_folds: 3              # 样本外折数（≥3）
  purge_days: 5                      # 训练/验证之间的交易日 purge
  embargo_days: 1
  label_horizon_days: 5              # ML 标签未来收益天数
  param_robustness_pct: 20           # 参数稳健性邻域（±%）
# ---- F14/F15（全市场筛选与验证，2026-10-26；M6 P0，默认关闭）----
screen:
  enabled: false                     # 全市场筛选总开关
  top_n: 50                          # 输出候选数
  min_amount_20d: 50000000           # 流动性：近 20 日日均成交额下限（元）
  min_turnover_pct: 1.0              # 流动性：换手率下限（%）
  min_price: 1.0                     # 流动性：价格下限（元）
  overheat_return_5d_pct: 30.0       # 过热剔除：近 5 日涨幅上限（%）
  overheat_amplitude_pct: 15.0       # 过热剔除：单日振幅上限（%）
  overheat_volume_ratio: 5.0         # 过热剔除：量比上限
  validate_rank_ic_min: 0.03         # F15 判定线：样本外 RankIC 下限
  validate_top_excess_10d_min: 0.0   # F15 判定线：Top 组 10 日超额（扣费后）下限
```

规则：

1. 密钥/密码**绝不写入 config.yaml、数据库、日志**；只从环境变量读取（`.env` 由用户自建并 gitignore）。
2. `risk_config` 表存在时覆盖 `risk.*`（UI 修改风控参数写表 + 审计）；其余配置以 YAML 为准。
3. 启动时做配置校验（pydantic），非法值给出中文可操作提示并保留上次可用配置。
4. 所有金额/百分比参数不得硬编码在代码里（F 验收抽查）。
5. **Phase 2 扩展（已批准 2026-10-25）**：`recommend.*` 与 `research.*` 仅在 M6 阶段生效；MVP 阶段忽略这些键（但配置校验不报错）。`research.ml_enabled=true` 时若未安装 scikit-learn，系统降级为“非 ML 多因子”并中文提示。
6. **`backtest.price_adjust`（Lead 批准 2026-10-25，默认 `none`）**：
   - `none`（默认，推荐）：**不复权真实价**用于撮合/手数/资金核算；复权仅用于收益与基准绩效展示（与 §4.6 一致）。
   - `hfq`：使用**复权合成价**跑全流程；此时系统**自动发告警**（结果警告区/日志，中文说明“复权合成价口径，仅供研究对照，不代表真实成交与手数”），并**自动关闭最小手数校验**（避免合成价导致整手判断失真）。
   - 非法值/未知值：配置校验报错并回退 `none`；UI 明示当前口径。
7. **`screen.*`（F14/F15，2026-10-26 新增，M6 P0）**：全市场筛选阈值与验证判定线；MVP 阶段忽略（校验不报错），M6 生效。详见 §8.14/§8.15 与配置块。

---

## 8. 功能规格 F1–F15（F1–F9 为 MVP，F10–F15 为 Phase 2）

> 每个功能包含：用户故事 / 规则细节 / 接口与数据 / 异常与降级 / 验收标准（AC 编号，QA 按此写用例）。

### 8.1 F1 数据层

**用户故事**：作为用户，我希望打开软件后数据是自动更新好的，不需要我手工下载 CSV；断网时也能看到上次的数据。

**规则细节**

1. 数据源：默认 `AkshareProvider`（日线、指数、基本面、交易日历）；`MockProvider` 内置确定性的示例数据（不少于 10 只标的 × 3 年），保证无网络、无 akshare 时全流程可跑。
2. 主备降级：`provider` 失败（异常/超时/返回空）→ 按 `fallbacks` 顺序切换；主源恢复后下次任务自动切回；降级事件写日志并在 UI 顶部提示。
3. 增量更新：先查 `daily_bars` 已有最大交易日，只拉 `max_date+1 ~ today`；价格类数据用 upsert（`ON CONFLICT DO UPDATE`），**重复拉取不重复写、不改已有行的时间戳以外的值**（幂等）。
4. 请求节流：`request_interval_ms` 控制请求间隔；单次调用失败重试 `max_retries` 次（指数退避），仍失败则记 `DQ` 问题并继续（不中断整批）。
5. 数据校验（写入 `data_quality_issues`）：缺失交易日（对比日历）、重复行、价格跳变（|涨跌幅| > 20% 且无除权事件）、零成交量、复权因子缺失、最新数据过期（> `stale_threshold_hours`）、停牌未标记。
6. 数据范围：默认只拉自选池（`universe`）标的，避免 5000+ 标的全量拉取；全市场需求见 OQ-5。
7. 复权：保存不复权价 + `adj_factor`；`adjust_events` 尽量从数据源补齐，缺失时给出警告。

**接口与数据**：`DataProvider`（6.1）、`BarCache`（6.1）、表 `symbols` / `trading_calendar` / `daily_bars` / `fundamentals` / `adjust_events` / `data_quality_issues`。

**异常与降级**

| 场景 | 处理 |
|---|---|
| 无网络 / akshare 未安装 | 自动降级 MockProvider；UI 黄条“离线演示模式，数据为示例数据”；日志 WARN |
| 数据源限流（返回空/异常） | 退避重试 3 次 → 切备源 → 仍失败则保留旧数据并标记 `DQ006` 过期 |
| 单只标的失败 | 跳过该标的，记录问题，其余继续；错误率 > 50% 时任务整体 FAILED 并通知 |
| 数据库写入冲突 | upsert + 事务；失败回滚该批并记录 |
| 停牌/退市 | 写入状态标记，不删历史数据 |

**验收标准**

- **AC-F1-01**：对同一区间连续执行两次增量更新，`daily_bars` 行数不变、无重复 `(symbol, trade_date)`，第二次 `UpdateReport.added == 0`。
- **AC-F1-02**：人为构造 3 类数据缺陷（缺 2 个交易日、1 条重复、1 处无事件的价格跳变），`validate()` 能全部检出并写入 `data_quality_issues`，报告可在日志页查看。
- **AC-F1-03**：断网（mock provider 抛 `ConnectionError`）时，系统在 3 秒内降级到缓存 + Mock，页面不崩溃，顶部出现离线提示。
- **AC-F1-04**：`get_bars` 返回的列名与顺序符合契约；对不存在的标的返回空 DataFrame 而非异常。
- **AC-F1-05**：默认自选池首次全量拉取（10 只 × 5 年）≤ 60 秒；增量（1 天）≤ 5 秒。

### 8.2 F2 策略层

**用户故事**：作为用户，我想从列表里选一个内置策略、改几个参数就用；以后新增策略只需要放一个文件。

**规则细节**

1. 注册表：`@register("key")` 装饰器自动注册；`list_strategies()` 返回 key、中文名、参数 schema（UI 用来自动生成表单）。
2. 参数：每个策略用 `params_schema()` 声明（类型/默认值/范围/中文标签/帮助）；参数校验失败给出中文提示与合法范围。
3. 内置 3 个策略（MVP 必须全部可运行）：
   - `ma_cross` 双均线：参数 `fast=5`、`slow=20`、`position_pct=10`（单票目标仓位 %）；`fast` 上穿 `slow` 买入，下穿卖出。
   - `rsi_reversal` RSI 反转：参数 `period=14`、`oversold=30`、`overbought=70`、`position_pct=10`；RSI < oversold 买入，RSI > overbought 卖出。
   - `multi_factor` 多因子打分：参数 `momentum_window=20`、`volatility_window=20`、`top_n=3`、`rebalance_days=5`、`position_pct=10`；对标的池计算（动量 z-score − 波动率 z-score）排序取前 N 名等权，按周期调仓。
4. 策略只产出 `Signal`，**不得下单、不得访问 DB、不得联网**；同一 bar 同一标的同一方向只产生一次信号（引擎侧去重）。
5. 指标计算：优先 TA-Lib；导入失败时使用 pandas 自实现同名指标（`sma/ema/rsi/macd/std`），**接口保持一致**；UI 设置页显示“TA-Lib 已安装/已使用兜底实现”。
6. 防未来函数：`ctx.bars` 只包含 `<= 当前交易日` 的数据；策略不得访问更晚数据（引擎提供切片，不给全量 DataFrame）。

**接口与数据**：`Strategy`/`register`/`Context`/`Signal`（6.2）、表 `strategy_instances`、`strategy_signals`。

**异常与降级**：策略抛异常 → 该策略本轮标记失败、记录 traceback 到日志、不影响其它策略与其它 symbol；参数非法 → 拒绝保存并提示；标的池为空 → 跳过并提示；数据不足（如不足 slow+1 根 bar）→ 该标的跳过并记 INFO。

**验收标准**

- **AC-F2-01**：`list_strategies()` 返回 3 个策略，各有完整参数 schema；UI 策略页能根据 schema 自动渲染表单（不写死字段）。
- **AC-F2-02**：新增一个测试策略文件（QA 在测试中用临时注册）后，无需改动其它文件即可在列表中看到并可回测。
- **AC-F2-03**：对同一份固定行情数据，3 个策略输出的信号数量与方向可复现（两次运行结果完全一致）。
- **AC-F2-04**：TA-Lib 不可用时（monkeypatch import 失败），3 个策略仍能跑通，指标值与 TA-Lib 版本差异 ≤ 1e-6（或均在容差内）。
- **AC-F2-05**：修改参数（如 `slow=60`）后回测结果随之变化；非法参数（`fast >= slow`）被拒绝并提示原因。

### 8.3 F3 回测引擎

**用户故事**：作为用户，我想用接近真实的 A 股规则验证策略，并相信结果没有“骗人”。

**规则细节**

1. 事件驱动、逐日循环（`for date in trading_days`）：加载当日 bar → 处理前一日挂单（T+1 开盘撮合）→ 更新持仓估值 → 调用策略 `on_bar` → 生成信号 → 转为“次日待执行订单” → 记录权益。
2. 撮合规则严格按第 4 节：T+1、涨跌停不可成交、整手、费用、滑点、停牌。
   **价格口径（Lead 裁决 2026-10-25）**：撮合、手数（整手/科创板 200 股等）、资金核算一律使用**不复权真实价格**（默认 `backtest.price_adjust: none`）；复权价（hfq）仅用于收益/基准绩效展示与分红处理；**hfq 合成价不得用于手数与资金核算**；交易明细用 raw 价与 raw 金额，绩效曲线可用复权序列计算收益率（详见 §4.6）。若显式设 `backtest.price_adjust: hfq`（仅供研究对照），系统自动告警并关闭最小手数校验。若 M4 需要“双价列”（raw + adj_factor 同时传入引擎），再单列任务。
3. 指标（`metrics.py` 必出）：`total_return`、`annual_return`（按 252 交易日）、`max_drawdown`（**负值**，`-0.25` 表示最大回撤 25%）、`max_drawdown_abs`（其绝对值，**正数**）、`sharpe`（日收益，rf=0，年化 √252）、`win_rate`（按平仓交易计）、`profit_factor`、`trade_count`、`volatility`、`calmar`（`= annual_return / |max_drawdown|`）。指标口径在 SPEC 内固定（Lead 裁决，2026-10-25），测试按此手算核对。
4. 交易明细：每笔成交含日期、代码、方向、价格、数量、金额、各项费用、滑点成本。
5. 权益曲线：每交易日含 equity/cash/position_value/benchmark_close/drawdown。
6. “结果可对比”：每次回测落 `backtest_runs`；UI 可选择 2 个历史回测叠加曲线 + 指标对比表。
7. 防过拟合**最低要求**（不扩大范围）：结果页必须展示警告——样本期 < 250 交易日、交易次数 < 30、未复权、停牌占比高；`warnings` 字段落库。
8. 基准：默认沪深 300（`000300.SH`），可配；基准缺失时曲线不画基准但给出提示。

**接口与数据**：`BacktestEngine.run`（6.4）、`SimBroker`/`AStockRules`/`FeeModel`、`compute_metrics`、表 `backtest_runs`/`backtest_equity`/`backtest_trades`。

**异常与降级**

| 场景 | 处理 |
|---|---|
| 区间内无数据/标的不存在 | 返回 FAILED + 中文原因，不写半截结果 |
| 初始资金不足以买 1 手 | 明确提示“资金不足，最小可买 1 手需 X 元” |
| 策略抛异常 | 中止回测，`status=FAILED`，保存 `error_msg`，UI 展示可操作建议 |
| 数据含停牌/退市 | 停牌跳过，退市按最后价平仓并警告 |
| 指标除零（无交易/无回撤/单日） | 返回 0 或 None 并在 `notes` 用中文说明；`max_drawdown` 无回撤时为 0.0，`calmar` 在 `max_drawdown == 0` 或年化无法计算时返回 None（不得抛异常） |
| 运行时间过长 | 单次回测 > 120 秒给出进度提示（UI 进度条 + 日志） |

**验收标准**

- **AC-F3-01**：构造“涨停日买入”用例，回测无成交且记录不可成交原因；构造“跌停日卖出”用例同理。
- **AC-F3-02**：构造“T 日买入、T 日卖出”用例，卖出被拒（T+1），T+1 日卖出成功。
- **AC-F3-03**：费用精确性：买 1000 股 @10.00 → 佣金 5.00、过户费 0.10、规费 0.54（四舍五入后 0.54）、合计 5.64；卖 1000 股 @10.00 → 佣金 5.00、印花税 5.00、过户费 0.10、规费 0.54、合计 10.64（具体数字以 `FeeModel` 单测锁定，容差 0.01）。
- **AC-F3-04**：整手校验：买 150 股主板被拒；买 200 股科创板通过；卖 0 股被拒。
- **AC-F3-05**：给定手工构造的权益序列，`max_drawdown`（负值口径）、`max_drawdown_abs`（正值）、`annual_return`、`sharpe` 与手算值一致（容差 1e-6）；无回撤时 `max_drawdown=0.0` 且 `calmar=None`、`notes` 有中文说明。
- **AC-F3-06**：3 个内置策略均可在默认自选池、近 3 年数据上跑通并产出完整指标与交易明细，全程无异常。
- **AC-F3-07**：同一参数两次回测结果完全一致（可复现）；两次回测可在 UI 对比。
- **AC-F3-08**：20 标的 × 5 年回测 ≤ 15 秒（本机基准）。

### 8.4 F4 模拟交易

**用户故事**：作为用户，我想像真实交易一样点几下就完成买卖，并且账户、持仓、流水都自动算对。

**规则细节**

1. 账户：MVP 单账户（`PAPER`），`initial_cash` 可配（默认 10 万），支持重置（清空持仓/订单，保留审计）。
2. 手动下单流程：标的 → 方向 → 价格类型/价格（默认最新价）→ 数量（步进 100，快捷 1/4、1/2、全部）→ **风控预检面板**（预计金额、费用明细、成交后仓位占比、风控结论）→ 二次确认 → 提交。
3. 提交后即时返回：`FILLED`（默认即时成交）或 `REJECTED`（带 RK/BR 码与中文原因 + 建议动作）。
4. 撤单：`SUBMITTED/PARTIALLY_FILLED` 可撤；`FILLED/CANCELLED/REJECTED/EXPIRED` 不可撤（返回错误码 `BR002` 或明确提示）。
5. 持仓与资金：成交后立即更新 `positions`（含 `available_quantity` T+1 语义）、`cash`、`cash_flows`、`equity_curve`（当日快照 upsert）。
6. 手工下单与自动下单写入同一张 `orders` 表，用 `source` 区分；审计记录 `actor=manual/auto`。
7. 市价单：MVP 默认**不允许**市价单（A 股实盘无真正市价单，且模拟盘无限价锚）；`order_type=MARKET` 时以快照价 + 滑点成交，需配置 `allow_market_order: true`（默认 false）。

**接口与数据**：`OrderService`（6.3）、`BrokerAdapter`/`PaperBroker`、`PortfolioService`、表 `accounts`/`positions`/`orders`/`trades`/`cash_flows`/`equity_curve`。

**异常与降级**

| 场景 | 处理 |
|---|---|
| 现金不足 | `RK014`，提示“还差 X 元（含费用）” |
| 持仓/T+1 不足 | `RK015`/`RK016`，提示“今日买入不可卖，可卖 0 股” |
| 标的不存在/退市 | `RK001`，提示搜索建议 |
| 重复点击提交 | `client_order_id` 幂等，返回同一订单结果（不重复下单） |
| PaperBroker 内部异常 | 订单置 `REJECTED(BR001)`，记录异常；账户数据回滚到提交前 |
| 页面并发操作（两个 tab） | DB 事务 + 行级检查；后到者按最新持仓重新风控 |

**验收标准**

- **AC-F4-01**：手动买入 100 股 → 订单 `FILLED`、持仓 +100、`available_quantity=0`、现金减少（含费用精确到分）、`cash_flows` 有 BUY 记录、`equity_curve` 当日更新。
- **AC-F4-02**：当日卖出该 100 股 → `RK016` 拒绝；次日（模拟日初结算后）卖出成功。
- **AC-F4-03**：撤单：`SUBMITTED` 状态可撤并退冻结；`FILLED` 撤单返回明确错误提示。
- **AC-F4-04**：同一 `client_order_id` 连续提交 3 次，只产生 1 条订单与 1 笔成交。
- **AC-F4-05**：买入后立刻查持仓页，数据与订单成交一致（数量、成本、市值、仓位占比）。
- **AC-F4-06**：账户初始资金 10 万，买入 100 股 10 元 → 现金余额与手算一致（误差 0）。

### 8.5 F5 自动化

**用户故事**：作为用户，我想设好策略后点一个开关，它就每天自己跑，我盘后看结果就行，出问题能一键停。

**规则细节**

1. 调度：APScheduler（BackgroundScheduler，本地时区 `Asia/Shanghai`），3 个固定 job：
   - `job_prepare` 盘前 08:50（可配）：刷新交易日历、更新自选池数据、日初结算（T+1 可用数量）、账户快照基线（用于熔断）、加载风控配置。
   - `job_scan` 盘中 10:00（可配）：拉快照 → 对每个启用的策略实例调用 `on_bar` → 信号去重 → 批量走 `OrderService`（逐单风控）→ 记录信号与订单。
   - `job_summarize` 盘后 15:30（可配）：计算当日盈亏、写 `equity_curve`、生成日报文本、发通知、清理过期数据。
2. 一键启停：自动化页总开关；停止时取消所有 job（不影响已提交订单状态）；启动时校验依赖（策略实例存在、数据源健康）。
3. 幂等：同一交易日同一策略实例同一标的同方向，`client_order_id = hash(strategy_instance_id, symbol, trade_date, side)`，重复触发不重复下单。
4. 交易日判定：非交易日 job 直接 `SKIPPED` 并记录原因（不报错）。
5. 失败处理：单个策略失败 → 跳过该策略继续其它；job 整体失败 → `job_runs.status=FAILED` + 通知；**任何 job 失败不得影响下一次调度**。
6. 手动触发：自动化页对每个 job 提供“立即运行”，同样写 `job_runs`，`actor=manual`。
7. 默认关闭（`automation.enabled: false`）：用户必须显式开启，防止装好即自动交易。

**接口与数据**：`SchedulerService`/`job_*`（6.5）、`OrderService`、表 `job_runs`/`strategy_signals`/`audit_logs`。

**异常与降级**

| 场景 | 处理 |
|---|---|
| 调度器已在运行再次 start | 幂等返回，不重复注册 job |
| 系统休眠/错过执行时间 | APScheduler `misfire_grace_time`（默认 300s）；错过过久则 `SKIPPED` 并通知 |
| 数据源不可用 | 盘中 job 放弃本次扫描并通知，不产生订单（绝不基于过期数据交易） |
| 行情过期（>阈值） | 风控 `RK006` 拦截所有订单，job 记为失败并告警 |
| 程序崩溃后重启 | 启动时对账：未完成订单置 `EXPIRED` 并通知；调度器按当前时间重新注册 |
| 自动化运行中用户手动操作 | 同一 `OrderService` 与风控，互不影响；审计区分 actor |

**验收标准**

- **AC-F5-01**：启动 → `SchedulerService.status()` 返回 3 个 job 及下次运行时间；停止 → 状态为未运行且不再产生 `job_runs`。
- **AC-F5-02**：手动触发一次 `job_scan`，日志中可用一个 `trace_id` 串起：信号 → 风控结论 → 订单 → 成交（或拦截原因）。
- **AC-F5-03**：同一交易日连续触发 2 次 `job_scan`，订单表不产生重复订单（幂等键生效）。
- **AC-F5-04**：非交易日触发 → `job_runs.status=SKIPPED`，不产生订单。
- **AC-F5-05**：某策略抛异常时，其它策略仍正常执行，job 结果统计里记录失败策略数。
- **AC-F5-06**：自动化总开关默认关闭；开启后运行、关闭后停止，均有审计记录。

### 8.6 F6 风控闸门

**用户故事**：作为用户，我担心自动交易失控；我希望所有下单（手动和自动）都必须过我设的规则，被拒时告诉我为什么。

**规则细节**

1. **唯一入口**：`OrderService.place()` 内部必调 `RiskGate.check()`；UI、自动化、脚本**禁止**直接调用 `BrokerAdapter.submit_order`。代码审查 + 单测双重保证（AC-F6-01）。
2. 纯函数：不写库、不联网、无随机；相同输入必得相同输出；每次返回 `checks` 明细（每条规则的通过/未通过 + 实际值/阈值）。
3. 判定顺序固定（返回第一个失败码）：
   1. 参数合法性（价格>0、数量>0、方向合法、整手、价格在涨跌停区间）→ `RK018/RK017/RK011`
   2. 交易时段/交易日 → `RK005`
   3. 标的可交易性（在池/非黑名单/非停牌/上市天数足够）→ `RK001/RK002/RK003/RK004`
   4. 行情新鲜度（快照时间超阈值）→ `RK006`
   5. 重复订单（幂等键）→ `RK019`
   6. 价格偏离（限价 vs 最新价）→ `RK010`
   7. 单笔金额上限 / 最小下单金额 / 下单频率 → `RK012/RK013`（**金额类仅约束买入；卖出委托跳过 RK012**；实现参数 `min_order_amount_enforce_sell` 默认 `False`，与口径一致）
   8. 账户状态（熔断中、账户停用、自动交易未授权时自动单）→ `RK009/RK020`
   9. 资金/持仓（买入现金含费用；卖出 T+1 可用）→ `RK014/RK015/RK016`
   10. 仓位额度（单票上限、总仓位上限）→ `RK007/RK008`
4. 上限计算口径：仓位占比 = `(现有市值 + 本次买入金额) / 账户总资产`；**卖出不受仓位上限与金额类校验（`RK012`）限制**（允许减仓，但不得卖空）；卖出仅受整手、持仓（含 T+1 可用）、停牌、涨跌停、交易时段约束。
5. 熔断：`(当前总资产 − 当日开盘总资产) / 当日开盘总资产 ≤ −max_daily_loss_pct` 时，**禁止买入**，允许卖出；次日盘前重置；手动解锁需在设置页二次确认并写审计。
6. 黑名单来源：系统自动（ST/*ST、退市整理、上市 < `min_listed_days`、停牌）+ 用户自定义（设置页/配置）；合并去重。
7. 风控参数热更新：UI 保存后立即生效（读 `risk_config` 表，带缓存失效），写审计（before/after）。
8. **参数来源与授权（Lead/Dev3 口径 2026-10-25）**：`config.yaml` 的 `risk` 段是唯一运行时来源；`core.models.RiskConfig` 仅作 DTO；`RiskGate(None)` 由实现经 `core.config` 读取配置（缺配置时用安全默认值）。
9. **自动交易授权**：新增参数 `allow_auto_orders`（默认 `false`）。`source=AUTO` 的订单默认按 `RK020` **fail-closed 拒绝**；显式授权方式：`risk.allow_auto_orders=true`，或整份配置中 `automation.enabled=true`（自动映射为授权）。手动单不受该开关影响。

**接口与数据**：`RiskGate.check`（6.3）、`RiskDecision`/`MarketSnapshot`、表 `risk_config`/`audit_logs`、拒绝原因码（第 9 节）、`trading/order_manager.py::assemble_market_snapshot()`（快照组装辅助）。

**异常与降级**：风控组件自身异常（理论上不应发生）→ **拒绝订单**（fail-closed），记 ERROR 与 `RK018`，绝不“放行”。**关键数据缺失即拒绝（fail-closed）**：快照缺少 `price_times`（无法确认新鲜度）→ `RK006`；缺少交易时段/交易日标记（且无法从交易日历推导）→ `RK005`；缺当前标的持仓信息（卖出）→ `RK015`。

**验收标准**

- **AC-F6-01**：代码层面唯一入口：`grep` 全仓 `submit_order(` 只出现在 `order_manager.py` 与测试中；单测用 mock broker 断言 `RiskGate.check` 在 `submit_order` 之前被调用至少一次。
- **AC-F6-02**：RK001–RK020 每个原因码至少 1 条单元测试覆盖（输入固定、断言 reason_code 与中文消息非空）。
- **AC-F6-03**：单票上限：总资产 10 万、单票上限 20%，已持有 15,000 元该标的 → 再买 10,000 元被 `RK007` 拒绝（允许额度 5,000）。
- **AC-F6-04**：总仓位上限：已有持仓 78%，再买入 5% → `RK008`。
- **AC-F6-05**：熔断：当日亏损 3.2% → 买入被 `RK009`，卖出正常通过；次日重置后买入恢复。
- **AC-F6-06**：价格偏离：最新价 10.00，限价 10.50（+5%）→ `RK010`；限价 10.10（+1%）→ 通过。
- **AC-F6-07**：风控纯函数：同一输入调用 2 次结果完全一致，且不产生任何数据库写入（测试用只读连接断言）。
- **AC-F6-08**：风控参数在 UI 修改后 1 秒内生效（下一单按新参数判定）。
- **AC-F6-09**：金额类校验仅约束买入：总资产 10 万、单笔上限 10%（1 万元）时，卖出市值 5 万元的持仓（持仓与可用足够）**通过**风控；同样的金额若为买入则被 `RK012` 拒绝（卖出豁免已由 Lead 裁决，见 §4.4）。
- **AC-F6-10**：自动交易授权 fail-closed：默认配置下 `source=AUTO` 的订单被 `RK020` 拒绝；`risk.allow_auto_orders=true` 或 `automation.enabled=true` 时放行；手动单在两种配置下均不受影响。
- **AC-F6-11**：关键数据缺失 fail-closed：快照缺 `price_times` → `RK006`；缺交易时段/交易日标记 → `RK005`；卖出缺少持仓信息 → `RK015`（不得静默放行）。

### 8.7 F7 Web UI

**用户故事**：作为代码能力弱的用户，我希望所有操作都在浏览器里点，不需要命令行，界面全中文。

**规则细节**

1. 入口：`app.py`（Streamlit），启动命令 `streamlit run app.py` 或双击 `scripts/start.bat`；MVP 9 个页面（见第 10 节），Phase 2 增至 **12 页**（+ `watchlist`/`recommend`/`screener`）。
2. 导航：侧边栏顶部显示应用名 + 环境状态（数据源、账户、调度器、离线提示）；页面间切换用 Streamlit 页面导航。
3. 统一交互规范：
   - 所有破坏性操作（下单、清仓、删除策略、重置账户、修改风控、启动自动化）需二次确认。
   - 所有失败提示为中文 + 原因码 + 建议动作（例：“单票仓位将达 23.4%，超过上限 20%〔RK007〕→ 建议减少 300 股”）。
   - 所有耗时操作显示进度/状态；不使用阻塞式无反馈操作。
   - 表格支持排序、导出 CSV。
   - 金额展示千分位、两位小数、涨红跌绿（A 股习惯）；时间统一本地时区。
4. 空状态与首次使用：无数据时给出“去拉取数据”按钮与说明；无账户时引导创建/重置。
5. 页面文件互不覆盖（BRIEF 文件级隔离）；共享逻辑放 `ui/pages/_common.py`（决策 S-2）。
6. 响应性：单页渲染 ≤ 2 秒（不含大数据查询）；查询加缓存（`st.cache_data`，TTL ≤ 60 秒，写操作后清缓存）。

**接口与数据**：只调用下层服务（`OrderService`/`SchedulerService`/`BacktestEngine`/`BarCache`/`PortfolioService`），不直接写 SQL。

**异常与降级**：页面级异常捕获 → 展示中文错误卡片 + trace_id + “查看日志”链接，页面不白屏；数据源离线 → 全局黄条 + 只读模式提示。

**验收标准**

- **AC-F7-01**：9 个页面全部可打开，无 Python 异常；空数据状态下有引导提示。
- **AC-F7-02**：不写任何代码完成 4 个核心任务：① 行情页看一只票 K 线；② 回测页跑出报告；③ 手动交易页买入；④ 自动化页开启调度并在日志页看到记录。
- **AC-F7-03**：所有破坏性操作都有二次确认，取消后不产生副作用。
- **AC-F7-04**：错误提示均为中文且带原因码与建议动作（抽查 5 类错误）。
- **AC-F7-05**：冷启动 ≤ 15s，页面切换 ≤ 2s（本机）。

### 8.8 F8 通知与日志

**用户故事**：作为用户，我希望系统自己记清楚发生了什么；关键事件能提醒我，但有开关。

**规则细节**

1. 结构化日志：`core/logging.py` 输出 JSON Lines 到 `runtime/logs/app-YYYYMMDD.log` + 控制台可读格式；字段：`ts, level, logger, trace_id, module, message, extra`。按日切分，保留 30 天。
2. 审计流水：写 `audit_logs`（下单、撤单、风控参数变更、策略启停、自动化开关、账户重置、数据源切换、备份恢复）；不可删除，UI 只读。
3. 通知渠道（至少一种可用）：
   - 桌面通知：Windows 原生 toast（无第三方依赖则用 `plyer`/PowerShell 兜底；实现由 Dev 决定，但必须在无网络时可用）。
   - 邮件：SMTP，密码从环境变量读；配置缺失时不启用。
   - Webhook：POST JSON（`{event_type,title,body,ts,trace_id}`），支持企业微信/飞书/钉钉格式（通用 JSON，用户自行适配）。
4. 通知事件：自动化 job 完成/失败、风控熔断触发、订单被拒（仅自动单，防刷屏）、日报。
5. 通知失败不影响主流程：记录 `notifications.status=FAILED`，重试 1 次，之后仅日志。
6. 通知全局开关 + 分渠道开关；`notify.enabled: false` 时只写日志。

**接口与数据**：`Notifier`（6.5）、`EventBus`、表 `audit_logs`/`notifications`/`job_runs`。

**验收标准**

- **AC-F8-01**：日志文件为有效 JSON Lines，每条含 `ts/level/trace_id/message`；一次手动下单能在日志中按 trace_id 追到全部步骤。
- **AC-F8-02**：审计：下单、风控参数修改、自动化开关各产生 1 条审计记录，含 before/after（参数修改）。
- **AC-F8-03**：桌面通知可用（或在无桌面环境时优雅降级为日志并说明）；配置错误（如 SMTP 密码缺失）时给出中文提示且不影响其他渠道。
- **AC-F8-04**：关闭通知后，触发事件仅写日志，`notifications` 表无新增或状态为 `DISABLED`。
- **AC-F8-05**：日志页可按级别/时间/关键词/trace_id 过滤，结果正确。

### 8.9 F9 实盘适配层

**用户故事**：作为用户，我希望以后接真实券商时不用重写系统；现在不配账户也能跑通全流程。

**规则细节**

1. `BrokerAdapter` 抽象（6.3）；`PaperBroker` 完整实现（账户、持仓、下单、撤单、成交回报）；`CcxtBroker`（加密 testnet，可选依赖 ccxt）与 `QmtBroker`（骨架）为 MVP 交付物。
2. `PaperBroker` 与回测共用 `AStockRules`/`FeeModel`，保证“回测 = 模拟盘”口径一致。
3. `CcxtBroker`：实现 `submit_order/cancel_order/get_positions/get_account` 的最小可用集（testnet），未安装 ccxt 或未配置 key 时 `is_available()=False`，UI 显示“未配置”，全流程仍走 Paper。
4. `QmtBroker`：骨架——类存在、方法签名齐全、调用抛 `NotImplementedError("QMT 尚未接入，请使用模拟盘")`；UI 选择 QMT 时明确提示“未接入，仅占位”。**不连接真实资金**。
5. 适配器注册表：`broker_registry`（name → class），配置 `trading.broker` 选择；切换 broker 不改业务代码。
6. 密钥安全：ccxt key/secret 只从环境变量读；UI 掩码显示；不写 DB、不进日志；`.gitignore` 排除 `.env`。

**接口与数据**：`BrokerAdapter`、表 `accounts`（broker 字段）。

**异常与降级**：ccxt 未安装 → 启动时不报错、UI 提示；ccxt 网络失败 → 订单 `REJECTED(BR001)` + 通知；QMT 调用 → 明确 `BR004`；任何 broker 异常不得破坏本地账户数据（本地为事实来源，重启对账）。

**验收标准**

- **AC-F9-01**：`PaperBroker` 全部方法可用，跑通手动下单/撤单/持仓/账户；无真实账户时全流程可用。
- **AC-F9-02**：未安装 ccxt 时系统正常启动，`CcxtBroker` 不可用但 UI 不报错；安装并配 testnet key 后可下 1 笔 testnet 单（可选验收，允许跳过并说明）。
- **AC-F9-03**：`QmtBroker` 调用返回明确中文错误码 `BR004`，不产生任何网络/资金行为。
- **AC-F9-04**：切换 `trading.broker` 只需改配置，无需改代码（抽查）。

---

## 8A. Phase 2 增值功能 F10–F15（M6，用户目标升级后新增）

> **范围声明**：本节为 **Phase 2（M6）** 功能，**不进入 M0–M5 的 MVP 交付**；入口条件见 `ROADMAP.md` M6。新增数据表/接口/配置键/警告码均标注“Phase 2 扩展，需 Lead 批准”，未批准前不实施。
> **合规总纲**：系统只提供研究工具与信号，**不承诺收益**；所有推荐必须展示回测区间、样本外表现与风险提示；UI 固定展示 **UBA（用户自担风险）声明**（详见 §11.8）。推荐/研究结果**默认不自动下单**。

### 8.10 F10 选股与推荐（Phase 2 / M6）

**用户故事**：作为用户，我希望系统每天给我两份**可解释**的榜单——远期价值型（长期持有）与近期异动型（短线机会）；我能看到“为什么推荐、历史（含样本外）表现如何、风险在哪”，而不是一句“买入”。

**规则细节**

1. **两套榜单（两条线）**：
   - **远期榜（FAR / 价值型）**：研究目标“持有 6–24 个月、年化 10–20% 量级”（**研究目标，不承诺**）。因子维度：估值（PE/PB 分位）、质量（ROE、毛利率、经营现金流）、成长（营收/利润增速）、低波动/股息；调仓频率默认月度（可配）。
   - **近期榜（NEAR / 异动型）**：研究目标“捕捉异动前兆与拉升起爆点（1–10 个交易日）”。因子维度：量比、成交额 z-score、换手突增、振幅、涨速、跳空缺口、尾盘异动、资金净流入（数据可得时）；调仓频率默认每交易日盘后（可配）。
2. **排名方法**：每因子先做截面标准化（z-score / 分位），加权合成 `score`；权重**版本化**——每次生成记录 `method/params/weights/source/数据截止日`，保证可复现、可对比。
3. **可解释性（强制）**：每条推荐必须给出 **≥2 条可读理由**，格式为“因子名 + 实际值/分位 + 对评分的贡献方向与贡献值”，例如“ROE 18.6%（全市场 92 分位，贡献 +0.21）”；**无理由不得上榜**（G7）。
4. **样本外强制**：榜单必须附“策略级回测区间 + 样本外（walk-forward）表现”；未完成样本外验证的榜单只能标记“**研究阶段**”，**禁止一键接入自动交易**（G8、AC-F13-02）。
5. **展示字段**（每条/每卡）：排名、代码、名称、板块、方向、综合评分、推荐理由（≥2）、因子拆解、建议持有周期、策略回测区间、样本外年化/最大回撤/夏普/胜率、风险提示、建议仓位上限（默认取风控单票上限的 50%）、数据截止日。
6. **生成与落库**：远期榜默认每周/每月、近期榜默认每交易日盘后（可配）；每次生成写 `recommend_runs` + `recommend_items`，支持历史对比。
7. **推荐不直接下单**：推荐 → 用户加入自选池 → 策略/手动下单；页面提供“加入自选池 / 去回测 / 去手动交易”跳转，**不提供“一键全自动买入”**（默认禁止；若未来放开须用户拍板新 OQ + 二次确认 + 审计）。推荐页“建议仓位”仅作提示，实际下单仍受风控闸门约束。

**数据模型（Phase 2 扩展，见 §5.4）**：`recommend_runs`、`recommend_items`。
**接口（Phase 2 扩展，见 §6.8）**：`strategy/recommend.py::RecommendService.build(kind, as_of, universe, params)`。
**异常与降级**：数据不足（上市 <60 日 / 历史 <250 日 / 停牌）→ 剔除并说明；某因子缺失 → 权重置 0 并列入“数据缺口”；样本外未完成 → 榜单降级“研究阶段”；无基准 → `RQ_WARN_NO_BENCHMARK` 且不展示超额收益。

**验收标准**

- **AC-F10-01**：抽查任意 10 条推荐，**100%** 含 ≥2 条可读理由（因子名 + 值/分位 + 贡献值），可追溯到因子拆解（G7）。
- **AC-F10-02**：远期/近期两套榜单可独立生成与查看；远期榜含估值/质量/成长/低波动因子拆解，近期榜含量比/换手/振幅/资金类因子拆解（数据缺失时明确标注）。
- **AC-F10-03**：榜单页面与导出报告必须同时展示：回测区间、样本外指标、风险提示、**UBA 声明**；缺任一项判不通过。
- **AC-F10-04**：数据不足标的被剔除且给出原因；缺因子/缺基准有中文降级提示（含 `RQ_WARN_NO_BENCHMARK`）。
- **AC-F10-05**：推荐不触发下单——点击“加入自选池”只写自选池、不产生订单；跳转回测/手动交易正确；推荐页无“一键自动买入”入口。

### 8.11 F11 异动雷达（Phase 2 / M6）

**用户故事**：作为用户，我希望每天看到“哪些票在异动、资金流入/换手突增/接近起爆点”，并说明触发了哪条规则、离阈值差多少。

**规则细节**

1. **扫描范围**：自选池（默认）+ 可选指数成分；**日频盘后**为主，盘中快照可选（数据源支持时）。
2. **触发规则（阈值可配，以下为默认值）**：量比 ≥ 2.0（5 日量比）或成交额 z-score(20) ≥ 2；换手率 ≥ 5% 或 20 日分位 ≥ 90%；单日涨幅 ≥ 5% 且未涨停（或涨停封板标记）；振幅 ≥ 7%；跳空高开 ≥ 2% 且未回补；尾盘 30 分钟涨幅 ≥ 2%（需分钟/快照数据，缺数据则跳过并注明）；主力/北向资金净流入 ≥ 阈值（数据源提供时）。
3. **输出**：异动事件列表（标的/类型/触发值/阈值/时间/持续天数/是否多规则共振）+ **雷达分（0–100，规则加权）**；雷达分作为近期榜（NEAR）的重要输入，并可单独查看。
4. **降噪**：同一标的同一类型 3 个交易日内只提示一次（可配）；ST/退市/新股默认不扫；多规则共振提高排序。
5. **不伪造数据**：数据源缺分钟/资金流字段 → 对应规则标记“数据不可用”并跳过；快照过期 → 不产出雷达（`RK006` 同口径）。

**数据模型**：`recommend_runs(kind=RADAR)` + `recommend_items`（Phase 2 扩展）。
**接口**：`strategy/recommend.py::scan_abnormal(as_of, universe, params) -> RadarReport`（Phase 2 扩展）。
**验收标准**：**AC-F11-01** 每条异动可解释（命中规则 + 实际值 + 阈值）；**AC-F11-02** 阈值可配且立即生效（下一轮扫描）；**AC-F11-03** 缺数据规则跳过并标注“数据不可用”，不得伪造触发；**AC-F11-04** 同一标的同类型 3 日内去重，多规则共振正确排序。

### 8.12 F12 自选池管理（Phase 2 / M6）

**用户故事**：作为用户，我想把“自己关注的票”分组管理（远期/近期/观察），能批量导入导出、随时增删改，并让推荐和自动化默认只处理我的池子。

**规则细节**

1. **分组**：默认分组「全部 / 远期 / 近期 / 手动（可改名）」；可增删改分组（名称唯一、≤20 字）；一个标的可属多个分组。
2. **增删改**：支持 `600519`、`600519.SH`、中文名输入并**归一化**（复用 CF-10 `guess_board` / 代码规则）；去重；标注停牌/退市/ST；备注与 `tags`；支持排序。
3. **导入/导出**：CSV（`code,name?,group?,note?`）与纯文本（每行一个代码）；导入前**预览差异**（新增/已存在/非法），确认后写入；导出 CSV。
4. **与配置的关系**：`config.yaml data.universe` 作为**初始种子池**；UI 修改写入 DB（`watchlist_items`），启动时以 DB 为准合并；推荐/自动化默认只处理自选池。
5. **校验**：非 6 位代码 / 未知市场 / 已退市 → 拒绝并中文提示；批量导入逐行列出错误。

**数据模型**：`watchlist_groups`、`watchlist_items`（Phase 2 扩展，见 §5.4）。
**接口**：`data/watchlist.py::WatchlistService`（CRUD/导入导出/归一化，见 §6.8）。
**验收标准**：**AC-F12-01** 分组增删改 + 同一标的多分组正确；**AC-F12-02** 增删改与去重、停牌/退市标记正确；**AC-F12-03** CSV/文本导入预览差异、非法行逐条提示、确认后写入且可导出回读一致；**AC-F12-04** 自选池修改后推荐/自动化扫描范围同步（下次生成生效）；**AC-F12-05** 非法代码/未知市场被拒并给出中文原因。

### 8.13 F13 策略研究（ML 打分 / walk-forward / 样本外验证）（Phase 2 / M6）

**用户故事**：我想用多因子/ML/公开方法找赚钱办法；但系统必须告诉我“样本外到底行不行”，而不是只给一个漂亮的回测曲线。

**规则细节**

1. **因子库扩充**：MVP 简单因子（动量/波动率）之上，Phase 2 首批 ≥20 个因子（价值/质量/成长/动量/波动/流动性/异动），以文件化清单维护（新增因子=新增条目/文件，可测试）。
2. **ML 排序（CPU-only）**：默认使用 **scikit-learn**（BSD-3，**可选依赖，需 Lead 批准后加入 requirements**）的树模型/线性模型做截面排序；标签=未来 N 日收益分位（默认 5 日）；特征=因子截面；**必须按时间序列切分**（禁止随机打乱）。**不引入任何深度学习框架（PyTorch/TensorFlow/JAX）**；GPU 受限，深度模型仅作为将来“远程训练、本地推理”的可选扩展，不进入 M6。
3. **Walk-forward / 样本外（强制）**：`backtest/validation.py` 提供时间序列滚动切分（默认 3–5 折，训练/验证间按**交易日** purge + embargo，默认 purge=5、embargo=1）；输出每折 train/valid 指标 + 拼接 OOS 权益曲线 + OOS 汇总（年化/最大回撤/夏普/胜率/换手）。**禁止未来数据进入训练**。
4. **防过拟合最低要求**：样本内/样本外指标同屏展示；参数稳健性检查（参数 ±20% 邻域表现不崩塌）；多组参数只展示对比，**不做自动寻优**（寻优留 Phase 3）；无基准时 `RQ_WARN_NO_BENCHMARK` 且不展示超额收益。
   **达标口径（Lead 裁决 OQ-8，2026-10-25）**：**近 3 年样本外（OOS）年化 ≥10% 且最大回撤 ≤15%** 才可标记“达到研究目标（研究参考、不承诺收益）”；未达标只能标“研究阶段”并禁止接入自动化。该门槛用于验收/展示，不构成收益承诺。
5. **可复现**：`research_runs` 记录数据区间、参数、随机种子、依赖版本；同配置重跑结果一致（容差可定义）。
6. **使用边界**：研究结果默认为“研究阶段”；只有“通过样本外 + 稳健性检查”的策略/榜单可标记“可用（仅模拟盘）”；接入自动化仍须用户二次确认（推荐默认不自动下单）。
7. **降级**：未安装 scikit-learn → ML 排序提示“可选依赖未安装”，推荐/回测/多因子仍可用；数据不足或检测到未来函数 → 拒绝生成结果并中文说明原因。

**数据模型**：`research_runs`（Phase 2 扩展，见 §5.4）。
**接口**：`strategy/ml_ranker.py::MLRanker`、`backtest/validation.py::walk_forward(...)`（Phase 2 扩展，见 §6.8）。
**验收标准**

- **AC-F13-01**：walk-forward 默认 ≥3 折，输出每折 + 拼接 OOS 指标与 OOS 权益曲线；样本内/外同屏展示。
- **AC-F13-02**：**回测必须样本外**——未通过样本外的策略/榜单被标记“研究阶段/不建议实盘”，且无法一键接入自动交易（G8）。
- **AC-F13-03**：未来函数检查——用“偷看下一根 bar”的策略做反向用例，必须被检出并拒绝出结果。
- **AC-F13-04**：参数稳健性——参数 ±20% 邻域内表现不崩塌（给出红/黄/绿风险灯）；不通过时标注“稳健性不足”。
- **AC-F13-05**：可复现——同数据/同参数/同种子重跑，OOS 指标一致（容差 1e-6）；`research_runs` 可读回。

### 8.14 F14 全市场筛选（Phase 2 / M6，2026-10-26 新增，M6 当前 P0）

**用户故事**：作为用户，我希望系统每天对**全 A 股（约 5000 只）**做一次筛选，按“流动性 → 量价信号 → 过热剔除”逐层收敛到 Top N 候选；我要看到**全市场总数、过滤后数量、实际扫描数、数据时间**，并且明确知道这只是**研究阶段、不构成买入建议**。

**规则细节**

1. **分层筛选漏斗（固定顺序，可配阈值）**：
   1. **基础剔除**：ST/*ST、退市整理、上市 < `min_listed_days`、停牌、数据不足（<250 交易日）；
   2. **流动性过滤**：近 20 日日均成交额 ≥ 阈值（默认 5000 万元）、换手率 ≥ 阈值（默认 1%）、价格 ≥ 1 元；
   3. **量价信号打分**：截面标准化后合成得分（动量、量能、突破、换手、波动结构等因子，因子清单可配）；
   4. **过热剔除**：剔除连续涨停/短期涨幅过高（默认近 5 日涨幅 ≥ 30%）、振幅异常（≥ 15%）、量比异常（≥ 5）等追高风险标的；
   5. **Top N 输出**：按得分排序取 Top N（默认 50，可配）。
2. **`as_of` 截断（防未来数据，强制）**：所有计算只使用 `trade_date ≤ as_of` 的数据；数据源/缓存层必须按 `as_of` 截断，**禁止使用 as_of 之后的行**；输出必须记录 `as_of` 与数据截止时间。
3. **可复现（强制）**：同输入（as_of/因子参数/阈值/股票池/数据版本）→ 同输出（排序与得分一致，容差 1e-9）；每次扫描写 `recommend_runs(kind=SCREEN)` + `recommend_items`（rank/score/因子拆解/理由）。
4. **展示字段（页面与导出 CSV 均含）**：`as_of`、**全市场总数**、**过滤后数量**、**实际扫描数**、**数据时间**、Top N 明细（代码/名称/板块/得分/关键词因子值/风险标记）；顶部固定“**研究阶段 · 不构成买入建议 · 不承诺收益**”与 UBA 声明。
5. **性能目标**：**扫描 200–300 只 ≤ 5 分钟**（分层漏斗先做低成本过滤，再对候选集做重计算；全市场 5000 只允许分批/缓存，不要求单次全量）。
6. **导出**：结果可导出 CSV（含 as_of、过滤统计、因子值、得分），文件名带时间戳。
7. **使用边界**：筛选结果只进入“自选池/手动交易页（需用户二次确认）”；**不直接下单、不自动下单**；未通过 F15 有效性验证前，全市场筛选结果只标“研究阶段”。

**数据模型**：复用 Phase 2 表——`recommend_runs(kind=SCREEN)`、`recommend_items`（不新增表）。
**接口**：`strategy/screener.py::ScreenService.run(as_of, universe, params) -> ScreenResult`（见 §6.8）。
**异常与降级**：数据源不可用 → 用本地缓存并标注数据时间，过期则拒绝出结果（`RK006` 同口径）；某因子缺失 → 该因子权重置 0 并标注；扫描时间超限 → 分批返回并说明“已扫描 X/Y”。

**验收标准**

- **AC-F14-01**：**可复现**——同 `as_of`/参数/数据版本连续运行 2 次，Top N 排序与得分完全一致（容差 1e-9），`recommend_runs` 可追溯。
- **AC-F14-02**：**能导出 CSV**——导出文件包含 as_of、全市场总数、过滤后数量、实际扫描数、数据时间、Top N 明细与因子值；重新读取校验一致。
- **AC-F14-03**：**性能**——200–300 只扫描 **≤5 分钟**（本机基准），页面显示扫描进度与耗时。
- **AC-F14-04**：**不能出现未来数据**——用 `as_of=T` 扫描，任何因子/过滤均不得读取 `T+1` 及以后数据；QA 用“注入未来行 + 截断校验”反向用例验证（见 M6-17）。
- **AC-F14-05**：页面/导出**必须显示**“全市场总数/过滤后/扫描数/数据时间”与“研究阶段 · 不构成买入建议 · 不承诺收益”声明；无“一键自动买入”。

### 8.15 F15 筛选器有效性验证（Phase 2 / M6，2026-10-26 新增，M6 当前 P0）

**用户故事**：作为用户，我不希望看到一个“看起来能选牛股”的筛选器就盲目相信；系统必须用**分组回测 + RankIC** 告诉我它到底有没有效。

**规则细节**

1. **分组回测**：按筛选得分把股票池分为 **5 组（Q1–Q5）**，统计每组**未来 5/10/20 日**收益、相对基准（默认沪深 300）**超额**、分组单调性（Q5−Q1 价差）。
2. **RankIC**：每个交易日计算得分与未来 N 日收益的截面 Spearman 相关，输出均值 **RankIC**、ICIR、t 统计量；分 5/10/20 日三档。
3. **换手成本**：统计 Top 组（Q5）的换手率与**扣费后**收益（佣金/印花税/过户费/滑点按 SPEC §4.5 口径）。
4. **样本外（强制）**：必须用 walk-forward / 时间序列切分（M6-08）验证；样本内结果只能参考，不能作为有效性证据。
5. **判定线（未通过不得在 UI 宣称有效）**：
   - **通过**：样本外 **RankIC > 0.03** 且 **Top 组 10 日超额 > 0（扣费后）**，且分组单调性方向正确 → 可标“验证通过（研究参考）”。
   - **未通过**：任一不满足 → UI 必须标“**研究阶段 / 无效**”，禁止出现“有效/推荐/可跟单”等表述，禁止接入自动化。
6. **结果落库**：`research_runs(kind=SCREEN_VALIDATION)` 记录参数、分组统计、RankIC/ICIR、换手成本、样本外区间、判定结论（`usable`）；可在页面查看历史。
7. **禁止宣称**：即使“通过”，页面/报告仍须标注“研究参考、不构成买入建议、不承诺收益”；不得用“胜率/收益率”做营销式表述。

**数据模型**：`research_runs(kind=SCREEN_VALIDATION)`（复用，不新增表）。
**接口**：`strategy/screener.py::ScreenService.validate(params) -> ScreenValidationResult`（见 §6.8）。
**验收标准**

- **AC-F15-01**：分组回测输出 5 组 × 5/10/20 日收益、超额、单调性；样本外 RankIC/ICIR/t 值齐全。
- **AC-F15-02**：换手与扣费后收益正确（与 `FeeModel` 口径一致）；Q5 换手率与费用明细可查。
- **AC-F15-03**：**未通过验证的筛选器不得宣称有效**——UI 显示“研究阶段/无效”，无“有效/推荐”字样；自动化/推荐不得消费其结果。
- **AC-F15-04**：判定线可配（默认 RankIC>0.03 且 Top10 日超额>0）；修改后在下次验证生效并留审计。
- **AC-F15-05**：可复现——同参数/同数据/同 as_of 两次验证结果一致；`research_runs` 可读回。

---

## 9. 风控规格（详细）

### 9.1 默认参数

| 参数 | 默认值 | 含义 | 调整建议 |
|---|---|---|---|
| `max_position_per_symbol_pct` | 20% | 单只股票市值占总资产上限 | 保守 10%、激进 30% |
| `max_total_position_pct` | 80% | 全部持仓市值占总资产上限（至少留 20% 现金） | 保守 60% |
| `max_daily_loss_pct` | 3% | 单日亏损熔断线（相对当日开盘总资产） | 保守 2% |
| `max_price_deviation_pct` | 2% | 限价与最新价最大偏离（近似价格笼子） | 通常不改 |
| `max_single_order_value_pct` | 10% | 单笔委托金额占总资产上限（防手误）；**仅约束买入，卖出豁免** | 大资金可降至 5% |
| `max_orders_per_minute` | 10 | 每分钟最多提交订单数（防循环下单） | 自动化高频可调高 |
| `min_listed_days` | 60 | 上市不足 N 个交易日不交易 | 不建议改小 |
| `min_order_amount` | 1000 元 | 单笔最小金额（防最低佣金侵蚀），低于拒绝 `RK012`；**仅约束买入**；配置键 `trading.min_order_amount` | 小资金可降至 500 |
| `blacklist` | `[]` | 用户自定义黑名单（系统黑名单另行自动合并） | 自选 |
| `allow_auto_orders` | false | 自动交易授权：AUTO 订单默认 `RK020` fail-closed；显式设 true 或 `automation.enabled=true` 才放行 | 仅开启自动化时设为 true |
| `enabled` | true | 风控总开关 | **不建议关闭**（关闭时审计告警） |

> **参数来源（Lead/Dev3 口径 2026-10-25）**：运行时以 `config.yaml` 的 `risk` 段为基准（`min_order_amount` 来自 `trading.min_order_amount`）；UI 修改后写入 `risk_config` 表并作为**覆盖值**叠加（写审计），读取顺序为 `risk_config` 覆盖值 → `config.yaml` → 安全默认值。`RiskGate` 本身不读文件/DB（纯函数），参数由调用方冻结后传入；`core.models.RiskConfig` 仅作传输 DTO。

### 9.2 拒绝原因码（冻结）

| 码 | 常量名 | 中文含义 | 建议动作 |
|---|---|---|---|
| RK001 | SYMBOL_NOT_IN_UNIVERSE | 标的不在可交易池 | 先加入自选池 |
| RK002 | SYMBOL_BLACKLISTED | 标的在黑名单（ST/退市整理/用户禁用） | 更换标的 |
| RK003 | SYMBOL_SUSPENDED | 标的停牌 | 复牌后再操作 |
| RK004 | LISTED_TOO_SHORT | 上市不足 60 个交易日 | 更换标的 |
| RK005 | MARKET_CLOSED | 非交易时段/非交易日 | 交易时段再下单 |
| RK006 | DATA_STALE | 行情数据过期 | 刷新行情/检查数据源 |
| RK007 | SINGLE_POSITION_LIMIT | 单票仓位超限 | 减少数量或调高上限 |
| RK008 | TOTAL_POSITION_LIMIT | 总仓位超限 | 减仓或调高上限 |
| RK009 | DAILY_LOSS_LIMIT | 单日亏损熔断 | 今日停止买入，明日自动恢复 |
| RK010 | PRICE_DEVIATION | 价格偏离过大 | 检查价格（接近涨跌停/手误） |
| RK011 | PRICE_OUT_OF_LIMIT | 价格超出涨跌停区间 | 改到有效区间 |
| RK012 | ORDER_VALUE_LIMIT | 单笔金额超限（**仅买入**：超过单笔上限或低于最小下单金额；卖出不适用） | 拆单/调整数量或调高上限 |
| RK013 | ORDER_RATE_LIMIT | 下单频率超限 | 稍后再试 |
| RK014 | INSUFFICIENT_CASH | 可用资金不足 | 减少数量/卖出腾资金 |
| RK015 | INSUFFICIENT_POSITION | 持仓不足 | 检查持仓数量 |
| RK016 | T1_UNAVAILABLE | T+1 可用数量不足（今日买入不可卖） | 明日再卖 |
| RK017 | LOT_SIZE_INVALID | 数量不符合整手规则 | 按 100 股整数倍 |
| RK018 | PARAM_INVALID | 参数非法（含风控内部异常 fail-closed） | 检查输入 |
| RK019 | DUPLICATE_ORDER | 重复订单（幂等键冲突） | 查今日委托 |
| RK020 | ACCOUNT_DISABLED | 账户停用/自动化关闭时提交自动单 | 开启账户或自动化 |

**券商/适配层错误码**：`BR001` 券商不可用/网络异常、`BR002` 券商拒单、`BR003` 标的不支持、`BR004` 适配器未实现、`BR005` 请求超时。
**数据质量码**：`DQ001` 缺失 K 线、`DQ002` 重复 K 线、`DQ003` 价格异常跳变、`DQ004` 零成交量、`DQ005` 复权因子缺失、`DQ006` 数据过期、`DQ007` 停牌未标记。
**回测警告码**：`RQ_WARN_NO_ADJ` 未复权、`RQ_WARN_ST_APPROX` ST 近似、`RQ_WARN_SHORT_SAMPLE` 样本过短（<250 交易日）、`RQ_WARN_FEW_TRADES` 交易过少（<30 笔）、`RQ_WARN_HIGH_SUSPEND` 停牌占比高、`RQ_WARN_NO_BENCHMARK` 无基准数据（Dev2 已实现，Lead 已批准 2026-10-25；Phase 2 推荐/研究榜同样沿用）、**`RQ_WARN_MOCK_DATA` 离线 Mock 数据（仅供演示，非真实行情）**、**`RQ_WARN_PRICE_JUMP` 相邻交易日异常跳变（可能数据错误，需核对除权事件/数据源）**、**`RQ_WARN_POSITION_MIN_LOT` 目标仓位不足 1 手（已按最小 1 手调整）**。

> 上述 `RQ_WARN_*` 一律为**“警告非阻断”**：记录到回测结果/运行日志/UI 警告区，不阻止回测或下单；若涉及数据可信度，UI 必须显著提示（如 Mock 数据条、跳变告警）。`RQ_WARN_NO_BENCHMARK` 时基准相关指标为 `None`，不计算超额收益，UI/报告必须展示“无基准，超额收益不可用”。

### 9.3 熔断生命周期

```
每日 08:50（盘前）: baseline_asset = 昨收总资产；fused = false
盘中每次下单前:     if (current_asset - baseline)/baseline <= -3%: fused = true
fused == true:     禁止买入（RK009），允许卖出；UI 红条 + 通知
次日 08:50:        baseline 更新，fused 重置为 false
手动解锁:           设置页 → 二次确认 → 写审计（actor=manual）→ fused=false（当日仍记录）
```

---

## 10. UI 页面清单（12 页 = MVP 9 页 + Phase 2 3 页，控件 / 交互 / 字段）

> **10.0 页面注册约定（决策 D-9）**：`app.py` 用**动态发现**方式注册页面——扫描 `ui/pages/*.py`（忽略 `_common.py`、`__init__.py`），每个模块必须暴露 `PAGE_TITLE: str` 与 `render() -> None`，按文件名排序生成侧边栏导航。**新增/重写页面只改自己的页面文件，不改 `app.py`**；尚未实现的页面由 `app.py` 自动显示“建设中”占位。MVP 9 页可并行开发；Phase 2（M6）新增 `watchlist.py`、`recommend.py` 两页，合计 **11 页**，同样无需改 `app.py`。

> 全局元素：侧边栏（导航、账户选择、模式标识“模拟盘/实盘未接入”、数据源状态灯、调度器开关状态、离线黄条）；顶部/底部统一错误卡片（中文 + 原因码 + trace_id）。

### 10.1 总览 `ui/pages/overview.py`

- **控件**：刷新按钮（清缓存重载）；日期范围选择（默认近 30 日）；账户切换。
- **展示**：
  - 状态条 4 项：数据源（OK/降级/离线 + 最后更新时间）、账户（总资产）、调度器（运行中/已停止 + 下次运行时间）、风控（正常/熔断中）。
  - KPI 卡片 6 个：总资产、当日盈亏（金额/百分比）、累计收益率、可用资金、持仓市值、总仓位%。
  - 权益曲线（plotly：line + 回撤填充，可切换近 1 月/3 月/1 年/全部）。
  - 持仓 Top 5（按市值）+ 资产配置饼图（现金/持仓）。
  - 今日信号与订单表（时间、策略/手动、代码、方向、数量、状态、原因码）。
  - 最近 5 条告警/通知。
- **交互**：点击持仓行跳转持仓页；点击订单跳日志页并按 trace_id 过滤。
- **空状态**：无数据 → “去行情页拉取数据”；无账户 → “创建模拟账户（默认 10 万）”。

### 10.2 行情 `ui/pages/market.py`

- **控件**：标的搜索（代码/名称模糊）；自选池下拉；区间（1 月/3 月/1 年/自定义）；复权方式（前复权/后复权/不复权，默认前复权）；指标副图选择（无/RSI/MACD/成交量）；“刷新数据”按钮；“加入自选”/“移出自选”。
- **展示**：K 线（candlestick）+ MA5/10/20/60 + 成交量；副图指标；最新价/涨跌幅/成交额；数据质量标记（停牌灰标、缺失红点、ST 标签）；最近 5 条相关信号。
- **交互**：点击“去回测”带入该标的到回测页；“去下单”带入手动交易页。
- **异常**：数据缺失 → 提示“该区间无数据，可点击刷新拉取”；离线 → 黄条。

### 10.3 手动交易 `ui/pages/manual_trade.py`

- **控件**：账户选择；标的搜索；方向（买/卖）；价格类型（限价默认；市价需配置允许）；价格输入（默认最新价，展示上涨/下跌按钮，标出跌停价-涨停价区间）；数量（步进 100，按钮 100/500/1000/全仓/半仓/1/4 仓，输入框）；“风控预检”自动刷新；二次确认 checkbox；“提交委托”按钮。
- **展示（风控预检面板）**：预计成交金额、费用明细（佣金/印花税/过户费/规费/合计）、成交后仓位占比（单票/总仓位）、风控结论（通过/拒绝 + RK 码 + 中文 + 建议）、当前可买/可卖数量。
- **交互**：提交 → loading → toast（成功/失败）；今日委托表（时间、代码、方向、价格、数量、状态、原因码、操作=撤单）。
- **异常**：全部以中文错误卡片展示；现金不足显示差多少；T+1 显示“今日买入不可卖，可卖 0 股”。

### 10.4 策略 `ui/pages/strategies.py`

> 页面文件命名约定：9 个页面文件由 `app.py` **动态发现**（`ui/pages/*.py`，忽略 `_underscore` 前缀与 `__init__.py`），导航显示名以各页 `PAGE_TITLE` 为准、并按 `PAGE_TITLE` 去重（决策 D-9）。本页实现文件名以 `PAGE_TITLE="策略"` 的 `strategies.py` 为准（早期草稿的 `strategy.py` 不再使用）。

- **控件**：策略实例列表（表格：名称、策略、参数摘要、标的池、模式、启停、最近信号时间）；“新建实例”（选策略 → 按 `params_schema` 动态生成表单 → 校验 → 保存）；“编辑/复制/删除”；模式选择（关闭/模拟盘自动/仅信号）；“预览信号”按钮（跑最近 60 交易日，只展示不下单）；标的池多选（默认取自选池）。
- **展示**：参数说明（中文 help）；参数校验错误；信号预览表（日期、代码、方向、强度、原因）；该实例最近 20 条信号与对应订单/拦截。
- **交互**：启停开关（写审计）；删除需二次确认；重复名称拒绝。
- **异常**：参数非法 → 字段级中文错误；策略不存在 → 列表自动刷新。

### 10.5 回测 `ui/pages/backtest.py`

- **控件**：策略下拉（含参数表单）；标的多选（默认自选池）；回测区间（默认近 3 年）；初始资金；费率设置（展开，默认读 config）；滑点 bps；基准指数；【运行回测】；【导出 HTML 报告】；历史回测列表（下拉/表格，支持选择 2 个对比）。
- **展示**：运行状态/进度；KPI 卡片（总收益、年化、最大回撤、夏普、胜率、盈亏比、交易次数、波动率）；权益曲线 vs 基准；回撤曲线；月度收益热力图；交易明细表（可排序/下载 CSV）；警告区（RQ_WARN_*）；对比视图（指标对比表 + 双曲线叠加）。
- **交互**：运行中禁用表单；完成后自动刷新历史列表；对比模式切换。
- **异常**：区间无数据/资金不足 → 中文提示 + 建议；运行失败 → 展示 `error_msg` 与“查看日志”。

### 10.6 持仓 `ui/pages/positions.py`

- **控件**：账户选择；刷新；【导出持仓 CSV】；行内“卖出”（跳手动交易页带入）；“一键清仓”（二次确认，逐单走风控）；“账户重置”（二次确认 + 输入确认词）。
- **展示**：账户摘要（总资产、现金、持仓市值、当日盈亏、总仓位）；持仓表（代码、名称、数量、可用、T+1 冻结、成本价、现价、市值、浮动盈亏、盈亏%、仓位占比、是否停牌/ST）；资金曲线；资产配置饼图；最近 20 笔成交表。
- **异常**：空持仓 → 引导去手动交易；对账不一致 → 顶部红条提示并给出“查看日志/修复建议”。

### 10.7 自动化 `ui/pages/automation.py`

- **控件**：总开关（启动/停止调度器，二次确认）；3 个任务卡片（盘前/盘中/盘后）：时间配置、下次运行、上次结果、【立即运行】、【查看记录】；策略实例启用清单（勾选）；风控状态卡（参数摘要 + 今日熔断状态）；【测试通知】按钮。
- **展示**：今日管线统计（信号数、风控拦截数、下单数、成交数）；最近 10 次 `job_runs`（时间、任务、状态、耗时、消息）；告警区；调度器自带状态。
- **异常**：启动失败（依赖缺失/数据源 DOWN）→ 中文原因 + 修复建议；停止时不取消已提交订单（提示文案）。

### 10.8 设置 `ui/pages/settings.py`

- **Tabs**：
  1. **数据源**：provider 选择、自选池编辑、历史年限、刷新间隔、连通性测试按钮、最后更新时间。
  2. **风控**：9.1 参数表（带单位/范围/中文说明）、黑名单编辑、保存（二次确认 + 审计）、恢复默认。
  3. **通知**：全局开关、桌面/邮件/Webhook 分渠道开关与参数、发送测试、最近通知记录。
  4. **存储与备份**：【立即备份】、备份列表与恢复（二次确认）、清理回测记录、数据库大小。
  5. **环境**：Python 版本、依赖检查（缺失列表 + 安装提示）、TA-Lib 状态、ccxt 状态、日志级别。
  6. **关于**：版本、许可说明（个人非商业使用、第三方依赖入口 THIRD_PARTY）、数据免责声明。
- **异常**：保存失败回滚旧值并提示；恢复备份前强制二次确认 + 自动先备份当前库。

### 10.9 日志 `ui/pages/logs.py`

- **控件**：日志级别（DEBUG/INFO/WARN/ERROR）；时间范围；关键词搜索；trace_id 输入（从其它页跳转带入）；【刷新】；【导出 CSV】。
- **Tabs**：① 运行日志（表格：时间、级别、模块、trace_id、消息；点击展开详情）；② 审计流水（时间、actor、动作、对象、结果、before/after）；③ 数据校验报告（DQ 问题：时间、标的、严重度、说明、建议）；④ 通知记录（渠道、事件、状态、错误）。
- **异常**：日志文件不存在 → 提示“暂无日志”并给出日志路径；数据量过大 → 默认只查最近 500 行并提供“加载更多”。

### 10.10 自选池 `ui/pages/watchlist.py`（Phase 2 / M6）

- **控件**：分组侧栏（新增/改名/删除分组）；标的搜索与添加（代码/中文名，自动归一化）；批量导入（上传 CSV / 粘贴文本，**先预览差异**再确认）；导出 CSV；条目行内编辑（备注/tags/排序/删除）；“全部/远期/近期/手动”默认分组。
- **展示**：分组内标的表（代码、名称、板块、最新价、涨跌幅、标签、备注、数据状态：停牌/ST/新股）；导入预览表（新增/已存在/非法）；池规模统计。
- **交互**：增删改立即生效（写 `watchlist_items` + 审计 + `watchlist.changed` 事件）；非法代码中文提示并保留原输入；“推荐页/行情页 → 加入自选”跳转落点为本页。
- **异常**：重复添加去重提示；未知市场/退市拒绝；导入中途失败整体回滚。

### 10.11 推荐 `ui/pages/recommend.py`（Phase 2 / M6）

- **控件**：榜单类型（远期价值型 / 近期异动型）；刷新/生成按钮（显示数据截止日）；TopN 选择；筛选（板块/是否 ST/评分下限）；“加入自选池”“去回测”“去手动交易”行内按钮；异动雷达 Tab（阈值展示、命中规则筛选）；导出 CSV。
- **展示**：
  - **顶部固定 UBA 风险声明 + “研究阶段/可用（模拟盘）”状态徽标**；
  - 远期榜/近期榜表：排名、代码、名称、板块、综合评分、**推荐理由（≥2 条，含因子/分位/贡献值）**、因子拆解、建议持有周期、**回测区间**、**样本外（年化/最大回撤/夏普/胜率）**、风险提示、建议仓位上限；
  - 异动雷达：事件列表（标的/类型/触发值/阈值/时间/去重状态/雷达分）；
  - 数据缺口与警告区（缺因子、缺基准 → `RQ_WARN_NO_BENCHMARK`、样本外未完成）。
- **交互**：切换榜单不触发下单；加入自选池只写自选池；点击“去回测/去手动交易”带参跳转；无“一键自动买入”入口。
- **异常**：数据不足/未生成 → 中文引导“先生成数据与榜单”；样本外缺失 → 榜单显示“研究阶段”且禁用自动接入；导出报告强制包含回测区间+样本外+UBA。

### 10.12 全市场筛选 `ui/pages/screener.py`（F14/F15，Phase 2 / M6，2026-10-26 新增）

- **控件**：`as_of` 日期（默认最新交易日）；扫描范围（全市场/自选池/指数成分）；阈值参数（流动性/过热，读取 `screen.*`）；【开始扫描】（显示进度与耗时）；【导出 CSV】；【运行有效性验证】；历史扫描/验证记录选择。
- **展示（F14）**：顶部状态条——**全市场总数 / 过滤后数量 / 实际扫描数 / 数据时间 / 耗时**；Top N 表（排名、代码、名称、板块、得分、关键词因子值、风险标记）；固定声明“**研究阶段 · 不构成买入建议 · 不承诺收益**” + UBA；警告区（数据缺口/过期/分批扫描进度）。
- **展示（F15）**：分组回测表（Q1–Q5 × 未来 5/10/20 日收益与超额、单调性）、RankIC/ICIR/t 值、Top 组换手率与扣费后收益；判定徽标：**验证通过（研究参考）/ 研究阶段·无效**（按 `screen.validate_*` 默认线：RankIC>0.03 且 Top10 日超额>0 扣费后）。
- **交互**：扫描结果可“加入自选池 / 去手动交易（需二次确认）”；**无“一键买入/自动跟单”**；未通过验证时禁用“宣称有效”类文案与自动化入口。
- **异常**：数据源不可用 → 用缓存并标注数据时间；过期 → 拒绝出结果；超时 → 分批返回并提示“已扫描 X/Y”；未装依赖/数据不足 → 中文降级说明。

---

## 11. 非功能需求

### 11.1 性能与容量

| 项 | 指标 |
|---|---|
| 冷启动（首次建库/建表） | ≤ 15 秒 |
| 热启动（DB 已存在） | ≤ 5 秒 |
| 页面切换/交互 | ≤ 2 秒 |
| 回测（20 标的 × 5 年，日频） | ≤ 15 秒 |
| 数据增量更新（10 标的 × 1 天） | ≤ 5 秒 |
| 查询（10 万行级表，带索引） | ≤ 200 毫秒 |
| 数据容量 | 300 标的 × 10 年日线（约 75 万行）后仍满足上述指标；DB < 1 GB |
| 内存占用 | 常规 < 1 GB |

### 11.2 离线可用（Mock 数据）

1. `data.provider: mock` 或 akshare 不可用时，系统**必须**能完成：行情展示、回测、手动下单、自动化 job 手动触发、日志查看——即全部 F1–F9 的演示路径。
2. Mock 数据为确定性生成（固定随机种子 + 内置 CSV 快照），保证测试可复现；“离线演示模式”在 UI 显著标识。
3. 依赖缺失（akshare/TA-Lib/ccxt/APScheduler）时给出中文安装提示，不得静默崩溃。

### 11.3 日志与审计

1. 日志：JSON Lines，按日轮转，保留 30 天；级别可配；敏感信息（密码/token）过滤。
2. 审计：所有资金/订单/风控参数/自动化开关变更写 `audit_logs`，不可修改删除；保留 1 年（本地磁盘）。
3. 追踪：每次手动操作与 job 生成 `trace_id`，贯穿信号/订单/成交/通知/日志。

### 11.4 数据备份与恢复

1. 每日自动备份（启动时检查：距上次备份 > 24 小时即备份），SQLite 在线备份 API；路径 `runtime/backups/quantdesk-YYYYMMDD-HHMMSS.db`，保留最近 7 份，超期自动清理。
2. 手动备份/恢复入口在设置页；恢复前自动备份当前库并二次确认；恢复后重启会话。
3. 备份失败不阻塞主流程，但 Write 通知/日志并在总览页提示。

### 11.5 异常恢复

1. 启动自检：DB 可读写、表结构完整、依赖可用、数据源连通性、账户对账（持仓/资金/订单一致性）。
2. 订单状态自愈：启动时 `SUBMITTED/PARTIALLY_FILLED` 且无对应成交的订单置 `EXPIRED` 并通知；Paper 账户按 `trades` 重算持仓与现金（以流水为事实来源）。
3. 调度恢复：APScheduler 不持久化 job 到 DB 时，启动按配置重建；错过的执行窗口按 `misfire_grace_time` 处理，不补跑历史交易日。
4. 数据库损坏：启动检测失败 → 提示从最近备份恢复（设置页入口），不自动覆盖原文件。

### 11.6 安全与隐私

1. 单用户本地运行，不监听公网（Streamlit 默认 localhost）；不收集/上传任何数据。
2. 密钥只从环境变量读取，不落库、不入日志、UI 掩码。
3. 实盘相关：MVP 不接真实资金；`QmtBroker` 为骨架；开启实盘前必须用户显式确认（未来版本）。

### 11.7 可维护性与可扩展性

1. 新增策略 = 1 个文件 + 装饰器（不改其它文件）。
2. 新增数据源 = 1 个 Provider 类 + 注册（不改缓存/回测）。
3. 新增 broker = 1 个 Adapter 类 + 注册（不改订单/风控）。
4. 所有费率/参数/阈值配置化，代码中无魔法数字（除 A 股法定规则常量外，需集中定义并注释来源）。
5. 中文注释与中文错误消息；关键函数有 docstring。

### 11.8 合规与免责（用户可见）

1. **定位声明**：本系统只提供**研究工具与交易信号**，**不承诺收益、不代客理财、不构成投资建议**；用户是唯一决策者与风险承担者（**UBA：User Bears All / 用户自担风险**）。
2. **推荐与研究报告的强制展示要素**（缺一不可）：①策略/榜单的**回测区间**；②**样本外（walk-forward）表现**；③**风险提示**（最大回撤、样本期、交易次数、过拟合风险灯）；④**UBA 声明**；⑤数据截止日与数据源。未通过样本外的结果必须标注“研究阶段/不建议实盘”。
3. **UI 固定声明位置**：推荐页、回测页、报告导出、手动交易页、自动化页、关于页均展示统一文案：“**本系统仅供个人研究学习，不构成投资建议；历史回测与样本外表现不代表未来收益；据此交易风险自担（UBA）**。”
4. **禁用表述**：全站禁止“稳赚 / 保证收益 / 必涨 / 无风险 / 翻倍”等承诺性用语；推荐理由只能陈述因子/规则与历史统计，不得预测涨跌。
5. **默认不自动下单**：推荐/研究结果**默认不触发下单**；只有用户显式加入自选池、配置策略并开启自动化后才可能下单，且全程受风控闸门约束（`RK` 码可追溯）。
6. **合规来源**：A 股数据来自公开接口，遵守上游条款，仅个人研究；第三方代码与许可登记在 `docs/THIRD_PARTY.md`；GPL/AGPL/非商用项目仅重写思路、不复制代码。
7. **免责留存**：UBA 声明与“研究阶段”标记写入审计/报告元数据，可追溯用户已阅（推荐页首次展示需用户确认）。
8. **全市场筛选（F14/F15）补充（2026-10-26）**：筛选页/导出结果必须固定展示“**研究阶段 · 不构成买入建议 · 不承诺收益**”与 UBA；**未通过 F15 有效性验证（样本外 RankIC>0.03 且 Top 组 10 日超额>0 扣费后）前，禁止出现“有效/推荐/可跟单”等表述**，禁止接入自动化；筛选结果只能“加入自选池/去手动交易（需二次确认）”。

---

## 12. 验收与质量门槛（Definition of Done）

一个任务“完成”需同时满足：

1. 代码可运行（`python -m pytest` 相关用例通过；`streamlit run app.py` 无报错）。
2. 有对应的 pytest 用例（新增逻辑至少 1 条正例 + 1 条反例；纯 UI 任务提供手动验收步骤）。
3. 不跨文件越界修改（按 TASKS.md 文件归属）；不硬编码配置。
4. 错误路径有中文可操作提示。
5. 更新 `docs/` 中受影响的内容（如有）。

里程碑验收门槛：

| 里程碑 | 门槛 |
|---|---|
| M0 | `python app.py` 等价启动成功；9 页占位可切换；DB 建表成功；日志/事件/配置可用；`pytest` 骨架通过 |
| M1 | 3 策略 × 自选池能回测出完整报告；T+1/涨跌停/整手/费用用例全绿；数据幂等与校验用例全绿 |
| M2 | 手动买入/卖出/撤单全通；T+1 拦截正确；风控 RK 码全覆盖；持仓与资金曲线与手算一致 |
| M3 | 调度器一键启停；3 job 可手动触发；信号→风控→订单链路 trace 完整；通知至少一种可用；风控页面可改参数并即时生效 |
| M4 | 断网 Mock 端到端冒烟通过；`start.bat` 一键启动；RUNBOOK/THIRD_PARTY 完成；P0/P1 缺陷清零 |
| M5 | 9 页视觉统一；回测报告可导出；示例数据与演示脚本可跑；用户手册完成；用户可独立完成 4 个核心任务 |
| **M6（Phase 2）** | F10–F15 全部可演示：远期/近期两套榜单**每条可解释**且展示回测区间+样本外+UBA；异动雷达阈值可配；自选池分组/导入导出可用；walk-forward 样本外强制、参数稳健性风险灯可用；**F14 全市场筛选（可复现/导出/200–300 只≤5 分钟/as_of 截断无未来数据/显示总数与数据时间）；F15 分组回测+RankIC 判定（未过线只标研究阶段/无效）**；`RQ_WARN_NO_BENCHMARK` 正确展示；推荐/筛选默认不自动下单 |

**缺陷分级**：P0（数据错误/资金算错/崩溃/风控失效）必须修；P1（核心流程不可用）必须修；P2（体验问题）排期；P3（建议）记录。

---

## 13. 风险与缓解（承接 BRIEF 第 9 节）

| 风险 | 影响 | 缓解 |
|---|---|---|
| akshare 改版/限流 | 数据断供 | 缓存优先 + Mock 兜底 + 主备 provider + 限流退避 + 一行配置切换 |
| TA-Lib 装不上 | 策略不可用 | pandas 兜底实现同名指标，接口不变；设置页显示状态 |
| A 股实盘无法接入 | 交付价值下降 | F9 适配层 + PaperBroker 完整；接口先冻结，未来只加 Adapter |
| 并行改文件冲突 | 集成失败 | 文件级归属 + 契约冻结 + 依赖排序（TASKS.md） |
| 回测好看实盘亏钱 | 用户误信 | 统一口径（同源规则）、警告项（样本短/交易少/未复权）、免责声明；MVP 不承诺盈利 |
| 规则口径实现错误（T+1/费用） | 结果失真 | SPEC 第 4 节唯一口径 + 精确数值单测（AC-F3-03/04）+ QA 独立复核 |
| 资金规模小导致最低佣金占比高 | 小单不经济 | `min_order_amount` + 预检提示费用占比 |
| 自动化失控 | 资金损失（模拟盘为数据） | 默认关闭 + 风控闸门 + 熔断 + 一键停止 + 全量审计 |
| 用户看不懂 | 弃用 | 全中文、原因码 + 建议动作、向导式空状态、用户手册、一键启动 |
| **推荐误导/过度自信（Phase 2）** | 用户把“推荐”当“保证”，实际亏损 | 可解释理由 + 样本外强制 + 研究阶段标记 + UBA 声明 + 推荐默认不自动下单（§11.8） |
| **过拟合（Phase 2）** | 样本内好看、样本外失效 | walk-forward + purge/embargo + 参数稳健性 + 禁止自动寻优（AC-F13-01/04） |
| **ML 依赖/算力（Phase 2）** | sklearn 安装失败、训练慢 | CPU-only、可选依赖、缺失降级为多因子；不引入深度学习框架、不要求 GPU |
| **全市场数据成本（F14）** | 5000 只拉取/扫描超时、限流 | 分层漏斗先做低成本过滤；缓存 + 分批扫描；性能目标只对 200–300 只设 ≤5 分钟；不足则明确标注已扫描 X/Y |
| **未来函数（F14/F15）** | 用了 as_of 之后的数据，结果虚高 | 强制 `as_of` 截断 + QA 注入未来行反向用例；F15 必须 walk-forward 样本外；未过验证只标“研究阶段/无效” |

---

## 14. 开放问题（需用户拍板，OQ-1…OQ-8）

> 以下问题必须在 Lead 与用户确认后写入 `config.yaml` 默认值或后续里程碑；未确认前按本文默认值执行。

| 编号 | 问题 | 选项与影响 | PM 建议 |
|---|---|---|---|
| **OQ-1** | **是否接入真实券商，以及走哪条路线？** 若未来要接，优先 QMT/miniQMT（迅投）还是其他（Ptrade/同花顺/华泰 MATIC）？ | A. 永不接，只做模拟盘（系统最稳、无风险） B. 后续接 QMT（需券商开通 + 客户端常驻 + Windows 环境，适配层按此预留字段） C. 后续接加密（ccxt testnet→实盘，另一套规则） | 建议 **B**：A 股用户的唯一现实路径是 QMT；MVP 只留骨架，先不影响进度 |
| **OQ-2** | **MVP 市场范围**：是否严格锁定沪深（主板/创业板/科创板）？北交所（8xxxxx/920xxx，±30%）与加密 testnet（CcxtBroker）是否纳入第一版范围？ | A. 仅沪深（数据、费用、涨跌停口径最简单） B. 沪深 + 北交所（+1 套规则与数据源） C. 再加加密 testnet（+1 套规则体系） | 建议 **A**：北交所/加密都只留扩展位，不进 MVP |
| **OQ-3** | **通知渠道偏好**：桌面通知 / 邮件 / 企业微信 / 飞书 / 通用 Webhook，哪个是主渠道？是否愿意提供 SMTP 或 Webhook 配置？ | 桌面：零配置但只能本机；邮件：需要 SMTP 授权码；企业微信/飞书：需要群机器人 webhook（最常用） | 建议默认**桌面通知**（零配置可用）+ 预留 Webhook；用户如常用微信生态再加企业微信 |
| **OQ-4** | **资金规模与风控偏好**：模拟账户初始资金默认多少（建议 10 万）？单票 20% / 总仓位 80% / 单日亏损 3% 熔断是否符合你的风险偏好？ | 10 万：适合演示与佣金测算；50 万：更接近真实；风控过松有穿仓感、过紧则经常被拒 | 建议 **10 万 + 上述默认值**，UI 可改；若真实资金 < 5 万，需下调 `min_order_amount`（最低 5 元佣金会显著侵蚀收益） |
| **OQ-5** | **数据频率与标的范围**：只做日频（每天盘后跑一次、盘中 10:00 扫一次快照）够吗？自选池规模多大（建议先 20–50 只）？是否接受首次拉取耗时（5000+ 全市场不现实）？ | 日频：够用且稳定；分钟级：akshare 限流严重、需另选数据源、调度复杂 | 建议 **日频 + 自选池 20–50 只**；分钟级列入后续版本 |
| **OQ-6（Phase 2）** | **推荐是否允许接入自动下单？** | A. 永不允许（推荐只做研究，下单必须手动/策略显式配置） B. 允许但需二次确认+审计 C. 允许一键自动 | **✅ 已拍板（2026-10-25）：选 A**。推荐只能“一键带入手动交易页/需用户二次确认”；**自动化任务不得消费 recommend 输出直接下单** |
| **OQ-7（Phase 2）** | **ML 依赖是否允许引入 scikit-learn（CPU，BSD-3）？** | A. 允许（可选依赖，缺失降级） B. 不允许，仅用纯 pandas 统计 C. 将来允许远程 GPU 训练（不进 M6） | **✅ 已拍板（2026-10-25）：选 A**。sklearn 为可选依赖（缺失时 ML 策略不可用、文档标注、UI 提示安装）；**禁止引入 torch/tensorflow** |
| **OQ-8（Phase 2）** | **“年化 10–20% 价值型”目标的评估口径**：用多长样本外区间/多少折来判定“达标”？是否需要设定“不达标就下榜”的硬门槛？ | A. 近 3 年 OOS 年化≥10% 且回撤≤15%（建议） B. 仅作参考展示不设门槛 C. 更严格（5 年 OOS + 稳健性） | **✅ 已拍板（2026-10-25）：选 A**。近 3 年 OOS 年化 ≥10% 且最大回撤 ≤15% 才算“达到研究目标”，必须标注“研究参考、不承诺收益”；未达标只能标“研究阶段”并禁止接自动化 |

---

## 附录 A：BRIEF → SPEC 映射（可追溯）

| BRIEF 条目 | SPEC 章节 | 里程碑 |
|---|---|---|
| F1 数据层 | 8.1、6.1、5.2A | M1 |
| F2 策略层 | 8.2、6.2 | M1 |
| F3 回测引擎 | 8.3、6.4、4.x | M1 |
| F4 模拟交易 | 8.4、6.3、5.2C | M2 |
| F5 自动化 | 8.5、6.5 | M3 |
| F6 风控闸门 | 8.6、9 | M2/M3 |
| F7 Web UI | 8.7、10 | M0/M2/M3/M4 |
| F8 通知与日志 | 8.8、11.3、11.4 | M3 |
| F9 实盘适配层 | 8.9、6.3 | M4 |
| **F10 选股与推荐（Phase 2）** | **8.10、6.8、5.4** | **M6** |
| **F11 异动雷达（Phase 2）** | **8.11、6.8、5.4** | **M6** |
| **F12 自选池管理（Phase 2）** | **8.12、6.8、5.4** | **M6** |
| **F13 策略研究（ML/walk-forward/样本外，Phase 2）** | **8.13、6.8、5.4** | **M6** |
| **F14 全市场筛选（Phase 2，2026-10-26 新增）** | **8.14、6.8、5.4（复用表）** | **M6（P0）** |
| **F15 筛选器有效性验证（Phase 2，2026-10-26 新增）** | **8.15、6.8、5.4（复用表）** | **M6（P0）** |
| 合规口径 | 11.8、THIRD_PARTY | 全程（M6 增加推荐/UBA 强制展示） |
| 技术栈 | 第 0 节（不改变；Phase 2 仅可加 CPU 可选依赖 sklearn） | 全程 |
| 里程碑 M0–M5 | 第 12 节 + TASKS.md | 全程 |
| 里程碑 M6（Phase 2） | 第 12 节 + ROADMAP.md M6 + TASKS.md M6 | M6 |

## 附录 B：设计决策记录（D-1 … D-14）

| 编号 | 决策 | 理由 | 备选与影响 |
|---|---|---|---|
| D-1 | 运行期数据统一放 `runtime/`（S-1） | `quantdesk/data/` 是 Python 包，不能放 DB | 备选：放项目根目录，但会污染目录 |
| D-2 | `ui/pages/_common.py` 作为页面共享工具（S-2） | 避免 9 个页面重复写格式化/渲染逻辑 | 也可各页自写，但重复且易不一致 |
| D-3 | A 股撮合规则与费用模型**只在** `backtest/broker_sim.py` 实现一次，`trading/broker.py` 的 PaperBroker 复用 | 落实“回测=实盘同源”，避免两套口径；BRIEF 文件清单无独立 rules 文件 | 备选：放 `core/`，但会把业务规则塞进基础层；当前方案仅在 trading → backtest 单向依赖，允许 |
| D-4 | 回测成交价用 T+1 开盘价；模拟盘用当日快照价 | 日频回测无法精确建模盘中成交，开盘价是集合竞价的可解释近似 | 用收盘价会引入未来函数；用 VWAP 需分钟数据（不做） |
| D-5 | 涨停价买入按“不成交”保守处理 | 避免“排板必成”的乐观偏差，宁可低估收益 | 乐观口径会系统性高估回测收益 |
| D-6 | 风控 fail-closed（内部异常即拒单） | 交易系统安全优先 | 放行会导致失控风险 |
| D-7 | 自动化默认关闭 | 用户装好即自动交易风险过高 | 默认开启体验更“爽”但危险 |
| D-8 | 拒绝原因码用 RK/BR/DQ/RQ 前缀 + 数字，冻结 | UI、日志、测试、文档统一引用，便于检索与自动化断言 | 纯中文消息无法稳定断言 |
| D-9 | `app.py` 动态发现 `ui/pages/*.py`（模块暴露 `PAGE_TITLE` + `render()`），新增页面不改 `app.py` | 9 个页面可完全并行开发、文件级零冲突；符合 BRIEF “页面文件互不覆盖” | 备选：集中注册需改 app.py，会产生写入冲突且串行化 |
| D-10（Phase 2） | 推荐/研究结果**默认不自动下单**；只允许“加入自选池 → 策略/手动下单” | 用户目标升级为赚钱导向，但自动执行放大风险；保留人工确认点符合合规与 UBA | 备选：一键自动买入（需 OQ-6 拍板 + 安全评审，默认不做） |
| D-11（Phase 2） | **样本外强制**：未通过 walk-forward 的榜单/策略只能标记“研究阶段”，禁止接入自动化 | 回测好看但样本外失效是最大陷阱（BRIEF §9 风险 5） | 只展示样本内会导致用户误信 |
| D-12（Phase 2） | **CPU-only + 不引入深度学习框架**；ML 排序用 scikit-learn（可选依赖），深度模型留作远程训练的可选未来项 | 用户显卡 GTX 1650 显存有限；安装失败率与“能直接运行”目标冲突 | 引入 PyTorch 会显著增加环境失败率，且 M6 不需要 |
| D-13（F14，2026-10-26） | **全市场筛选采用“分层漏斗 + as_of 截断 + 复用 Phase 2 表”**（不新增表、不新增未来函数）；性能目标只承诺 200–300 只 ≤5 分钟 | 5000 只全量逐日重算成本过高；分层漏斗先剔除再打分，兼顾速度与质量；复用表避免契约膨胀 | 新增独立表会触发 CF-7 二次扩展；全量逐日扫描不现实 |
| D-14（F15，2026-10-26） | **筛选器“先验证后宣称”**：未通过样本外 RankIC>0.03 且 Top10 日超额>0（扣费后）前，只能标“研究阶段/无效”，禁止宣称有效、禁止接自动化 | 避免“看起来能选牛股”的伪 alpha 误导用户；与 OQ-8 门槛互补 | 不做验证直接展示会被用户当成买入建议（合规风险） |

## 附录 C：变更记录

| 版本 | 日期 | 变更 | 变更人 |
|---|---|---|---|
| v1.0 | 2026-10-25 | 初版：F1–F9 规格、A 股规则口径、数据模型（22 表）、接口契约、UI 9 页、非功能需求、开放问题 | pm-quant |
| v1.1 | 2026-10-25 | 按 Lead 裁决登记两项口径：①**RK012 金额类校验仅约束买入**（卖出不受 `min_order_amount` / `max_single_order_value_pct` 限制，仅受整手/持仓/停牌/涨跌停/时段约束）——更新 §4.4、§8.6 判定顺序与规则、§9.1、§9.2 RK012、新增 AC-F6-09；②**绩效口径**：`max_drawdown` 为负数、另出 `max_drawdown_abs` 正数，`calmar = annual / |mdd|` 除零返回 None 并在 `notes` 说明——更新 §6.4、§8.3 规则与异常、AC-F3-05 | pm-quant |
| v1.2 | 2026-10-25 | 按 Dev3 实现口径登记（Lead 已确认）：①`MarketSnapshot` 向后兼容扩展字段清单 + `assemble_market_snapshot()` 组装入口（§6.7）；②自动交易授权 `risk.allow_auto_orders`（默认 false，AUTO 单 fail-closed）与 `automation.enabled=true` 映射授权（§7、§8.6 规则 8/9、§9.1，新增 AC-F6-10）；③风控参数来源（`config.yaml` 基准 + `risk_config` 覆盖，RiskConfig 仅 DTO，RiskGate 纯函数）（§6.3、§9.1）；④关键数据缺失即拒绝（缺 price_times→RK006、缺时段标记→RK005、缺持仓→RK015）（§8.6 异常与降级，新增 AC-F6-11） | pm-quant |
| v1.3 | 2026-10-25 | **用户目标升级（赚钱导向）范围扩展（Lead 指示）**：①新增 Phase 2 功能 **F10 选股与推荐、F11 异动雷达、F12 自选池管理、F13 策略研究**（§8A，新增 19 条 AC，含“推荐理由可解释”“回测必须样本外”）；②新增 Phase 2 数据模型 5 张表（§5.4，CF-7 扩展待批）；③新增 Phase 2 接口（§6.8）与配置键（§7 recommend/research）；④合规强化：不承诺收益、推荐强制展示回测区间/样本外/UBA、禁用承诺性表述、推荐默认不自动下单（§11.8）；⑤新增警告码 `RQ_WARN_NO_BENCHMARK`（§9.2）；⑥新增 G7–G9 成功标准、M6 验收门槛、OQ-6/7/8、决策 D-10/11/12；⑦UI 页面由 9 页扩展为 11 页（新增 `watchlist.py`/`recommend.py`，§10） | pm-quant |
| v1.4 | 2026-10-25 | **Lead 批准落地（Phase 2 生效）**：①批准 Phase 2 契约扩展（§5.4 5 张表按“扩展新增、不改已有 22 表”、§6.8 接口、§7 recommend/research 配置键、2 个事件主题、`RQ_WARN_NO_BENCHMARK`）；②OQ-6 拍板“推荐禁止自动下单（仅带入手动交易页/需确认）”；③OQ-7 拍板“允许 sklearn 可选依赖（缺失降级）、禁止 torch/tensorflow”；④OQ-8 拍板“近 3 年 OOS 年化 ≥10% 且最大回撤 ≤15% 才算达到研究目标，标注研究参考不承诺，未达标只能研究阶段并禁止接自动化”（§8.13 规则 4）；⑤BRIEF 同步 v0.2（F1–F13） | pm-quant |
| v1.5 | 2026-10-25 | **Lead 裁决登记（M1 验收期）**：①**价格口径**：撮合/手数/资金核算一律用**不复权真实价格**；复权价仅用于收益/基准绩效展示与分红处理；hfq 合成价不得用于手数/资金核算；M4 若需双价列再单列任务（§4.6、§8.3 规则 2）；②新增 3 个警告码并明确“警告非阻断”：`RQ_WARN_MOCK_DATA`（离线 Mock 数据）、`RQ_WARN_PRICE_JUMP`（相邻交易日异常跳变）、`RQ_WARN_POSITION_MIN_LOT`（目标仓位不足 1 手已按 1 手调整）（§9.2） | pm-quant |
| v1.6 | 2026-10-25 | **Lead 批准新配置键**：`backtest.price_adjust`（默认 `none`）——`none`=不复权真实价撮合/算手数（默认推荐）；`hfq`=复权合成价全流程，**自动告警并关闭最小手数校验**（仅供研究对照）；登记于 §7 配置块与规则 6、§4.6 第 8 条、§8.3 规则 2。同步 M1 验收修订（REVIEW_M1 v1.1）：不复权基线、ML OOS AUC 0.4456 负结果、M1 结论=功能通过/有效性未证明、M6-08 为 R8 第一任务 | pm-quant |
| v1.7 | 2026-10-26 | **用户目标升级 + F14/F15（M6 P0）**：①目标从“可运行工作台”升级为“以盈利为目标的选股与自动交易系统”；②新增 **F14 全市场筛选**（分层漏斗：基础剔除→流动性→量价打分→过热剔除→TopN；显示全市场/过滤后/扫描数/数据时间；as_of 截断；可复现；CSV 导出；200–300 只 ≤5 分钟；研究阶段声明）与 **F15 筛选器有效性验证**（5 分组回测、5/10/20 日收益/超额、RankIC/ICIR、换手成本、walk-forward 样本外；判定线 RankIC>0.03 且 Top10 日超额>0 扣费后；未过不得宣称有效/接自动化）；③数据模型**复用** Phase 2 表（kind=SCREEN / SCREEN_VALIDATION，不新增表）、§6.8 新增 `ScreenService` 接口、§7 新增 `screen.*` 配置、§10 新增第 12 页 `screener.py`、§11.8 新增筛选合规条款、G10/G11、M6 验收门槛、D-13/D-14；④合规红线不变：不承诺收益、未过样本外不得接自动化 | pm-quant |
