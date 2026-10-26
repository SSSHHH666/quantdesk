# BORROW_MAP —— QuantDesk 借鉴代码精确索引与许可判定

> 版本 v1.0 · 2026-10-25 · 作者：code-scout（技术调研）
> 配套机读版：[`borrow_map.json`](borrow_map.json)（同目录，字段一一对应，可被脚本/UI 直接消费）
> 计数口径：正文表格 **38 个借鉴点**；同一借鉴点跨多个文件时，JSON 拆成多条，共 **42 条记录**。
> 对应任务：task-5；对应 BRIEF §6「借鉴来源」与 §2「合规口径」

## 0. 怎么用这份文档 / 判定规则

**用途**：Dev 拿到一行就能开工——知道「去哪个仓库的哪个文件看什么」「能不能抄代码」「抄完放我们哪个文件」。

**判定规则（与 BRIEF §2 一致，不另行解释）**

| 上游许可 | 处理方式 | 本表标记 |
|---|---|---|
| MIT / Apache-2.0 / BSD / Unlicense | 可直接复用代码，保留版权与许可声明，登记到 `docs/THIRD_PARTY.md` | ✅ 复用代码 |
| GPL-3.0 / LGPL-3.0 / AGPL-3.0 | 只重写设计思想，**不复制任何代码片段、不复制注释/变量名/文件结构** | ✏️ 重写思路 |
| 非商用 / 全版权保留 / 许可不明（NOASSERTION 且未核实） | 只读思路，绝不复制代码 | ✏️ 重写思路（保守） |

**路径真实性的保证方式（不是猜的）**

1. 用 `repo_tree.py` 抓 14 个仓库的完整文件树，落在 `data/trees/<owner>__<name>.txt`（共 14 个文件，含 `freqtrade__freqtrade.txt` 等）；
2. 本表每一行的路径都从对应树文件里筛出来，再用 `raw.githubusercontent.com` 拉全文（HTTP 200 实测），确认文件里确有本文描述的类/函数；
3. 链接统一指向**仓库默认分支**（freqtrade 的默认分支是 `develop`，main/master 均 404；lumibot=`dev`、nautilus_trader=`develop`、OpenBB=`develop`），已逐个实测 200。

**标记说明**：`优先级 1` = MVP（F1–F9）必须落地；`2` = 第二迭代就该有；`3` = 有余力再做。

---

## 1. 表 A：基础设施与执行层（映射模块 `core/` `trading/`）

| # | 借鉴点 | 来源仓库 | 具体文件路径 | GitHub blob 链接 | 许可 | 判定 | 映射模块 | 具体怎么改造 |
|---|---|---|---|---|---|---|---|---|
| A1 | 事件引擎：主题注册/注销 + 队列 + 定时器线程 + 通用 handler | vnpy/vnpy | `vnpy/event/engine.py` | [engine.py](https://github.com/vnpy/vnpy/blob/master/vnpy/event/engine.py) | MIT | ✅ 复用代码 | core | 抽成 `core/events.py`：`Event(type,data)`、`EventEngine.start/stop/put/register/unregister/register_general`，保留 1 秒定时器线程发 `EVENT_TIMER`；`_process` 内单 handler try/except，避免一个回调炸掉整条总线。事件名用 `vnpy/trader/event.py` 的常量思路改写成 BRIEF 的点号主题（`market.bar`/`signal`/`order.filled`/`risk.blocked`/`job.*`） |
| A2 | 网关基类契约：行情/交易回调 + connect/subscribe/send/cancel/query | vnpy/vnpy | `vnpy/trader/gateway.py` | [gateway.py](https://github.com/vnpy/vnpy/blob/master/vnpy/trader/gateway.py) | MIT | ✅ 复用代码 | trading | `BrokerAdapter` 直接对齐方法名：`connect/subscribe/send_order/cancel_order/query_account/query_position/query_history/close`；`on_tick/on_order/on_trade/on_position/on_account` 改成向事件总线 publish（而不是 vnpy 里直接 `event_engine.put`），这样回测与实盘共用一条链路 |
| A3 | 引擎注册表 + 内存态单点（MainEngine / OmsEngine） | vnpy/vnpy | `vnpy/trader/engine.py` | [engine.py](https://github.com/vnpy/vnpy/blob/master/vnpy/trader/engine.py) | MIT | ✅ 复用代码 | core/trading | 借 `MainEngine.add_engine/add_gateway/add_app` 的装配模式做 QuantDesk 的 bootstrap；`OmsEngine` 是「内存态订单/成交/持仓/账户 + get_all_xxx」的现成样板，落到 `trading/order_manager.py`+`portfolio.py`，落库异步做，UI 只读内存态 |
| A4 | 参数寻优配置 + 暴力/遗传并行搜索 | vnpy/vnpy | `vnpy/trader/optimize.py` | [optimize.py](https://github.com/vnpy/vnpy/blob/master/vnpy/trader/optimize.py) | MIT | ✅ 复用代码 | backtest | `OptimizationSetting.add_parameter/generate_settings`（网格笛卡尔积）几乎可原样用；`run_bf_optimization`（多进程 + 进度回调）作为第一版优化器；遗传算法部分可以留到 2 期 |
| A5 | 消息总线通配符主题匹配（`*`/`?` + 精确快路径 + 贪婪双指针） | nautechsystems/nautilus_trader | `crates/common/src/msgbus/matching.rs` | [matching.rs](https://github.com/nautechsystems/nautilus_trader/blob/develop/crates/common/src/msgbus/matching.rs) | LGPL-3.0 | ✏️ 重写思路 | core | 只抄设计：订阅用 pattern（`order.*`、`market.bar.?`），先判「无通配 = 直接 dict 查」，再走贪婪双指针；Python 约 30 行实现 `subscribe(pattern)`，别用 `startswith` 递归前缀匹配（vnpy 的做法在多级主题下会误匹配） |
| A6 | cache-then-publish 的顺序语义（先写状态缓存、再发事件） | nautechsystems/nautilus_trader | `docs/concepts/architecture.md` | [architecture.md](https://github.com/nautechsystems/nautilus_trader/blob/develop/docs/concepts/architecture.md) | LGPL-3.0 | ✏️ 重写思路 | core/trading | 写进 `core/events.py` 的纪律：任何 handler 触发的状态变更必须先 `cache.add_*` 成功再 `bus.publish`；回测与实盘同一顺序，避免 handler 读到旧 state（这是 nautilus「回测实盘同构」的根因之一） |
| A7 | Adapter 统一域模型分层（Domain↔Venue 双向转换 + 错误码映射） | nautechsystems/nautilus_trader | `docs/concepts/adapters.md` | [adapters.md](https://github.com/nautechsystems/nautilus_trader/blob/develop/docs/concepts/adapters.md) | LGPL-3.0 | ✏️ 重写思路 | trading | 每个 broker 适配器只做 3 件事：① 订单/持仓 Domain→Venue 转换 ② 回报 Venue→Domain 转换 ③ 券商错误码→`OrderRejected(reason)`；统一 dataclass（Order/Position/Account）放 `core/models.py`，CcxtBroker/QmtBroker 只写转换层 |
| A8 | 策略↔券商彻底解耦（Strategy 只产出 Order 对象，broker 由配置注入） | Lumiwealth/lumibot | `lumibot/strategies/strategy.py` | [strategy.py](https://github.com/Lumiwealth/lumibot/blob/dev/lumibot/strategies/strategy.py) | GPL-3.0 | ✏️ 重写思路 | strategy/trading | 策略侧只需要 4 个方法：`create_order/submit_order/get_positions/get_portfolio_value`，broker 从 `config.yaml` 注入；回测/模拟/实盘换 broker 不改策略代码——这是 BRIEF F9 的直接依据 |
| A9 | Broker 抽象基类的「最小方法集」划分 | Lumiwealth/lumibot | `lumibot/brokers/broker.py` | [broker.py](https://github.com/Lumiwealth/lumibot/blob/dev/lumibot/brokers/broker.py) | GPL-3.0 | ✏️ 重写思路 | trading | 从 13 个抽象方法里取最小集：`_submit_order`、`cancel_order`、`_get_balances_at_broker`、`_pull_positions`、`_pull_broker_order`，外加 `is_backtesting_broker` 标志；**不要**抄它的 streams/期权链实现（美股期权我们用不到） |
| A10 | 确定性下单校验闸门（挂在唯一 submit 路径内，失败即 return False） | Lumiwealth/lumibot | `lumibot/strategies/_strategy.py` | [_strategy.py](https://github.com/Lumiwealth/lumibot/blob/dev/lumibot/strategies/_strategy.py) | GPL-3.0 | ✏️ 重写思路 | trading | 扩成 `RiskGate.check(order, account, positions, market) -> RiskDecision(allow, reason)`；**唯一入口**是 `OrderService.place(order)`：先 check、不过就发 `risk.blocked` 事件并落审计；手动下单与自动下单都只能走这里（BRIEF F6 的硬要求） |
| A11 | 策略生命周期钩子（指标→入场→出场 + 确认/止损/仓位调整钩子） | freqtrade/freqtrade | `freqtrade/strategy/interface.py` | [interface.py](https://github.com/freqtrade/freqtrade/blob/develop/freqtrade/strategy/interface.py) | GPL-3.0 | ✏️ 重写思路 | strategy | 除 `on_bar(ctx)->list[Signal]` 外提供可选钩子：`confirm_signal`、`custom_stoploss`、`adjust_position`、`custom_stake_amount`，默认实现返回 None/空；策略作者只实现需要的，引擎统一按生命周期调用 |
| A12 | Provider 注册表（name + fetcher 能力字典 + credentials） | openbq-org/OpenBB | `openbb_platform/core/openbb_core/provider/abstract/provider.py`、`.../provider/registry.py` | [provider.py](https://github.com/openbq-org/OpenBB/blob/develop/openbb_platform/core/openbb_core/provider/abstract/provider.py) · [registry.py](https://github.com/openbq-org/OpenBB/blob/develop/openbb_platform/core/openbb_core/provider/registry.py) | NOASSERTION（README 称 Apache-2.0，**未核实**） | ✏️ 重写思路 | data | 我们的 provider 注册表照结构写：每类一个类，声明 `name/description/credentials/能力字典`；注册表 `dict[name] = provider`；`DataProvider` 工厂按配置顺序实例化 → 生成降级链 |
| A13 | Fetcher 三段式（transform_query → extract_data → transform_data） | openbq-org/OpenBB | `openbb_platform/core/openbb_core/provider/abstract/fetcher.py` | [fetcher.py](https://github.com/openbq-org/OpenBB/blob/develop/openbb_platform/core/openbb_core/provider/abstract/fetcher.py) | NOASSERTION（同上） | ✏️ 重写思路 | data | 每个数据接口拆 3 个纯函数：① 参数校验/规范化 ② 取数（含重试） ③ 标准化为列契约 `[date,symbol,open,high,low,close,volume]`；基类按序串起来并记录 provenance（数据源/请求参数/耗时），供校验报告与 UI 展示 |
| A14 | Provider 可用性/凭证映射（available_providers + credentials dict） | openbq-org/OpenBB | `openbb_platform/core/openbb_core/provider/registry_map.py` | [registry_map.py](https://github.com/openbq-org/OpenBB/blob/develop/openbb_platform/core/openbb_core/provider/registry_map.py) | NOASSERTION（同上） | ✏️ 重写思路 | data/ui | 生成 `{provider: {available, credentials: [...]}}` 快照，UI「设置」页渲染每个数据源状态（可用/缺 key/降级中），也是 `DataProvider` 选源顺序的可视化依据 |

## 2. 表 B：策略、因子与回测层（映射模块 `strategy/` `backtest/`）

| # | 借鉴点 | 来源仓库 | 具体文件路径 | GitHub blob 链接 | 许可 | 判定 | 映射模块 | 具体怎么改造 |
|---|---|---|---|---|---|---|---|---|
| B1 | 防未来函数自动检测：全量跑 vs 截断数据重跑，逐笔比信号 | freqtrade/freqtrade | `freqtrade/optimize/analysis/lookahead.py` | [lookahead.py](https://github.com/freqtrade/freqtrade/blob/develop/freqtrade/optimize/analysis/lookahead.py) | GPL-3.0 | ✏️ 重写思路 | backtest | 实现 `check_lookahead(strategy, df, sample=60)`：对每根 bar 用 `df[:i]` 重算指标+信号，与全量结果逐条比对，不一致即报「未来函数」；先抽样近 60 个交易日跑，避免 O(n²) 太慢。输出报告进 UI 回测页「可信度」区 |
| B2 | 可插拔优化目标（hyperopt loss 接口） | freqtrade/freqtrade | `freqtrade/optimize/hyperopt_loss/hyperopt_loss_interface.py`、`freqtrade/optimize/hyperopt/hyperopt_interface.py` | [hyperopt_loss_interface.py](https://github.com/freqtrade/freqtrade/blob/develop/freqtrade/optimize/hyperopt_loss/hyperopt_loss_interface.py) · [hyperopt_interface.py](https://github.com/freqtrade/freqtrade/blob/develop/freqtrade/optimize/hyperopt/hyperopt_interface.py) | GPL-3.0 | ✏️ 重写思路 | backtest | 定义 `Objective(metrics: dict) -> float` 纯函数 + 注册表（`sharpe`/`calmar`/`max_drawdown`/`trade_count`/多指标加权），优化器与目标解耦；不要把 optuna 的具体 API 抄进来，先手写网格/随机搜索即可 |
| B3 | Algo 组合式再平衡（策略树：选股→加权→再平衡） | pmorissette/bt | `bt/algos.py` | [algos.py](https://github.com/pmorissette/bt/blob/master/bt/algos.py) | MIT | ✅ 复用代码 | strategy/backtest | 直接搬类名与语义：`SelectAll/SelectN/WeighEqually/WeighTarget/Rebalance/RunMonthly`，多因子策略写成 `AlgoStack([SelectTopN, WeighScore, Rebalance])` 的 pandas 版；「Algo 是可调用对象、按顺序改 target 权重」这个抽象比"策略返回买卖信号"更适合行业轮动验证 |
| B4 | 目标权重→持仓的成本/撮合模型 | pmorissette/bt | `bt/backtest.py`、`bt/core.py` | [backtest.py](https://github.com/pmorissette/bt/blob/master/bt/backtest.py) · [core.py](https://github.com/pmorissette/bt/blob/master/bt/core.py) | MIT | ✅ 复用代码 | backtest | 借 `Backtest` 的 weights→positions 计算与 `CostModel/SqrtCostModel/AlmgrenChrissCostModel` 接口签名，落成 `broker_sim.py` 的手续费/滑点/冲击成本类；A 股额外叠加 T+1、涨跌停不可成交 |
| B5 | Alpha158 因子清单与窗口（可直接抄表的「因子字典」） | microsoft/qlib | `qlib/contrib/data/loader.py` | [loader.py](https://github.com/microsoft/qlib/blob/main/qlib/contrib/data/loader.py) | MIT | ✅ 复用代码 | strategy | 把 `Alpha158DL.get_feature_config` 的 `windows=[5,10,20,30,60]` 与因子名抄成 YAML/dict 清单：`ROC/MA/STD/BETA/RSQR/RESI/MAX/MIN/QTLU/QTLD/RANK/RSV/IMAX/IMIN` + price/volume ref；用 pandas 实现同名因子作为多因子策略特征池。**不要**搬 qlib 的表达式 DSL 与数据后端 |
| B6 | DataHandler 分层（原始 → 共享/学习/推理 processors） | microsoft/qlib | `qlib/data/dataset/handler.py` | [handler.py](https://github.com/microsoft/qlib/blob/main/qlib/data/dataset/handler.py) | MIT | ✅ 复用代码 | data/strategy | 借 `setup_data/fit/process_data/fetch` 四段式与「processor 分 learn/infer 两组」的约定：`data/cache.py` 只存 raw，归一化/去极值在 strategy 侧 fit 后 transform，避免把统计量写死进缓存导致回测信息泄露 |
| B7 | 因子 Zoo 注册表（扫目录 + 元数据 + 统一 compute） | HKUDS/Vibe-Trading | `agent/src/factors/registry.py` | [registry.py](https://github.com/HKUDS/Vibe-Trading/blob/main/agent/src/factors/registry.py) | MIT | ✅ 复用代码 | strategy | 一个因子=一个文件+`__alpha_meta__`（`id/theme/formula_latex/columns_required/min_warmup_bars/universe/frequency/decay_horizon`）；`Registry._scan()` 启动扫描、`compute(alpha_id, panel)` 统一入口、`health()`/`export_manifest()` 自检与导出。BRIEF F2「新增策略=新增一个文件」可直接套这个模型 |
| B8 | 因子算子面（DataFrame 向量化算子纯函数集） | HKUDS/Vibe-Trading | `agent/src/factors/base.py` | [base.py](https://github.com/HKUDS/Vibe-Trading/blob/main/agent/src/factors/base.py) | MIT | ✅ 复用代码 | strategy | 搬 `rank/zscore/scale/ts_rank/ts_corr/ts_cov/ts_argmax/ts_argmin/delta/decay_linear/signed_power/safe_div` 到 `indicators.py`，全部 DataFrame 向量化 + `safe_div` 保护除零；GTJA191/Alpha101 的公式实现（如 `zoo/alpha101/alpha_001.py`）依赖这一层，先有算子面再抄因子 |
| B9 | A 股代码归一化 + ETF/LOF 前缀识别 | HKUDS/Vibe-Trading | `agent/backtest/loaders/_symbol_utils.py` | [_symbol_utils.py](https://github.com/HKUDS/Vibe-Trading/blob/main/agent/backtest/loaders/_symbol_utils.py) | MIT | ✅ 复用代码 | data | 借 `_ETF_PREFIXES={15,16,50,51,52,56,58}` + 6 位码/后缀 `.SH/.SZ` 校验；QuantDesk 统一内部格式 `600519.SH`/`000001.SZ`，指数（`000001` 歧义码）走白名单单独判定 |
| B10 | 指标 Function 门面：同名批量动态绑定 | TA-Lib/ta-lib-python | `talib/abstract.py` | [abstract.py](https://github.com/TA-Lib/ta-lib-python/blob/master/talib/abstract.py) | BSD-2-Clause | ✅ 复用代码 | strategy | 用同样手法做 `indicators` 门面：遍历 `__TA_FUNCTION_NAMES__` 动态 `globals()[name] = Function(name)`；好处是 TA-Lib 装不上时，可以整体替换成 pandas 同名实现而**调用方零改动**（BRIEF §9 风险 3 的兜底方案） |
| B11 | 流式指标 API（每根已闭合 bar O(1)，peek/copy/advance） | TA-Lib/ta-lib-python | `talib/stream.py` | [stream.py](https://github.com/TA-Lib/ta-lib-python/blob/master/talib/stream.py) | BSD-2-Clause | ✅ 复用代码 | core/strategy | 盘中扫描要增量：借 `update(bar)/peek(forming)/copy()/advance()/out_range` 语义，给 MA/RSI/MACD 写 pandas 增量版；历史不足返回「未就绪」（对应 `InsufficientHistory`）而不是抛错 |
| B12 | 绩效指标全集与默认口径 | ranaroussi/quantstats | `quantstats/stats.py` | [stats.py](https://github.com/ranaroussi/quantstats/blob/main/quantstats/stats.py) | Apache-2.0 | ✅ 复用代码 | backtest | 搬公式与默认参数（`periods=252/rf=0/compounded=True`）：sharpe/smart_sharpe/sortino/calmar/max_drawdown/cagr/win_rate/profit_factor/VaR/CVaR/probabilistic_sharpe_ratio；**先统一 period-based 收益序列口径**再算胜率，否则短线策略指标失真（其 README 也提示了这点） |
| B13 | HTML 绩效报告（tear sheet 一键生成） | ranaroussi/quantstats | `quantstats/reports.py` | [reports.py](https://github.com/ranaroussi/quantstats/blob/main/quantstats/reports.py) | Apache-2.0 | ✅ 复用代码 | ui/backtest | 用 `reports.html(returns, benchmark, rf, title, output)` 出报告；UI 回测页加「下载报告」按钮直接调它，不自写 HTML 模板（保留 LICENSE/NOTICE） |
| B14 | Walk-forward + purge/embargo 切分（含 CPCV 概念） | stefan-jansen/machine-learning-for-trading | `06_strategy_definition/02_cv_foundations.py` | [02_cv_foundations.py](https://github.com/stefan-jansen/machine-learning-for-trading/blob/main/06_strategy_definition/02_cv_foundations.py) | MIT | ✅ 复用代码 | backtest | 参照 `WalkForwardCV(n_splits=5, test_size=252, expanding=True, label_horizon=21)` 写我们的 `time_series_split`：训练/验证之间按**交易日**留 purge 间隔（不是自然日！），再加 embargo；输出 OOS 权益曲线 + 每折指标，作为「回测可信度」第二证据 |
| B15 | Deflated Sharpe / PBO 防过拟合（脚本内自实现函数） | stefan-jansen/machine-learning-for-trading | `16_strategy_simulation/12_dsr_validation.py` | [12_dsr_validation.py](https://github.com/stefan-jansen/machine-learning-for-trading/blob/main/16_strategy_simulation/12_dsr_validation.py) | MIT | ✅ 复用代码 | backtest/ui | 抄脚本里**自己实现**的 `deflated_sharpe_ratio/probabilistic_sharpe_ratio/compute_pbo(CSCV 分块)`；**不要**引入 `ml4t.diagnostic` 依赖（其许可未在本次核实范围内）。回测结果页展示「试了 N 组参数后的 DSR 概率」+「PBO%」红黄绿灯，BRIEF §9 风险 5 的落地 |

## 3. 表 C：数据、自动化与 UI 层（映射模块 `data/` `automation/` `ui/`）

| # | 借鉴点 | 来源仓库 | 具体文件路径 | GitHub blob 链接 | 许可 | 判定 | 映射模块 | 具体怎么改造 |
|---|---|---|---|---|---|---|---|---|
| C1 | 离线接口注册表（name/module/category/params/outputs/example） | akfamily/akshare | `akshare/data/interfaces.json` | [interfaces.json](https://github.com/akfamily/akshare/blob/main/akshare/data/interfaces.json) | MIT | ✅ 复用代码 | data | 读它的**结构**设计 `data/registry.py` 的接口清单（每个接口的入参 schema、输出列名、示例、限流说明）；用途：① UI 数据浏览器按清单渲染 ② 未来接 LLM 时直接喂清单选接口。不直接分发 akshare 的 json 文件本身 |
| C2 | 请求重试/限流/错误分类（指数退避 + 429 特判） | akfamily/akshare | `akshare/request.py` | [request.py](https://github.com/akfamily/akshare/blob/main/akshare/request.py) | MIT | ✅ 复用代码 | data | `make_request_with_retry_json` 的指数退避 + `RateLimitError/NetworkError/DataParsingError` 分类，落到 `AkshareProvider` 包装层，统一转成 `DataFetchError(source, reason, retryable)`，让上层降级链能判断「换源」还是「直接报错」 |
| C3 | 主备源降级 + 防封限流（「备用源速查 & 降级策略」「东财防封」章节） | simonlin1212/a-stock-data | `SKILL.md`（章节：数据源优先级 & 东财防封 / 备用源速查 & 降级策略） | [SKILL.md](https://github.com/simonlin1212/a-stock-data/blob/main/SKILL.md) | Apache-2.0 | ✅ 复用代码 | data | 落成 provider 链：每类数据声明有序源列表（如 K 线 `[tencent, sina, eastmoney]`）+ 每源独立限流器（最小间隔+抖动，参考其 `em_get` 的 1s 串行）+ 失败自动切换并记 `fallback` 事件；缓存优先，绝不并发猛拉（其真实封禁令案例：10 线程 1 小时 4.5 万请求被封 IP） |
| C4 | 数据口径校验（成交量单位/复权/科创板特殊规则） | simonlin1212/a-stock-data | `SKILL.md`（§1.2 成交量对账口径、V3.10.1 更正说明） | [SKILL.md](https://github.com/simonlin1212/a-stock-data/blob/main/SKILL.md) | Apache-2.0 | ✅ 复用代码 | data | `cache.py` 校验报告加三项断言：① volume 单位（手 vs 股，科创板更特殊）② 复权方式（qfq/hfq/不复权，同一表内不可混）③ 涨跌停价与昨收一致性；不一致写 warnings 并阻断入库，**不静默覆盖** |
| C5 | Ticker 归一化与歧义码防呆（前缀/后缀互斥 + 指数白名单） | simonlin1212/a-stock-data | `SKILL.md`（章节：Ticker 格式归一化 / 市场前缀规则） | [SKILL.md](https://github.com/simonlin1212/a-stock-data/blob/main/SKILL.md) | Apache-2.0 | ✅ 复用代码 | data | 用**整串锚定**正则（别用 `re.search(r"\d{6}")` 捞 6 位，`6005190` 会被截成 `600519`）+ 市场前缀/后缀二选一校验；`000001`（上证指数 vs 平安银行）这类歧义码强制要求带市场标识，静默选错标的比报错危险得多 |
| C6 | 定时任务调度器（多时间点日任务 + 后台轮询 + 优雅退出） | ZhuLinsen/daily_stock_analysis | `src/scheduler.py` | [scheduler.py](https://github.com/ZhuLinsen/daily_stock_analysis/blob/main/src/scheduler.py) | MIT | ✅ 复用代码 | automation | 借 `set_daily_task(多时间点)/add_background_task(≥30s 轮询)/GracefulShutdown(signal handler)` 三件套；我们用 APScheduler 实现同等能力，但保留「配置热更新后重排 job」的 refresh 逻辑与「任务异常不杀主循环」的 safe_run |
| C7 | 通知渠道路由（route_type→env_key/config_attr + 每渠道一个 sender） | ZhuLinsen/daily_stock_analysis | `src/notification_routing.py`、`src/notification_sender/__init__.py` | [notification_routing.py](https://github.com/ZhuLinsen/daily_stock_analysis/blob/main/src/notification_routing.py) · [__init__.py](https://github.com/ZhuLinsen/daily_stock_analysis/blob/main/src/notification_sender/__init__.py) | MIT | ✅ 复用代码 | automation | `notify.py` 照抄结构：渠道基类 `send(title, content) -> bool`，路由表 `report/alert/system_error -> (env_key, config_attr)`；未配置渠道静默跳过并输出「能力清单」，UI 设置页据此显示哪些通道可用（BRIEF F8） |
| C8 | 决策看板字段契约（pydantic 嵌套模型，前后端共用） | ZhuLinsen/daily_stock_analysis | `src/schemas/report_schema.py` | [report_schema.py](https://github.com/ZhuLinsen/daily_stock_analysis/blob/main/src/schemas/report_schema.py) | MIT | ✅ 复用代码 | ui | 直接借字段名做我们总览/策略页的固定 schema：`CoreConclusion`、`BattlePlan(sniper_points{ideal_buy,stop_loss,take_profit})`、`DataPerspective(trend_status/price_position/volume_analysis/chip_structure)`、`PhaseDecision(action_window/watch_conditions)`、`SignalAttribution`；UI 与后端按同一契约渲染，后续换 LLM 生成也不会破版 |
| C9 | 端点的离线回归测试组织方式（按数据源/版本分文件，全 mock） | simonlin1212/a-stock-data | `tests/test_v310_sources.py` | [test_v310_sources.py](https://github.com/simonlin1212/a-stock-data/blob/main/tests/test_v310_sources.py) | Apache-2.0 | ✅ 复用代码 | data（配套 `tests/`） | QA 照此写 provider 回归测试：每个端点一个断言（字段存在/单位/最少条数），网络全 mock，断网也能跑；数据源改版时只需改对应文件 |

---

## 4. 许可判定摘要（可直接登记 `docs/THIRD_PARTY.md`）

| 来源仓库 | 许可（GitHub API 实测） | 判定 | 复用范围 | 需保留 |
|---|---|---|---|---|
| vnpy/vnpy | MIT | ✅ 复用代码 | 事件引擎/网关基类/MainEngine·OmsEngine/参数优化 | LICENSE + 版权声明 + 出处 URL |
| pmorissette/bt | MIT | ✅ 复用代码 | Algo/AlgoStack 与成本模型 | 同上 |
| microsoft/qlib | MIT | ✅ 复用代码 | Alpha158 因子清单/DataHandler 四段式（仅思想与清单，不搬 DSL） | 同上 |
| HKUDS/Vibe-Trading | MIT | ✅ 复用代码 | 因子注册表/算子面/代码归一化 | 同上；README 注明 v0.1.x 接口高频变动，锁定版本 |
| akfamily/akshare | MIT | ✅ 复用代码 | 接口清单结构/重试策略 | 同上；数据仅个人研究，遵守上游数据源条款 |
| simonlin1212/a-stock-data | Apache-2.0 | ✅ 复用代码 | 降级链/口径校验/ticker 归一化/测试组织（SKILL.md 为文档型代码） | LICENSE + NOTICE |
| ZhuLinsen/daily_stock_analysis | MIT | ✅ 复用代码 | 调度器/通知路由/看板 schema | 同上 |
| ranaroussi/quantstats | Apache-2.0 | ✅ 复用代码 | 绩效指标公式/HTML 报告 | LICENSE + NOTICE |
| TA-Lib/ta-lib-python | BSD-2-Clause | ✅ 复用代码 | 指标门面/流式 API 语义 | COPYRIGHT 原文 |
| stefan-jansen/machine-learning-for-trading | MIT | ✅ 复用代码 | walk-forward 切分/DSR·PBO **脚本内自实现函数** | 同上；**不引入 `ml4t.diagnostic`**（许可未核实） |
| freqtrade/freqtrade | GPL-3.0 | ✏️ 重写思路 | 只借：策略钩子清单、lookahead 检测方法、hyperopt 目标抽象 | 不复制代码；文档/实现里注明「思路来自 freqtrade」 |
| Lumiwealth/lumibot | GPL-3.0 | ✏️ 重写思路 | 只借：Strategy↔Broker 解耦、Broker 最小方法集、唯一 submit 校验 | 同上 |
| nautechsystems/nautilus_trader | LGPL-3.0 | ✏️ 重写思路 | 只借：主题通配符匹配、cache-then-publish、Adapter 分层 | 同上 |
| openbq-org/OpenBB | **NOASSERTION**（README 称 Apache-2.0，LICENSE 未核实） | ✏️ 重写思路（保守） | 只借：provider 注册表结构、Fetcher 三段式、可用性映射 | 若后续核实 LICENSE 确为 Apache-2.0，可升级为 ✅ 复用（需 Lead 批准） |

**红线复述**：GPL/LGPL 三个仓库（freqtrade、lumibot、nautilus_trader）**一行代码都不复制**，包括注释、变量名、文件结构；只把设计写进本节表 A/B 的「怎么改造」列，由 Dev 用我们自己的命名重写。

---

## 5. 未定位到 / 需人工确认的项（不编造）

| 项 | 现状 | 影响/建议 |
|---|---|---|
| vnpy 4.x 的具体网关实现（CTP/IB 等） | 主仓 `vnpy/trader/gateway.py` **只有 `BaseGateway` 抽象基类**；具体网关已拆到独立仓库（`vnpy_ctp` 等），本次未逐个抓树 | 我们只需要基类契约（表 A2），**够用**；若日后接 QMT，再让 Lead 批准抓 `vnpy_qmt` 仓库确认路径 |
| vnpy 的 App 插件（cta_strategy 等） | 主仓 `vnpy/trader/app.py` 只有 `BaseApp` 元数据类，`docs/community/app/*.md` 里是各 app 的文档 | 表 A 未单列 app 实现；`BaseApp` 元数据思路已并入 A3 的装配模式 |
| lumibot 的「AI agent 决策/风控」专用模块 | 主仓抓树**未发现**独立 agents 包（只有 `strategies/` `brokers/` `components/`）；报告 B 描述的「确定性风控闸门」对应到 `_validate_order` 与 Broker 层校验（表 A10） | 若需 LLM agent 那一层，需另查 lumibot 文档/商业 BotSpot 部分（不在本次范围，未定位到） |
| nautilus_trader 的 Python 版 MessageBus 实现 | 抓树显示仓库已**整体 Rust 化**（`crates/` 为主，未见 `nautilus_trader/` Python 包目录），故只引 `docs/concepts/*` 与 `crates/common/src/msgbus/*` 作设计来源 | 表 A5/A6/A7 的路径真实存在且内容匹配；若想要 v1.x Python 实现，需指定 tag（如 `1.2xx`）后再确认路径 |
| freqtrade 默认分支 | 默认分支是 **`develop`**（`main`/`master`/`develop` 中只有 develop 能抓树），`repo_tree.py` 的 main→master 探测会 404 | 本表 freqtrade 链接统一用 `develop`；后续抓该仓库请手工指定分支 |
| OpenBB 许可 | GitHub API 返回 `NOASSERTION`，README 自称 Apache-2.0 | 已按保守策略判为「重写」；Lead 若要复用，需先核实 `LICENSE` 原文 |
| quantstats 报告模板 | `reports.html` 依赖其内部 `plots`/`utils` 模块，且项目仍是 0.0.x | 表 B13 只借最终调用方式；若要自定义美化，改我们自己的 Streamlit 页面而不是改 quantstats 内部 |
| ml4t 的 `ml4t.diagnostic` 包 | 脚本 import 了该包（DSR/PBO 的库版实现），但包本身不在本仓库树内、许可未核实 | 表 B15 明确「只抄脚本内自实现函数」，不引入该包 |

---

## 6. 实现模式速查（Dev 照着写，伪代码级）

> 这 8 个模式覆盖了 MVP 里最容易做歪、且返工成本最高的地方。每个模式都标了「落地文件」与「验收点」。

### P1. 事件总线（Event Bus）—— 来源：vnpy A1 + nautilus A5/A6

```
core/events.py
class Event: type:str, data:Any
class EventEngine:
    init(): queue=Queue(); handlers=defaultdict(list); general=[]; active=False; thread; timer_thread
    start(): active=True; thread=Thread(_run); timer=Thread(_run_timer)
    stop(): active=False; threads.join()
    put(e): queue.put(e)
    register(type, handler): handlers[type].append(handler)
    register_general(handler): general.append(handler)        # 全量监听，用于审计落库
    _run(): while active: e=queue.get(); _process(e)
    _process(e): for h in handlers.get(e.type,[]) + general: try h(e) except Exception: log
    _run_timer(): 每 1s put(Event(EVENT_TIMER))
    # 订阅支持通配符（A5）：register("order.*", h) → 匹配时用 match(topic, pattern)
顺序纪律（A6）：状态变更先 cache/DB 再 publish；handler 里禁止直接改别人模块的状态
```
**落地**：`core/events.py` · **验收**：策略发 `signal`、OrderService 发 `order.filled`、风控发 `risk.blocked`，UI 订阅后 1s 内刷新；单 handler 抛异常不影响其它订阅者。

### P2. 策略插件注册表 + 参数 schema —— 来源：Vibe-Trading B7 + freqtrade A11

```
strategy/base.py
class Strategy(ABC):
    name: str                       # 唯一键，如 "ma_cross"
    params_schema: dict             # pydantic 模型/JSON Schema，UI 自动渲染表单
    startup_bars: int = 0           # 预热所需 bar 数（避免前 N 根出信号）
    def on_bar(self, ctx) -> list[Signal]: ...
    def confirm_signal(self, s) -> bool: return True          # 可选钩子
REGISTRY = {}
def register(name): 装饰器 → REGISTRY[name] = cls
def get(name): 查表，未注册抛 StrategyNotFound
# 新增策略 = strategy/builtin/xxx.py 一个文件 + @register("xxx") + import 触发
```
**落地**：`strategy/base.py`、`strategy/builtin/*.py` · **验收**：`pytest` 里 `for name in REGISTRY: run_backtest(name)` 全绿；删掉一个文件策略就消失，无需改引擎。

### P3. 确定性风控闸门（唯一入口）—— 来源：lumibot A10 + BRIEF F6

```
trading/risk.py
@dataclass RiskDecision: allow:bool; reason:str; rule:str
class RiskGate:
    def check(self, order, account, positions, market) -> RiskDecision:
        规则按顺序短路：黑名单 → 单票仓位上限 → 总仓位上限 → 单日亏损熔断 → 价格偏离(现价±X%) → 可用资金
        任一条不过 → return RiskDecision(False, reason, rule)
        return RiskDecision(True, "ok", "")
trading/order_service.py
def place(self, order, source):        # source ∈ {manual, strategy, scheduler}
    d = self.risk.check(order, account, positions, market)
    if not d.allow: events.put(Event("risk.blocked", {...})); audit.write(...); return Rejected(d.reason)
    return broker.submit_order(order)
```
**红线**：UI 手动下单、策略自动下单、定时任务下单 **全部只能调 `OrderService.place`**；`broker.submit_order` 在代码评审里禁止被 UI/策略直接调用（可加单测断言）。
**落地**：`trading/risk.py`、`trading/order_service.py`（或并入 order_manager）· **验收**：绕过 `OrderService` 直接调 broker 的用例测试失败。

### P4. Provider 注册表 + 统一返回 + 主备降级链 —— 来源：OpenBB A12–A14 + akshare C1/C2 + a-stock C3

```
data/provider.py
class DataProvider(ABC):
    name: str; capabilities: set[str]; credentials: list[str]
    def get_bars(self, symbols, start, end, freq) -> DataFrame  # 列契约固定
PROVIDERS = {}  # name -> instance，注册顺序 = 默认优先级
class ProviderChain:
    def __init__(self, order: list[str], limiters: dict[str, RateLimiter]): ...
    def get_bars(...):
        for name in self.order:                      # 1) 按优先级
            p = PROVIDERS[name]
            try:
                with limiter[name]:                  # 2) 每源独立限流（间隔+抖动）
                    df = p.get_bars(...)
                validate(df)                         # 3) 口径校验（单位/复权/涨跌停一致性）
                emit("data.fetched", {source:name, rows:len(df)})
                return df
            except RetryableError: continue          # 4) 可重试→换源
            except FatalError:   raise
        raise AllSourcesFailed(...)
```
**落地**：`data/provider.py`、`data/cache.py`、`data/registry.py` · **验收**：断网时自动切 `MockProvider`；单源被限流时日志里出现 `fallback` 且结果仍正确；校验报告能列出成交量单位/复权不一致。

### P5. 回测-实盘同源（同一套订单编排与风控）—— 来源：nautilus A6/A7 + bt B4 + lumibot A8

```
trading/order_service.py  ← 回测和实盘共用这一层
BacktestBroker(BrokerAdapter)   # 内部用 backtest/broker_sim.py 的撮合：T+1/涨跌停/费用/滑点
PaperBroker(BrokerAdapter)      # 模拟盘，内存撮合
CcxtBroker / QmtBroker          # 真实通道（骨架）
三者都实现：submit_order/cancel_order/get_positions/get_account/get_bars
回测 = 引擎把 bar 灌进同一个 EventLoop → 策略 → 风控 → OrderService → BacktestBroker
```
**纪律**：策略/风控/订单服务三层里**不得出现 `if backtest:` 分支**；差异只允许存在于 BrokerAdapter 实现与数据来源。
**落地**：`backtest/engine.py`、`trading/broker.py` · **验收**：同一个策略同一段数据，回测引擎与 PaperBroker 走的调用链日志完全一致（除时间源）。

### P6. 因子 Zoo（AlphaMeta + 算子面 + 热加载）—— 来源：Vibe-Trading B7/B8 + qlib B5

```
strategy/factors/
  base.py     # 算子面：rank/zscore/ts_rank/ts_corr/ts_argmax/delta/decay_linear/safe_div
  alpha_001.py:
      __alpha_meta__ = {id, theme, formula_latex, columns_required, min_warmup_bars, universe, frequency}
      def compute(panel: dict[str, DataFrame]) -> DataFrame   # 宽表 日期×股票
  registry.py # scan() 遍历目录 + importlib 加载；compute(id, panel)；health() 检查缺列/长度
多因子策略 = 选 N 个因子 → 截面 zscore/rank → 加权 → 生成目标权重 → Rebalance
```
**落地**：`strategy/factors/*`、`strategy/builtin/multi_factor.py` · **验收**：加一个因子文件无需改注册代码；`health()` 能报出「缺 close 列 / 历史不足 min_warmup_bars」。

### P7. 参数优化（可插拔目标 + 网格/随机搜索）—— 来源：vnpy A4 + freqtrade B2

```
backtest/optimizer.py
@dataclass ParamSpace: name; values: list | range
def grid(space: list[ParamSpace]) -> Iterator[dict]        # 笛卡尔积（vnpy generate_settings）
def random_search(space, n) -> Iterator[dict]              # 大空间用随机
OBJECTIVES = {"sharpe": f, "calmar": f, "max_dd": f, "combo": f}   # 注册表
def optimize(strategy_name, space, objective, n_jobs=4):
    for params in grid/random:  run_backtest(params) → metrics → score
    返回 top-k + 全部结果表（供 UI 热力图/表格）
```
**落地**：`backtest/optimizer.py`、UI「策略」页参数区 · **验收**：3 组参数 × 5 折回测能并行跑完且结果可复现（固定种子/顺序）。

### P8. 抗过拟合三件套（walk-forward + lookahead 自检 + DSR/PBO）—— 来源：ml4t B14/B15 + freqtrade B1

```
backtest/validation.py
1) walk_forward(df, n_splits=5, test_size=252, expanding=True, purge=21, embargo=2):
     for 每折: train=df[:i- purge]  val=df[i:i+test_size]  → 训练参数 → 在 val 上 OOS 评估
     拼接 OOS 权益曲线 → 输出每折指标 + 整体 OOS 指标
2) check_lookahead(strategy, df, sample=60):
     full = strategy.signals(df)
     for i in 抽样最后 60 根: cut = strategy.signals(df[:i])
       if full[:i] != cut: 记录「未来函数」位置
3) overfit_report(returns_matrix: 参数×时间):
     dsr = deflated_sharpe_ratio(...)     # 参考 ml4t 脚本内自实现
     pbo = compute_pbo(CSCV 对半切分)      # PBO 越低越好
     输出：DSR 概率 / PBO% / 试参数量 N / 红黄绿灯
UI：回测页「可信度」区块必须展示这三项结果，不允许只显示夏普
```
**落地**：`backtest/validation.py`、`backtest/metrics.py`、UI 回测页 · **验收**：故意写一个用 `df.shift(-1)` 的策略，`check_lookahead` 必须报出未来函数。

---

## 7. 执行顺序建议（给 PM 排期用）

1. **先做**：A1（事件总线）→ A2/A3（BrokerAdapter/OmsEngine）→ P3（风控闸门）→ C1/C2/C3（数据 provider 链）→ B5（Alpha158 清单）→ P2（策略注册表）。这几项是其它所有模块的地基。
2. **再做**：P5（回测实盘同源）→ P4 / B3 / B4 / B10（指标与成本）→ B12/B13（绩效与报告）→ P1 的 UI 订阅。
3. **然后**：B1/B14/B15（三类防过拟合）→ P7（参数优化）→ C6/C7/C8（自动化与看板）→ B11（流式指标）。
4. **最后**：B7/B8 因子 Zoo 扩充（GTJA191/Alpha101），视工期决定是否引入第二因子池。

## 8. 参考证据（仓库内文件）

- 文件树（14 个仓库）：`data/trees/` 下 `vnpy__vnpy.txt`、`freqtrade__freqtrade.txt`、`Lumiwealth__lumibot.txt`、`nautechsystems__nautilus_trader.txt`、`pmorissette__bt.txt`、`microsoft__qlib.txt`、`HKUDS__Vibe-Trading.txt`、`akfamily__akshare.txt`、`simonlin1212__a-stock-data.txt`、`openbq-org__OpenBB.txt`、`ZhuLinsen__daily_stock_analysis.txt`、`ranaroussi__quantstats.txt`、`TA-Lib__ta-lib-python.txt`、`stefan-jansen__machine-learning-for-trading.txt`
- 前置调研：`quant_github_projects.md`、`data/eval/report_A.md`、`report_B.md`、`report_C.md`
- 项目契约：`quantdesk/BRIEF.md`（§2 合规口径、§5 目录与接口、§6 借鉴来源）
