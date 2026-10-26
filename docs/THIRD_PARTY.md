# QuantDesk 第三方与借鉴来源登记（THIRD_PARTY）

> 版本：v1.0 · 日期：2026-10-25 · 维护：Integrator（集成/交付）
> 依据：`BRIEF.md` 第 2、6 节；`docs/BORROW_MAP.md` 第 4 节；`docs/borrow_map.json`（42 条记录、14 个来源项目）
> 适用：QuantDesk M0–M5 全部本地版本。每次新增第三方代码、依赖或数据源，必须同步更新本文件。

---

## 0. 登记规则与判定口径

### 0.1 许可判定

| 许可类型 | 处理方式 | 本文件图例 |
|---|---|---|
| MIT / Apache-2.0 / BSD / Unlicense | **可复用代码**；保留原版权声明、LICENSE / NOTICE，并登记来源 URL | ✅ 复用（reuse） |
| GPL-3.0 / LGPL-3.0 / AGPL-3.0 | **只借鉴设计思想，代码全部自行重写**；不复制代码、注释、变量名与文件结构 | ✏️ 重写（rewrite） |
| 非商用许可、全版权保留（如 rqalpha、mlfinlab） | **只读思路，绝不复制代码** | ✏️/📖 仅思路 |
| 数据接口与数据集（akshare、a-stock-data、yfinance 等） | 遵守上游条款；本项目默认仅个人研究使用；数据源不稳定必须有缓存与 fallback | 数据源 |

### 0.2 三条红线

1. **GPL / LGPL / AGPL 项目一行代码都不复制**，包括注释、变量名、目录结构；只把设计写进我们自己的模块。
2. 复用 MIT / Apache-2.0 / BSD 代码时，**必须保留版权声明与许可文本**，并在对应源文件头部注明出处。
3. 许可无法确认的项目（当前为 OpenBB）**按最保守方式处理**：只重写思路，不复用代码；待核实 LICENSE 原文并经 Lead 批准后方可升级。

### 0.3 免责声明

- QuantDesk 为**个人自用、非商业、不对外分发**的本地量化研究工作台。
- 本项目**不构成任何投资建议**；回测收益不代表未来表现；A 股数据来自公开接口，可能存在缺失、延迟或口径差异。
- 所有第三方商标、代码与数据的权利归其各自所有者；本文件仅作来源与许可记录，不代表任何形式的授权背书。

---

## 1. 借鉴来源汇总（按项目）

> 共 14 个来源项目、42 条明细。判定以 `docs/borrow_map.json` 的 `reuse_policy` 为准；`rewrite` 表示只借鉴设计、代码重写。

| 项目 | 来源 URL | 许可 | 主要用途（QuantDesk 落地模块） | 判定 | 需保留 / 注意事项 |
|---|---|---|---|---|---|
| vnpy/vnpy | https://github.com/vnpy/vnpy | MIT | 事件引擎、Gateway/App 分层、主引擎装配、参数网格优化 | ✅ 复用 4 处 | LICENSE、版权声明、出处 URL |
| nautechsystems/nautilus_trader | https://github.com/nautechsystems/nautilus_trader | LGPL-3.0 | 消息总线通配符、cache-then-publish、Adapter 分层 | ✏️ 重写 3 处 | 不复制代码；仅 Python 层自行实现 |
| Lumiwealth/lumibot | https://github.com/Lumiwealth/lumibot | GPL-3.0 | 策略与 Broker 解耦、Broker 最小方法集、唯一 submit 校验 | ✏️ 重写 3 处 | 不复制代码；文档注明“思路来自 lumibot” |
| freqtrade/freqtrade | https://github.com/freqtrade/freqtrade | GPL-3.0 | 策略钩子清单、lookahead 检测方法、优化目标抽象 | ✏️ 重写 4 处 | 不复制代码；文档注明“思路来自 freqtrade” |
| openbq-org/OpenBB | https://github.com/openbq-org/OpenBB | NOASSERTION（README 称 Apache-2.0，LICENSE 未核实） | Provider 注册表、Fetcher 三段式、数据源可用性快照 | ✏️ 重写 4 处 | 保守处理；核实 LICENSE 并经 Lead 批准后才可复用 |
| pmorissette/bt | https://github.com/pmorissette/bt | MIT | Algo/权重再平衡语义、成本模型接口 | ✅ 复用 3 处 | LICENSE、版权声明、出处 URL |
| microsoft/qlib | https://github.com/microsoft/qlib | MIT | Alpha158 因子清单、DataHandler 四段式（仅思想与清单，不搬 DSL） | ✅ 复用 2 处 | 同上；不引入 qlib 数据后端 |
| HKUDS/Vibe-Trading | https://github.com/HKUDS/Vibe-Trading | MIT | 因子 Zoo 注册表、算子面、A 股代码归一化 | ✅ 复用 3 处 | 同上；锁定 v0.1.x 版本（接口高频变动） |
| TA-Lib/ta-lib-python | https://github.com/TA-Lib/ta-lib-python | BSD-2-Clause | 指标门面、流式 API 语义；缺失时 pandas 兜底 | ✅ 复用 2 处 | 保留 COPYRIGHT 原文 |
| ranaroussi/quantstats | https://github.com/ranaroussi/quantstats | Apache-2.0 | 绩效指标公式、HTML tear sheet 报告 | ✅ 复用 2 处 | LICENSE + NOTICE |
| stefan-jansen/machine-learning-for-trading | https://github.com/stefan-jansen/machine-learning-for-trading | MIT | walk-forward 切分、DSR / PBO 防过拟合 | ✅ 复用 2 处 | 同上；**不引入 `ml4t.diagnostic`**（许可未核实） |
| akfamily/akshare | https://github.com/akfamily/akshare | MIT | 数据接口清单结构、限流与重试策略 | ✅ 复用 2 处 | 同上；数据仅个人研究，遵守上游数据源条款 |
| simonlin1212/a-stock-data | https://github.com/simonlin1212/a-stock-data | Apache-2.0 | 主备源降级、数据口径校验、ticker 归一化、离线回归测试组织 | ✅ 复用 4 处 | LICENSE + NOTICE |
| ZhuLinsen/daily_stock_analysis | https://github.com/ZhuLinsen/daily_stock_analysis | MIT | 调度器封装、通知路由、看板字段契约 | ✅ 复用 4 处 | LICENSE、版权声明、出处 URL |

---

## 2. 借鉴来源明细（`borrow_map.json` 全量 42 条）

> “落地模块”是 QuantDesk 的目标文件或模块；标注“二期/预留”的条目不在 MVP 第一版范围内，仅登记设计来源。

### 2.1 vnpy/vnpy（MIT · ✅ 复用）

| # | 来源文件 | 落地模块 | 借鉴内容与用途 | 判定 |
|---|---|---|---|---|
| 1 | [vnpy/event/engine.py](https://github.com/vnpy/vnpy/blob/master/vnpy/event/engine.py) | `core/events.py` | Event/EventEngine 的主题注册/注销、队列、1 秒定时器、通用 handler；改为点号主题（`market.bar` / `signal` / `order.*` / `risk.blocked` / `job.*`），handler 内 try/except 互不干扰 | ✅ 复用 |
| 2 | [vnpy/trader/gateway.py](https://github.com/vnpy/vnpy/blob/master/vnpy/trader/gateway.py) | `trading/broker.py` | `BaseGateway` 方法契约对齐 `BrokerAdapter`：connect/subscribe/send_order/cancel_order/query_account/query_position/query_history/close；回报改为向事件总线 publish | ✅ 复用 |
| 3 | [vnpy/trader/engine.py](https://github.com/vnpy/vnpy/blob/master/vnpy/trader/engine.py) | `core/` 装配、`trading/order_manager.py` | `MainEngine.add_engine/add_gateway/add_app` 装配模式做 bootstrap；`OmsEngine` 的订单/成交/持仓/账户内存态作为订单管理与持仓服务样板，UI 只读内存态 | ✅ 复用 |
| 4 | [vnpy/trader/optimize.py](https://github.com/vnpy/vnpy/blob/master/vnpy/trader/optimize.py) | `backtest/`（参数对比，二期） | `OptimizationSetting.add_parameter/generate_settings` 生成参数网格笛卡尔积；多进程 + 进度回调作为后续网格优化器，遗传算法不纳入 | ✅ 复用 |

### 2.2 nautechsystems/nautilus_trader（LGPL-3.0 · ✏️ 重写）

| # | 来源文件 | 落地模块 | 借鉴内容与用途 | 判定 |
|---|---|---|---|---|
| 5 | [crates/common/src/msgbus/matching.rs](https://github.com/nautechsystems/nautilus_trader/blob/develop/crates/common/src/msgbus/matching.rs) | `core/events.py` | 只借鉴设计：订阅支持通配符 `order.*`；先精确查 dict，再贪婪双指针匹配；Python 约 30 行自行实现，不用 `startswith` 前缀匹配 | ✏️ 重写 |
| 6 | [docs/concepts/architecture.md](https://github.com/nautechsystems/nautilus_trader/blob/develop/docs/concepts/architecture.md) | `core/` + `trading/` | 只借鉴纪律：状态先写 Cache 成功再 publish 事件（cache-then-publish）；回测与实盘保持同一顺序，避免订阅者读到旧状态 | ✏️ 重写 |
| 7 | [docs/concepts/adapters.md](https://github.com/nautechsystems/nautilus_trader/blob/develop/docs/concepts/adapters.md) | `trading/broker.py` | 只借鉴分层：每个 broker 适配器只做 Domain→Venue、Venue→Domain、错误码映射三件事；统一 dataclass 放 `core/models.py` | ✏️ 重写 |

### 2.3 Lumiwealth/lumibot（GPL-3.0 · ✏️ 重写）

| # | 来源文件 | 落地模块 | 借鉴内容与用途 | 判定 |
|---|---|---|---|---|
| 8 | [lumibot/strategies/strategy.py](https://github.com/Lumiwealth/lumibot/blob/dev/lumibot/strategies/strategy.py) | `strategy/base.py`、`trading/` | 只借鉴解耦：策略侧仅 `create_order/submit_order/get_positions/get_portfolio_value` 四个动作，broker 由配置注入；回测/模拟/实盘换 broker 不改策略代码 | ✏️ 重写 |
| 9 | [lumibot/brokers/broker.py](https://github.com/Lumiwealth/lumibot/blob/dev/lumibot/brokers/broker.py) | `trading/broker.py` | 只借鉴抽象基类的最小方法集：提交、撤单、查询余额、拉取持仓/订单，以及 `is_backtesting_broker` 标志；期权、数据流等实现不纳入 | ✏️ 重写 |
| 10 | [lumibot/strategies/_strategy.py](https://github.com/Lumiwealth/lumibot/blob/dev/lumibot/strategies/_strategy.py) | `trading/risk.py`、`trading/order_manager.py` | 只借鉴闸门思想：`_validate_order` 挂在唯一 submit 路径内，失败即拒绝；扩展为 `RiskGate.check(order, account, positions, market) -> RiskDecision`，唯一入口 `OrderService.place`，手动/自动下单都走这里 | ✏️ 重写 |

### 2.4 freqtrade/freqtrade（GPL-3.0 · ✏️ 重写）

| # | 来源文件 | 落地模块 | 借鉴内容与用途 | 判定 |
|---|---|---|---|---|
| 11 | [freqtrade/strategy/interface.py](https://github.com/freqtrade/freqtrade/blob/develop/freqtrade/strategy/interface.py) | `strategy/base.py` | 只借鉴钩子清单：`on_bar` 之外提供可选钩子 confirm_signal / custom_stoploss / adjust_position / custom_stake_amount，默认空实现，引擎按生命周期统一调用 | ✏️ 重写 |
| 12 | [freqtrade/optimize/analysis/lookahead.py](https://github.com/freqtrade/freqtrade/blob/develop/freqtrade/optimize/analysis/lookahead.py) | `backtest/`（防未来函数自检，二期） | 只借鉴方法：全量跑一遍拿交易/信号，再对每根 bar 用截断数据重跑比对，不一致即报未来函数；抽样近 60 个交易日控制 O(n²) 成本 | ✏️ 重写 |
| 13 | [freqtrade/optimize/hyperopt_loss/hyperopt_loss_interface.py](https://github.com/freqtrade/freqtrade/blob/develop/freqtrade/optimize/hyperopt_loss/hyperopt_loss_interface.py) | `backtest/`（目标函数接口，二期） | 只借鉴接口形状：`Objective(metrics) -> score` 纯函数 + 注册表（sharpe/calmar/max_drawdown/trade_count），优化器与目标解耦 | ✏️ 重写 |
| 14 | [freqtrade/optimize/hyperopt/hyperopt_interface.py](https://github.com/freqtrade/freqtrade/blob/develop/freqtrade/optimize/hyperopt/hyperopt_interface.py) | `backtest/`（参数搜索，二期） | 只借鉴可插拔参数空间思路；不引入 optuna，第一版仅手写网格/随机搜索 | ✏️ 重写 |

### 2.5 openbq-org/OpenBB（NOASSERTION · ✏️ 保守重写）

| # | 来源文件 | 落地模块 | 借鉴内容与用途 | 判定 |
|---|---|---|---|---|
| 15 | [openbb_core/provider/abstract/provider.py](https://github.com/openbq-org/OpenBB/blob/develop/openbb_platform/core/openbb_core/provider/abstract/provider.py) | `data/provider.py` | 只借鉴结构：Provider 声明 name/description/credentials/能力字典；注册表 `dict[name]=provider`，工厂按配置顺序实例化生成降级链 | ✏️ 重写 |
| 16 | [openbb_core/provider/registry.py](https://github.com/openbq-org/OpenBB/blob/develop/openbb_platform/core/openbb_core/provider/registry.py) | `data/provider.py` | 只借鉴极简注册表形态（include_provider / providers），避免大量 if-elif 选源；加载失败记 warning，不崩进程 | ✏️ 重写 |
| 17 | [openbb_core/provider/abstract/fetcher.py](https://github.com/openbq-org/OpenBB/blob/develop/openbb_platform/core/openbb_core/provider/abstract/fetcher.py) | `data/provider.py` | 只借鉴三段式：transform_query（参数校验）/ extract_data（取数含重试）/ transform_data（标准化为统一 OHLCV 列），基类串起并记录 provenance | ✏️ 重写 |
| 18 | [openbb_core/provider/registry_map.py](https://github.com/openbq-org/OpenBB/blob/develop/openbb_platform/core/openbb_core/provider/registry_map.py) | `data/provider.py`、设置页 | 只借鉴产物形态：生成 `{provider: {available, credentials}}` 快照，设置页渲染每个数据源状态（可用/缺 key/降级中） | ✏️ 重写 |

### 2.6 pmorissette/bt（MIT · ✅ 复用）

| # | 来源文件 | 落地模块 | 借鉴内容与用途 | 判定 |
|---|---|---|---|---|
| 19 | [bt/algos.py](https://github.com/pmorissette/bt/blob/master/bt/algos.py) | `strategy/builtin/multi_factor.py` | 复用类名与语义：SelectAll / SelectN / WeighEqually / WeighTarget / Rebalance / RunMonthly；多因子策略写成 AlgoStack 的 pandas 版 | ✅ 复用 |
| 20 | [bt/backtest.py](https://github.com/pmorissette/bt/blob/master/bt/backtest.py) | `backtest/broker_sim.py`、`backtest/engine.py` | 复用目标权重→持仓计算与成本模型挂钩方式，落成手续费/滑点类，再叠加 A 股 T+1 与涨跌停 | ✅ 复用 |
| 21 | [bt/core.py](https://github.com/pmorissette/bt/blob/master/bt/core.py) | `backtest/broker_sim.py` | 复用 CostModel / SqrtCostModel / AlmgrenChrissCostModel 的接口签名（volume/volatility 入参），保持可替换 | ✅ 复用 |

### 2.7 microsoft/qlib（MIT · ✅ 复用）

| # | 来源文件 | 落地模块 | 借鉴内容与用途 | 判定 |
|---|---|---|---|---|
| 22 | [qlib/contrib/data/loader.py](https://github.com/microsoft/qlib/blob/main/qlib/contrib/data/loader.py) | `strategy/`（因子清单，二期扩展） | 复用 Alpha158DL.get_feature_config 的因子清单与 windows=[5,10,20,30,60]（ROC/MA/STD/BETA/RSQR/RESI/MAX/MIN/QTLU/QTLD/RANK/RSV/IMAX/IMIN + price/volume ref），落成 YAML + pandas 实现；不搬 qlib 表达式 DSL 与数据后端 | ✅ 复用 |
| 23 | [qlib/data/dataset/handler.py](https://github.com/microsoft/qlib/blob/main/qlib/data/dataset/handler.py) | `data/cache.py`、策略侧预处理 | 复用 DataHandlerLP 的 setup_data/fit/process_data/fetch 四段式与 learn/infer processor 约定：cache 只存 raw，归一化在策略侧 fit 后 transform，避免统计量写死进缓存导致信息泄露 | ✅ 复用 |

### 2.8 HKUDS/Vibe-Trading（MIT · ✅ 复用）

| # | 来源文件 | 落地模块 | 借鉴内容与用途 | 判定 |
|---|---|---|---|---|
| 24 | [agent/src/factors/registry.py](https://github.com/HKUDS/Vibe-Trading/blob/main/agent/src/factors/registry.py) | `strategy/`（因子注册表，二期） | 复用因子 Zoo 注册表：一个因子 = 一个文件 + `__alpha_meta__`（id/theme/formula/columns_required/min_warmup_bars/universe/frequency）；启动扫描、`compute(id, panel)` 统一入口、health()/export_manifest() 自检 | ✅ 复用 |
| 25 | [agent/src/factors/base.py](https://github.com/HKUDS/Vibe-Trading/blob/main/agent/src/factors/base.py) | `strategy/`（算子面，二期） | 复用算子面：rank/zscore/scale/ts_rank/ts_corr/ts_cov/ts_argmax/ts_argmin/delta/decay_linear/signed_power/safe_div；全部 DataFrame 向量化并做除零保护 | ✅ 复用 |
| 26 | [agent/backtest/loaders/_symbol_utils.py](https://github.com/HKUDS/Vibe-Trading/blob/main/agent/backtest/loaders/_symbol_utils.py) | `data/provider.py` | 复用 A 股代码归一化：6 位码 + `.SH`/`.SZ` 后缀校验、ETF/LOF 前缀集 {15,16,50,51,52,56,58}；内部统一 `600519.SH` / `000001.SZ`，指数歧义码走白名单 | ✅ 复用 |

### 2.9 TA-Lib/ta-lib-python（BSD-2-Clause · ✅ 复用）

| # | 来源文件 | 落地模块 | 借鉴内容与用途 | 判定 |
|---|---|---|---|---|
| 27 | [talib/abstract.py](https://github.com/TA-Lib/ta-lib-python/blob/master/talib/abstract.py) | `strategy/base.py`（指标兼容层） | 复用指标门面手法：遍历函数名动态 `globals()[name] = Function(name)`，使 TA-Lib 缺失时可整体替换为 pandas 同名实现，调用方零改动 | ✅ 复用 |
| 28 | [talib/stream.py](https://github.com/TA-Lib/ta-lib-python/blob/master/talib/stream.py) | `strategy/base.py`（指标兼容层） | 复用流式语义：update(bar)/peek(forming)/copy()/advance()/out_range；为 MA/RSI/MACD 写 pandas 增量版，历史不足返回“未就绪”而非抛错 | ✅ 复用 |

### 2.10 ranaroussi/quantstats（Apache-2.0 · ✅ 复用）

| # | 来源文件 | 落地模块 | 借鉴内容与用途 | 判定 |
|---|---|---|---|---|
| 29 | [quantstats/stats.py](https://github.com/ranaroussi/quantstats/blob/main/quantstats/stats.py) | `backtest/metrics.py` | 复用指标公式与默认口径（periods=252 / rf=0 / compounded=True）：sharpe / sortino / calmar / max_drawdown / cagr / win_rate / profit_factor / VaR / CVaR；先统一 period-based 收益序列口径 | ✅ 复用 |
| 30 | [quantstats/reports.py](https://github.com/ranaroussi/quantstats/blob/main/quantstats/reports.py) | `ui/pages/backtest.py`（报告导出） | 复用 `reports.html(returns, benchmark, rf, title, output)` 生成 tear sheet；UI 提供下载按钮直接调用，不自写 HTML 模板 | ✅ 复用 |

### 2.11 stefan-jansen/machine-learning-for-trading（MIT · ✅ 复用）

| # | 来源文件 | 落地模块 | 借鉴内容与用途 | 判定 |
|---|---|---|---|---|
| 31 | [06_strategy_definition/02_cv_foundations.py](https://github.com/stefan-jansen/machine-learning-for-trading/blob/main/06_strategy_definition/02_cv_foundations.py) | `backtest/`（walk-forward，二期） | 复用 walk-forward 切分参数与纪律：WalkForwardCV(n_splits=5, test_size=252, expanding=True, label_horizon=21)、按交易日 purge、加 embargo；输出 OOS 曲线与每折指标 | ✅ 复用 |
| 32 | [16_strategy_simulation/12_dsr_validation.py](https://github.com/stefan-jansen/machine-learning-for-trading/blob/main/16_strategy_simulation/12_dsr_validation.py) | `backtest/`、回测页（防过拟合，二期） | 复用脚本内自实现的 deflated_sharpe_ratio / probabilistic_sharpe_ratio / compute_pbo(CSCV)；**不引入 `ml4t.diagnostic` 依赖**；UI 展示 DSR 概率与 PBO% 红黄绿灯 | ✅ 复用 |

### 2.12 akfamily/akshare（MIT · ✅ 复用）

| # | 来源文件 | 落地模块 | 借鉴内容与用途 | 判定 |
|---|---|---|---|---|
| 33 | [akshare/data/interfaces.json](https://github.com/akfamily/akshare/blob/main/akshare/data/interfaces.json) | `data/provider.py`、数据浏览器 | 复用离线接口注册表字段结构（name/module/category/params/outputs/example），做接口清单供 UI 数据浏览器使用；不分发其 json 原件 | ✅ 复用 |
| 34 | [akshare/request.py](https://github.com/akfamily/akshare/blob/main/akshare/request.py) | `data/provider.py` | 复用指数退避重试 + 错误分类（RateLimitError / NetworkError / DataParsingError），在 AkshareProvider 包装层统一转为 `DataFetchError(source, reason, retryable)`，供降级链判断换源或报错 | ✅ 复用 |

### 2.13 simonlin1212/a-stock-data（Apache-2.0 · ✅ 复用）

| # | 来源文件 | 落地模块 | 借鉴内容与用途 | 判定 |
|---|---|---|---|---|
| 35 | [SKILL.md（数据源优先级 & 东财防封 / 备用源速查 & 降级策略）](https://github.com/simonlin1212/a-stock-data/blob/main/SKILL.md) | `data/provider.py`、`data/cache.py` | 复用主备源降级 + 防封限流：每类数据声明有序源列表 + 每源独立限流器（最小间隔 + 抖动，参考其 1 秒串行）+ 失败自动切源并记录 fallback；绝不并发猛拉 | ✅ 复用 |
| 36 | [SKILL.md（§1.2 成交量对账口径 / V3.10.1 更正）](https://github.com/simonlin1212/a-stock-data/blob/main/SKILL.md) | `data/cache.py`（数据质量校验） | 复用口径校验清单：volume 单位（手 vs 股、科创板特殊）、复权方式（qfq/hfq/不复权不可混）、涨跌停价与昨收一致性；不一致写 warnings 并阻断入库，不静默覆盖 | ✅ 复用 |
| 37 | [SKILL.md（Ticker 格式归一化 / 市场前缀规则）](https://github.com/simonlin1212/a-stock-data/blob/main/SKILL.md) | `data/provider.py` | 复用 ticker 归一化防呆：整串锚定正则（禁用 re.search 捞 6 位）、市场前缀/后缀二选一、`000001` 等歧义码强制带市场标识 | ✅ 复用 |
| 38 | [tests/test_v310_sources.py](https://github.com/simonlin1212/a-stock-data/blob/main/tests/test_v310_sources.py) | `tests/`（数据源回归测试） | 复用端点离线回归测试组织：按数据源/版本分文件，每端点断言字段存在/单位/最少条数，网络全 mock、断网可跑；数据源改版只改对应文件 | ✅ 复用 |

### 2.14 ZhuLinsen/daily_stock_analysis（MIT · ✅ 复用）

| # | 来源文件 | 落地模块 | 借鉴内容与用途 | 判定 |
|---|---|---|---|---|
| 39 | [src/scheduler.py](https://github.com/ZhuLinsen/daily_stock_analysis/blob/main/src/scheduler.py) | `automation/scheduler.py` | 复用三件套：set_daily_task（多时间点）/ add_background_task（≥30 秒轮询）/ GracefulShutdown（信号处理）；我们用 APScheduler 实现同等能力，保留配置热更新后重排 job 与“任务异常不杀主循环” | ✅ 复用 |
| 40 | [src/notification_routing.py](https://github.com/ZhuLinsen/daily_stock_analysis/blob/main/src/notification_routing.py) | `automation/notify.py` | 复用路由表结构：route_type（report/alert/system_error）→（env_key, config_attr）+ 渠道白名单；未配置渠道静默跳过并输出能力清单供 UI 展示 | ✅ 复用 |
| 41 | [src/notification_sender/__init__.py](https://github.com/ZhuLinsen/daily_stock_analysis/blob/main/src/notification_sender/__init__.py) | `automation/notify.py` | 复用“每渠道一个 sender 类”的插件组织方式（统一 `send(title, content) -> bool` 接口），notify 按路由分发；先实现桌面/邮件/Webhook 三种 | ✅ 复用 |
| 42 | [src/schemas/report_schema.py](https://github.com/ZhuLinsen/daily_stock_analysis/blob/main/src/schemas/report_schema.py) | `ui/pages/overview.py`、日报 schema | 复用决策看板字段契约：CoreConclusion / BattlePlan / DataPerspective / PhaseDecision / SignalAttribution，做成前后端共用的 pydantic schema | ✅ 复用 |

---

## 3. 直接依赖（Python 包）

> 版本约束以 `requirements.txt` 与 `docs/SPEC.md` 第 7 节为准；M4-08 将补 `requirements.lock` 记录精确锁定版本。

### 3.1 核心运行依赖

| 包 | 版本约束 | 许可 | 来源 URL | 用途（QuantDesk） | 复用判定 |
|---|---|---|---|---|---|
| streamlit | `>=1.37,<2.0`（BRIEF：1.37） | Apache-2.0 | https://github.com/streamlit/streamlit | Web UI 框架：9 页导航、表单、数据表格、图表容器 | 直接依赖（未改源码） |
| pandas | `>=2.2,<3.0`（BRIEF：2.2） | BSD-3-Clause | https://github.com/pandas-dev/pandas | 数据处理、指标计算、回测与报表 | 直接依赖（未改源码） |
| plotly | `>=5.24,<8.0`（BRIEF 记录 5.24；实装 5.24.1） | MIT | https://github.com/plotly/plotly.py | K 线、权益曲线、回撤图等交互式图表 | 直接依赖（未改源码） |
| SQLAlchemy | `>=2.0.30,<3.0`（实装 2.1.6） | MIT | https://github.com/sqlalchemy/sqlalchemy | ORM、SQLite 引擎/会话、建表与迁移 | 直接依赖（未改源码） |
| pydantic | `>=2.8,<3.0` | MIT | https://github.com/pydantic/pydantic | 配置校验、领域模型、参数 schema 校验 | 直接依赖（未改源码） |
| PyYAML | `>=6.0,<7.0` | MIT | https://github.com/yaml/pyyaml | 解析 `config.yaml` | 直接依赖（未改源码） |
| APScheduler | `>=3.10,<4.0` | MIT | https://github.com/agronholm/apscheduler | 盘前/盘中/盘后定时任务、手动触发 | 直接依赖（未改源码） |
| akshare | `>=1.14` | MIT | https://github.com/akfamily/akshare | A 股日线/指数/基本面/交易日历数据源 | 直接依赖 + 少量设计复用（见 2.12） |
| numpy | `>=1.26,<3.0` | BSD-3-Clause | https://github.com/numpy/numpy | 数值计算（pandas 底层） | 传递依赖（未改源码） |
| requests | `>=2.31,<3.0` | Apache-2.0 | https://github.com/psf/requests | akshare 数据请求、Webhook 通知 | 传递/直接依赖（未改源码） |
| tzdata | `>=2024.1` | Apache-2.0 | https://pypi.org/project/tzdata/ | Windows 下的 IANA 时区数据（`zoneinfo`） | 传递依赖（未改源码） |
| TA-Lib（ta-lib-python） | 可选；建议 `>=0.4.28` | BSD-2-Clause（底层 TA-Lib C 库为 BSD） | https://github.com/TA-Lib/ta-lib-python | 技术指标加速；缺失时用 pandas 同名实现兜底 | 直接依赖 + 设计复用（见 2.9） |

### 3.2 开发 / 可选依赖

| 包 | 版本约束 | 许可 | 来源 URL | 用途 | 缺失时行为 |
|---|---|---|---|---|---|
| pytest | `>=8.0,<10.0` | MIT | https://github.com/pytest-dev/pytest | 单元/集成/端到端测试 | 无法运行测试；不影响启动 |
| ccxt | 可选（`>=4.2`，未列入 requirements） | MIT | https://github.com/ccxt/ccxt | `CcxtBroker` 加密测试网适配 | `is_available()=False`，全流程走 PaperBroker |
| plyer | 可选（`>=2.1`，未列入 requirements） | MIT | https://github.com/kivy/plyer | 桌面通知兜底 | 通知降级为日志 |

### 3.3 本机实测版本（M0-08 记录）

> 采集时间：2026-10-25；方式：`.venv\Scripts\python.exe -m pip list` + 实际 `import` 冒烟。
> 环境：Python 3.12.7（Anaconda 打包版），Windows 10 AMD64。
> 精确锁定版本将在 M4-08 生成 `requirements.lock` 后以 lock 文件为准。

| 包 | 实测版本 | 状态 |
|---|---|---|
| Python | 3.12.7 | 正常 |
| pip | 26.2.2 | 正常 |
| streamlit | 1.37.1 | 正常（符合 `<2.0`） |
| pandas | 2.2.2 | 正常（符合 `>=2.2,<3.0`） |
| numpy | 1.26.4 | 正常（符合 `>=1.26,<3.0`） |
| pydantic | 2.14.1 | 正常（符合 `>=2.8,<3.0`） |
| PyYAML | 6.0.3 | 正常（符合 `>=6.0,<7.0`） |
| APScheduler | 3.11.3 | 正常（符合 `>=3.10,<4.0`） |
| akshare | 1.19.3 | 正常（符合 `>=1.14`） |
| requests | 2.34.2 | 正常（符合 `>=2.31,<3.0`） |
| tzdata | 2026.5 | 正常（符合 `>=2024.1`） |
| pytest | 9.1.1 | 正常（符合 `>=8.0,<10.0`） |
| TA-Lib | 0.8.2 | 正常（可选依赖，已安装） |
| plotly | 5.24.1 | 正常（符合 `>=5.24,<8.0`） |
| SQLAlchemy | 2.1.6 | 正常（符合 `>=2.0.30,<3.0`） |
| ccxt | 未安装 | 可选，按需安装 |

> **说明**：`requirements.txt` 已于 2026-10-25 将 `plotly` 上限放宽至 `<8.0`、`SQLAlchemy` 上限放宽至 `<3.0`，上表实装版本均在区间内；精确锁定仍由 M4-08 的 `requirements.lock` 负责。
> 另：`requirements.txt` 第 20 行注释仍写“本机实测 7.1.0”，而当前实装为 5.24.1，属注释滞后，建议 M0-01 维护者同步更正（不影响安装与运行）。

---

## 4. 复用代码的保留声明与落地位置

| 来源（许可） | 目标文件 | 需保留的声明 |
|---|---|---|
| vnpy/vnpy（MIT） | `core/events.py`、`trading/broker.py`、`trading/order_manager.py` | 文件头注释：`Portions adapted from vnpy/vnpy (MIT License), https://github.com/vnpy/vnpy`，并随仓库保留 MIT LICENSE 文本 |
| pmorissette/bt（MIT） | `strategy/builtin/multi_factor.py`、`backtest/broker_sim.py` | 同上格式，注明 bt 来源与 MIT |
| microsoft/qlib（MIT） | `strategy/`（因子清单）、`data/cache.py` | 注明 Alpha158 因子清单来源；不复制 qlib DSL |
| HKUDS/Vibe-Trading（MIT） | `strategy/`（因子/算子）、`data/provider.py` | 注明版本 v0.1.x 与来源；内部接口可能变动 |
| TA-Lib/ta-lib-python（BSD-2） | `strategy/base.py`（指标兼容层） | 保留 BSD-2 COPYRIGHT 原文与来源 URL |
| ranaroussi/quantstats（Apache-2.0） | `backtest/metrics.py`、回测报告导出 | 保留 LICENSE 与 NOTICE；注明公式来源 |
| akshare（MIT） | `data/provider.py` | 注明接口清单与重试策略来源；数据条款另行遵守 |
| simonlin1212/a-stock-data（Apache-2.0） | `data/provider.py`、`data/cache.py`、`tests/` | 保留 LICENSE 与 NOTICE |
| ZhuLinsen/daily_stock_analysis（MIT） | `automation/scheduler.py`、`automation/notify.py`、日报 schema | 注明来源与 MIT |
| stefan-jansen/machine-learning-for-trading（MIT） | 二期回测/验证模块 | 注明来源；不引入 `ml4t.diagnostic` |
| nautilus_trader（LGPL-3.0） | `core/events.py` 等 | **不保留代码**，仅在设计文档注明“思路参考”；代码为独立重写 |
| lumibot / freqtrade（GPL-3.0） | `strategy/`、`trading/`、`backtest/` | 同上，仅注明思路来源，不复制代码 |
| OpenBB（NOASSERTION） | `data/provider.py` | 仅注明结构参考；在许可核实前不复用代码 |

> 说明：M0 阶段上述目标文件多数尚未创建。本表是**交付时的保留声明要求**，由 Integrator 在 M4 集成评审时逐项核对；缺少声明的复用代码不得合入。

---

## 5. 维护与核查清单

- [ ] 新增任何第三方 Python 包：更新 3.1/3.2 表，并在 `requirements.txt` 中注明版本区间。
- [ ] 新增任何借鉴代码：更新 2.x 明细表，写明来源文件 URL、许可、落地模块、复用/重写判定。
- [ ] 复用 MIT/Apache-2.0/BSD 代码：确认目标文件头有来源与许可注释，仓库保留 LICENSE/NOTICE。
- [ ] GPL/LGPL/AGPL/非商用项目：代码审查确认**零复制**（含注释、命名、结构）。
- [ ] 数据源变更：确认降级链与本地缓存仍可用，遵守上游条款。
- [ ] 发版前：`docs/BORROW_MAP.md`、`docs/borrow_map.json`、本文件三者一致。

---

## 6. 变更记录

| 版本 | 日期 | 变更 | 作者 |
|---|---|---|---|
| v1.0 | 2026-10-25 | 初版：按 `borrow_map.json` 登记 14 个项目 / 42 条来源；补直接依赖许可证；补保留声明与核查清单 | Integrator |
