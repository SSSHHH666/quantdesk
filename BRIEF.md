# QuantDesk 项目简报（Lead 起草 · 团队唯一事实来源）

> 版本 **v0.3** · 2026-10-26 · 由 Lead 根据用户原话转译，PM 可修订并回传
> v0.2 变更（用户目标升级，Lead 批准）：范围由 F1–F9 扩展为 **F1–F13**；新增 **Phase 2（M6）**：选股与推荐、异动雷达、自选池管理、策略研究（ML/walk-forward 样本外）；明确 **CPU-only、不引入深度学习框架、不承诺收益（UBA）**。
> v0.3 变更（2026-10-26，Lead 批准）：目标升级为“**以盈利为目标的选股与自动交易系统**”；新增 **F14 全市场筛选**、**F15 筛选器有效性验证**（M6 当前 P0）；重申不承诺收益、未过样本外不得接自动化。

## 1. 用户意图转译（用户原话 → 产品需求）

用户原话要点：
1. 个人项目，**不依附任何公司、不服务任何公司，非商业用途**，因此可直接吸收 GitHub 开源项目的代码与设计。
2. 想做一个**大项目**：既能**手动交易**、又能**全自动交易**的量化交易系统，以量化为主。
3. 要吸收调研成果里「比较好的方面」，并**记录 GitHub 网址与代码位置**。
4. 团队按**互联网大厂研发流程**组织：项目经理（1-2）、开发（2-3）、测试（1）、集成/整体跑通与逻辑修正（1）。
5. 用户**代码能力薄弱**：交付必须完成度高、**能直接运行**、有友好 UI 与人交互。
6. 先由项目经理智能体修正/补充需求逻辑并重新输出，再分发给程序员智能体。
7. **目标升级（v0.2，用户原话）**：项目要“**以赚钱为导向**”；希望系统 ①推荐自选池 ②自选池可自行添加 ③找到赚钱办法（多因子/ML/公开方法等）④为手动/自动下单留接口 ⑤关注资金流入大、异动盘；分两条线——**远期（年化 10–20% 价值型）** + **近期（异动前兆、拉升起爆点）**。用户为个人自用，**不承诺收益、风险自担（UBA）**。

转译后的产品定义：
**QuantDesk** —— 一个本地运行的量化交易工作台。数据自动获取与落库 → 策略信号 → 风控闸门 → 回测验证 → 模拟盘/实盘执行 → 定时自动化 → 可视化与通知。用户既可点几下按钮手动下单，也可开启策略全自动运行。
**Phase 2（M6）**在此基础上增加“研究 → 选股 → 验证 → 交易”闭环：选股与推荐（远期价值型/近期异动型两套可解释榜单）+ 异动雷达 + 自选池管理 + 策略研究（ML 排序、walk-forward 样本外、参数稳健性）。系统只提供工具与信号，**不承诺收益**。

## 2. 合规口径（重要，所有成员遵守）

用户为个人自用、非商业、不对外分发，合规上可以吸收开源代码。但为保留未来自由度，执行以下规则：

| 许可类型 | 处理方式 |
|---|---|
| MIT / Apache-2.0 / BSD / Unlicense | **可直接复用代码**，保留版权与许可声明，登记到 `THIRD_PARTY.md` |
| GPL-3.0 / LGPL-3.0 / AGPL-3.0（vnpy 除外，vnpy 是 MIT；freqtrade/OctoBot/lumibot 为 GPL，backtesting.py/FinceptTerminal 为 AGPL） | **只借鉴设计思想，代码自行重写**（避免未来分发/商用时的传染义务） |
| 非商用许可（rqalpha）、全版权保留（mlfinlab） | **只读思路，绝不复制代码** |
| 数据集/接口（akshare、a-stock-data、yfinance） | 使用时遵守上游条款，默认仅个人研究；数据源不稳定要有 fallback |

所有第三方依赖与借鉴代码必须在 `docs/THIRD_PARTY.md` 登记：名称、版本、许可、来源 URL、用途、是复用还是重写。

**收益与推荐合规（v0.2，所有成员遵守）**：
1. 系统只提供**研究工具与交易信号**，**不承诺收益、不代客理财、不构成投资建议**；用户自担风险（**UBA**）。
2. 所有推荐/研究结果必须展示**回测区间 + 样本外表现 + 风险提示 + UBA 声明**；未通过样本外的只能标“研究阶段”，禁止接入自动化。
3. 全站禁用“稳赚/保证收益/必涨/无风险”等承诺性表述。
4. 推荐/筛选结果**默认不自动下单**：只能“一键带入手动交易页/需用户二次确认”；自动化任务不得消费 recommend/screen 输出直接下单（OQ-6 已拍板）。
5. **全市场筛选（F14/F15）**：结果固定标注“研究阶段 · 不构成买入建议 · 不承诺收益”；未通过 F15 验证（样本外 RankIC>0.03 且 Top 组 10 日超额>0 扣费后）前，禁止宣称有效、禁止接自动化。

## 3. 功能范围（MVP F1–F9 + Phase 2 F10–F13）——必须全部可运行

> **MVP（F1–F9，M0–M5）** 为第一版必须交付；**Phase 2（F10–F13，M6）** 为用户目标升级后新增（已批准，入口为 M1 验收通过）。

| 编号 | 功能 | 验收标准（摘要） |
|---|---|---|
| F1 | **数据层** | akshare 拉 A 股日线/指数/基本面 → SQLite 缓存 → 增量更新，重复拉取不重复写；数据缺失/异常有校验报告 |
| F2 | **策略层** | 策略插件接口 + 注册表（新增策略=新增一个文件）；内置 3 个示例策略（双均线、RSI 反转、多因子打分），参数可配置 |
| F3 | **回测引擎** | 支持手续费、滑点、A 股 T+1、涨跌停不可成交；输出收益/年化/最大回撤/夏普/胜率/交易明细/权益曲线；结果可对比 |
| F4 | **模拟交易** | 账户、持仓、订单、成交、资金曲线完整；UI 手动下单（买/卖/撤单）即时生效 |
| F5 | **自动化** | APScheduler 定时任务（盘前准备/盘中扫描/盘后总结）；策略信号 → 风控 → 订单管线；可一键启停 |
| F6 | **风控闸门** | 单票仓位上限、总仓位上限、单日亏损熔断、黑名单、价格偏离校验；**所有订单（手动+自动）必须经过同一风控函数**，不可绕过 |
| F7 | **Web UI** | Streamlit 多页：总览 / 行情 / 手动交易 / 策略 / 回测 / 持仓 / 自动化 / 设置 / 日志 |
| F8 | **通知与日志** | 结构化日志 + 审计流水；通知支持桌面/邮件/Webhook（至少一种可用，配置可关） |
| F9 | **实盘适配层** | `BrokerAdapter` 抽象 + `PaperBroker` 完整实现 + `CcxtBroker`（加密，testnet）与 `QmtBroker` 骨架；不配真实账户也能跑通全流程 |
| **F10** | **选股与推荐（Phase 2 / M6）** | 远期价值型（研究目标年化 10–20%，不承诺）与近期异动型两套榜单；每条推荐**≥2 条可解释理由**（因子+值/分位+贡献值）；展示回测区间与**样本外表现**、风险提示与 UBA；推荐**默认不自动下单** |
| **F11** | **异动雷达（Phase 2 / M6）** | 量比/换手/振幅/涨速/缺口/资金流（数据可得）等规则扫描；阈值可配、3 日去重、缺数据跳过不伪造；输出雷达分并与近期榜联动 |
| **F12** | **自选池管理（Phase 2 / M6）** | 分组增删改、标的增删改、代码归一化、CSV/文本导入（先预览差异）、导出；推荐与自动化默认只处理自选池 |
| **F13** | **策略研究（Phase 2 / M6）** | 因子库扩充、ML 排序（CPU，可选 scikit-learn）、walk-forward（≥3 折 + purge/embargo）、参数稳健性；**回测必须样本外**，未通过只能标“研究阶段” |
| **F14** | **全市场筛选（Phase 2 / M6，2026-10-26）** | 全 A（约 5000 只）分层筛选：基础剔除→流动性→量价信号打分→过热剔除→TopN；显示**全市场总数/过滤后/扫描数/数据时间**；`as_of` 截断无未来数据；结果可复现、可导出 CSV；200–300 只扫描 ≤5 分钟；固定标注“研究阶段·不构成买入建议” |
| **F15** | **筛选器有效性验证（Phase 2 / M6，2026-10-26）** | 按得分分 5 组做分组回测（未来 5/10/20 日收益/超额、RankIC、换手成本，walk-forward 样本外）；**判定线：样本外 RankIC>0.03 且 Top 组 10 日超额>0（扣费后）**；未通过不得宣称有效、不得接自动化 |

**第一版（MVP，M0–M5）明确不做**：真实资金接入（需用户券商接口与账户，先留适配层）、A 股 L2 逐笔、多用户/权限、云部署、移动端、深度学习/GPU 训练。
**Phase 2（M6）边界**：只做 F10–F15；不承诺收益；不自动下单（推荐/筛选结果仅入手动交易页或自选池，需确认）；不使用 GPU、不引入 torch/tensorflow（sklearn 为可选依赖，缺失时 ML 功能不可用并提示）。
**用户目标（v0.3）**：**以盈利为目标的选股与自动交易系统**（核心诉求：短线可盈利）。**合规红线不变**：不承诺收益（UBA）、未过样本外验证（walk-forward / F15）不得进入推荐或自动化。

## 4. 技术栈（Lead 已定，不建议更换）

- 解释器：`D:\Anaconda3\python.exe`（Python 3.12.7），依赖装在项目内 `quantdesk/.venv`
- UI：Streamlit 1.37（已装）+ plotly 5.24
- 存储：SQLite + SQLAlchemy 2.0（已装）
- 调度：APScheduler（待装）
- 数据：akshare（待装）、pandas 2.2
- 指标：TA-Lib（待装，BSD-2）；若安装失败，用 pandas 自实现同名指标兜底（接口保持一致）
- 可选：ccxt（加密测试网）
- **Phase 2 可选**：scikit-learn（CPU，BSD-3，仅 ML 排序；缺失时 ML 功能不可用并提示）；**禁止引入 torch/tensorflow 等深度学习框架，全程 CPU-only**
- 配置：YAML + pydantic 2.8
- 测试：pytest
- 启动：`scripts/start.bat`（双击）+ `streamlit run app.py`

## 5. 目录与模块契约（文件级隔离，避免并行冲突）

```
quantdesk/
  app.py                    # Streamlit 入口（导航 + 页面注册）
  core/                     # Dev1
    config.py               # 配置加载（config.yaml + 环境变量）
    models.py               # pydantic 模型 + SQLAlchemy ORM 表
    db.py                   # 引擎/会话/建表/迁移
    events.py               # 轻量事件总线（借鉴 vnpy EventEngine、nautilus MessageBus 思想，自行实现）
    logging.py              # 结构化日志
  data/                     # Dev1
    provider.py             # DataProvider 抽象 + AkshareProvider + MockProvider
    cache.py                # 增量缓存与校验
    watchlist.py            # （Phase 2/M6）自选池分组/CRUD/导入导出/代码归一化
  strategy/                 # Dev2
    base.py                 # Strategy 基类 + 注册表 + 参数 schema
    builtin/ma_cross.py     # 双均线
    builtin/rsi_reversal.py # RSI 反转
    builtin/multi_factor.py # 多因子打分
    builtin/value_quality.py      # （Phase 2/M6）远期价值型
    builtin/breakout_abnormal.py  # （Phase 2/M6）近期异动突破
    recommend.py            # （Phase 2/M6）推荐引擎 + 异动雷达（两榜/可解释理由/样本外字段）
    ml_ranker.py            # （Phase 2/M6）ML 排序（CPU-only，sklearn 可选）
    screener.py             # （Phase 2/M6，v0.3）全市场筛选 + 有效性验证（F14/F15）
  backtest/                 # Dev2
    engine.py               # 回测引擎（事件驱动）
    broker_sim.py           # 模拟撮合（T+1/涨跌停/费用/滑点）
    metrics.py              # 绩效指标
    validation.py           # （Phase 2/M6）walk-forward 样本外/参数稳健性
  trading/                  # Dev3
    broker.py               # BrokerAdapter + PaperBroker + CcxtBroker/QmtBroker 骨架
    order_manager.py        # 订单生命周期（唯一入口 OrderService）
    risk.py                 # 风控闸门（唯一入口）
    portfolio.py            # 账户/持仓/资金曲线
  automation/               # Dev3
    scheduler.py            # APScheduler 封装
    jobs.py                 # 盘前/盘中/盘后
    notify.py               # 通知渠道
  ui/                       # Dev3（页面文件互不覆盖）
    pages/*.py              # MVP 9 + Phase 2：watchlist / recommend / screener（合计 12 页）
  tests/                    # QA
  docs/                     # PM/Scout/Integrator
  scripts/                  # Integrator
  config.yaml
  requirements.txt
```

**关键接口（先定死，开发按此实现）**：
- `DataProvider.get_bars(symbols, start, end, freq) -> DataFrame[date, symbol, open, high, low, close, volume]`
- `Strategy.on_bar(ctx) -> list[Signal]`，`Strategy.params_schema()`；策略通过注册表 `@register("ma_cross")` 暴露
- `BrokerAdapter.submit_order(order) -> OrderResult`、`cancel_order(id)`、`get_positions()`、`get_account()`
- `RiskGate.check(order, account, positions, market) -> RiskDecision(allow, reason)`；**下单唯一入口**：`OrderService.place(order)` 内部必调风控
- 事件主题：`market.bar`、`signal`、`order.submitted/filled/rejected`、`risk.blocked`、`job.*`；**Phase 2 新增（已批准）**：`recommend.updated`、`watchlist.changed`
- **Phase 2 接口（M6，已批准）**：`data/watchlist.py::WatchlistService`；`strategy/recommend.py::RecommendService.build/scan_abnormal`；`strategy/ml_ranker.py::MLRanker`；`backtest/validation.py::walk_forward(...)`（详见 `docs/SPEC.md` §6.8）；**推荐默认不自动下单**（OQ-6）

## 6. 借鉴来源（Scout 产出 `docs/BORROW_MAP.md`：网址 + 具体文件路径 + 借鉴点 + 复用/重写判定）

vnpy（事件引擎、gateway/app 分层）、freqtrade（策略接口、hyperopt、lookahead-analysis）、lumibot（策略-broker 解耦、确定性风控闸门）、nautilus_trader（消息总线/Cache/Adapter）、bt（Algo 组合式再平衡）、qlib（Alpha158 因子、DataHandler）、Vibe-Trading（478 因子 Zoo、A 股代码映射）、akshare（数据接口）、a-stock-data（主备源降级/口径校验）、OpenBB（provider 注册表）、daily_stock_analysis（推送/看板/定时）、quantstats（绩效报告）、TA-Lib（指标）、ml4t（walk-forward/Deflated Sharpe 防过拟合）。

调研证据：`quant_github_projects.md`、`data/eval/report_A.md`、`report_B.md`、`report_C.md`、`data/eval/*.md`。

**Phase 2（M6）重点借鉴（已批准）**：qlib Alpha158 因子清单、Vibe-Trading 因子 Zoo/算子面、ml4t 的 walk-forward/purge/embargo 与 Deflated Sharpe 思路、jesse 的 Monte Carlo/规则显著性思路、freqtrade 的 lookahead 检测思路（GPL 仅重写，不复制代码）。

## 7. 团队与流程

| 角色 | 人数 | 职责 |
|---|---|---|
| Lead | 1 | 架构决策、契约审批、代码评审、最终集成验收、与用户沟通 |
| PM（pm-quant） | 1 | 需求规格 SPEC、任务分解 TASKS、验收标准、进度监督、需求变更裁决 |
| 架构/技术负责人 | 1（Lead 兼） | 接口契约、技术选型、跨模块决策 |
| Dev1 | 1 | 基础设施：配置/模型/DB/事件/日志/数据层 |
| Dev2 | 1 | 策略引擎 + 回测引擎 |
| Dev3 | 1 | 交易执行/模拟盘/风控 + 自动化 + UI |
| QA | 1 | pytest、端到端脚本、缺陷清单、验收测试 |
| Integrator | 1 | 装配跑通、修集成缺陷、启动脚本、RUNBOOK、发版 |

流程（每轮）：
1. PM 出 SPEC/TASKS（带验收标准与文件归属）
2. Lead 审核契约 → 按文件归属并行派单给 Dev
3. Dev 自测 + 提交（每次改动必须可运行）
4. QA 按验收标准跑测试，输出缺陷清单
5. Integrator 集成、冒烟、修依赖、写文档
6. Lead 验收 → 用户可操作演示

纪律：**不跨文件写**、不改他人模块、每次提交带测试、配置不硬编码、无网络时用 MockProvider 兜底。

## 8. 里程碑

- **M0 骨架**：venv + 依赖 + 目录 + 配置 + DB + 日志 + 空 UI 能启动
- **M1 研究闭环**：数据层 + 回测 + 3 策略 → 能跑出绩效报告
- **M2 交易闭环**：模拟盘 + 手动下单 UI + 持仓/资金曲线
- **M3 自动化**：定时任务 + 风控闸门 + 通知
- **M4 交付**：实盘适配层骨架 + 全量测试 + 一键启动 + RUNBOOK
- **M5 打磨**：图表美化、报告导出、示例数据集、演示脚本
- **M6 策略研究增值（Phase 2，v0.2 新增）**：推荐两榜（远期价值型/近期异动型）+ 异动雷达 + 自选池 + 策略研究（ML 排序/walk-forward 样本外/参数稳健性）→ 样本外达标（近 3 年 OOS 年化 ≥10% 且最大回撤 ≤15%）才标“达到研究目标（研究参考、不承诺收益）”；CPU-only
- **M6 全市场筛选（v0.3 新增，P0）**：F14 全市场分层筛选（5000 只 → TopN；as_of 截断、可复现、CSV 导出、200–300 只 ≤5 分钟、研究阶段声明）+ F15 有效性验证（5 分组、RankIC、扣费超额、walk-forward；判定线 RankIC>0.03 且 Top10 日超额>0）→ 未通过不得宣称有效、不得接自动化

## 9. 已知风险

1. **A 股实盘接入**：真实下单需券商柜台（QMT/miniQMT、Ptrade 等）与账户，第一版只做适配层与模拟盘，需用户后续提供接口。
2. **数据源稳定性**：akshare 为爬虫聚合，可能限流/改版；必须有缓存 + Mock/备源。
3. **TA-Lib 安装**：Windows 需 wheel；失败时用 pandas 兜底实现（接口不变）。
4. **并行开发冲突**：靠文件级归属 + 契约冻结规避。
5. **策略有效性**：不承诺盈利；回测必须防过拟合（walk-forward、样本外、事务成本），UI 明示风险。
6. **推荐误导/合规（Phase 2）**：用户可能把“推荐”当“保证”；必须可解释 + 样本外强制 + “研究阶段”标记 + UBA 声明 + 推荐默认不自动下单。
7. **全市场筛选预期（v0.3）**：5000 只全量扫描有成本（限流/耗时）；分层漏斗 + 缓存 + 分批；未通过 F15 验证不得宣称有效；筛选结果只是候选，不是买入建议。
7. **数据缺口（Phase 2）**：资金流/龙虎榜/分钟数据依赖第三方源且不稳定；异动雷达按“数据可得才启用、缺失明确标注、不伪造”降级。
8. **工期膨胀（Phase 2）**：M6 以 M1 验收为硬门槛；先 F10/F12 再 F11/F13；ML 作为可选增强。

## 10. 变更记录

| 版本 | 日期 | 变更 | 变更人 |
|---|---|---|---|
| v0.1 | 2026-10-25 | 初版：用户意图转译、合规口径、MVP F1–F9、技术栈、目录/接口契约、里程碑 M0–M5、风险 | Lead |
| v0.2 | 2026-10-25 | 用户目标升级（赚钱导向）：①范围扩展 F1–F13；②新增 Phase 2（M6）F10–F13 与目录/接口/事件主题（5 表/4 接口/2 事件）；③明确 CPU-only、不引入深度学习（sklearn 可选）、不承诺收益/UBA、推荐默认不自动下单；④里程碑新增 M6；⑤风险新增推荐误导/数据缺口/工期；⑥新增 §2 收益与推荐合规 4 条 | Lead 批准，PM 执行 |
| v0.3 | 2026-10-26 | 目标升级为“**以盈利为目标的选股与自动交易系统**”：①新增 **F14 全市场筛选**、**F15 筛选器有效性验证**（Phase 2 / M6 当前 P0）；②目录新增 `strategy/screener.py` + `ui/pages/screener.py`；③新增 §2 合规第 5 条（筛选研究阶段/未过验证不得宣称有效）；④M6 里程碑增加全市场筛选；⑤风险新增全市场筛选预期；⑥合规红线不变（不承诺收益、未过样本外不得接自动化） | Lead 批准，PM 执行 |
