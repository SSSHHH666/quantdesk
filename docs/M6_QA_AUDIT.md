# M6 QA 独立审计报告（M6_QA_AUDIT）

> **审计角色**：QA engineer（独立审计，不写策略代码、不信任原作者注释）
> **审计对象（最终冻结版）**：`strategy/m6_research.py` = `sha256:A95E7F34B601…`（77,776 B，23:19:58）；
> `scripts/m6_research.py` = `sha256:E4EB203430CD…`（37,920 B，23:35:10）；矩阵数据 `bars_200.csv` = `sha256:EA35970C…`。
> **审计结论**：**框架/数据/复现性 PASS**；**36 组矩阵中唯一 gate=True 的 `gap_up_1d + gbdt_rank`
> 经 QA 可执行性专项复核判定为「不可成交 artifact」**（统计信号真实、无前视，但超额来自收盘涨停无法买入的票；
> 剔除后扣费净超额为负）→ 报告不得将其写为“可用/可接入”。
> 核心三项（无未来函数 / purge-embargo / 可复现）与 gate 门槛行为均有可复现断言；
> 审计过程中发现并闭环修复 8 项代码缺陷（F1–F8）+ 1 项结论层面问题（F9=gate 缺可成交性条件）；
> 残余风险见 §6。
> 最终复核：`tests/test_m6_research.py` **21 passed**；全量 `tests` **405 passed / 60 deselected**。

---

## 0. 结论摘要

| # | 审计项 | 结论 | 关键证据（可复现） |
|---|---|---|---|
| 1 | 无未来函数（特征层） | **PASS** | bars 截断到 2024-06-28 重建面板：共同 54,743 行 × 32 个 `FEATURES_ALL` 逐元素 0 差异（atol 1e-9）；index 同步截断亦然 |
| 2 | 标签正确性（复权） | **PASS** | `future_ret_h = adjclose.shift(-h)/adjclose-1`（`adjclose=close×adj_factor`）：h=1/3/5/10 分别 82,645/82,545/82,445/82,195 行，0 不符、max_abs=0.0 |
| 3 | 标签日 / 尾部 | **PASS** | `label_date_h == date.shift(-h)` 0 不符；每标的最后 h 行 `future_ret/label_*` 全 NaN、`label_date` 全 NaT（0 违规） |
| 4 | purge / embargo | **PASS** | 15/15 折：`train_label_max < test_start` 且 `train_date_max ≤ 测试开始前第 5 个交易日` |
| 5 | 可复现性 | **PASS** | 同配置两次运行 58 项 metrics 标量 0 差异、folds 元数据逐字段相等 |
| 6 | 被动基准对齐 | **PASS** | `passive.start/end == OOS 首末预测日`；`symbols=45 == OOS 预测标的数 45`（修复前 46，见 F7） |
| 7 | gate_check 行为 | **PASS** | `usable ≡ all(criteria)`；多项不达标同时列出；"绝对收益>0 但基准超额<0" → usable=False |
| 8 | 数据来源 | **PASS** | bars_50：89,000 行 / 50 标的 / source 全 `akshare` / 无 mock / adj_factor 无缺失 / 无重复 |
| 9 | 性能守卫 | **PASS** | `build_m6_panel(bars_50)=3.3–4.7s` ≪ 120s |
| 10 | bars_200 全量抽查 | **PASS** | 200 只 / 349,466 行；无重复、source 全 akshare、adj_factor 无缺失、与 universe_200 一致、0 坏行；40 只最小截面抽样截断不变性 0 差异 |
| 11 | 矩阵合并一致性 | **PASS** | `m6_matrix.csv` 36 行 = s1/s2/s3 各 12 行并集；同配置核心指标与分片、JSON 逐字段 0 差异（1e-6） |
| 12 | best 配置可执行性 | **FAIL→结论修正** | `gap_up_1d/gbdt_rank`（唯一/唯二 gate=True）：Top5 中 17.4% 收盘涨停不可买；剔除后扣费净超额 **-0.076%**（可买池）/ -0.094%（全池），命中率 21.54%≈基础 21.53% → **不可成交 artifact**（详见 §12） |
| 13 | best 配置可复现 | **PASS** | `runtime/qa/replay_best.py` 重跑 best：15/15 核心指标与 `m6_experiments.json` 完全一致（rank_ic 0.146079、hit 0.286071、base 0.215279、excess_net 0.002310…） |

**质量保障章节可直接引用的一句结论**：M6 研究框架在特征/标签/purge-embargo/被动基准/gate 五个层面
通过独立 QA 审计（截断不变性、复权金标准、折元数据、二次运行一致性），未发现未来函数；
审计期间发现的分折统计口径 bug 曾使分折 t 由 +1.63 翻转为 −0.36，修复后结论口径一致，
最终统计显著性仍须以 `gate_check` 与多重比较校正结果为准。

---

## 1. 审计范围与方法

### 1.1 范围

| 文件 | 审计内容 | 快照 |
|---|---|---|
| `strategy/m6_research.py` | 面板/特征/标签/walk-forward/purge-embargo/gate/被动基准/统计口径 | A95E7F34…（23:19 冻结版） |
| `strategy/short_term_v2.py` | 静态前视扫描（legacy v2 研究模块，M6 复用其 `_symbol_raw_features`/`sector_of`） | 71E6FE16… |
| `strategy/factors.py` | `normalize_bars` + 因子函数是否存在未来数据 | 744A172F… |
| `strategy/screener_validation.py` | 静态前视扫描（历史基线结论来源） | E4B73AB0… |
| `runtime/short_term/bars_50.csv` | 数据来源/复权因子完整性/重复行 | 3C126647…（89,000 行） |
| `runtime/m6/index_000300.csv` | 市场状态数据覆盖（2018-06-01~2026-10-23，2,036 行） | C16AB50F… |

### 1.2 方法（独立、可复现）

1. **金标准复算**：不复用被测代码的标签/特征产物，直接用原始 CSV（`close×adj_factor`）重算
   `future_ret_h`、`label_date_h`、截面 Top20% 标签，再与面板逐元素比对；特征层用"截断重建"做
   **不变性**检验（任何使用 T 之后信息的特征都会破坏它）。
2. **结构不变量**：fold 元数据（`train_label_max/train_date_max/embargo_date/test_start`）
   与独立重算的交易日历对照；两次运行结果逐字段比较。
3. **探针**：`runtime/qa/probe_core.py`、`probe_v2.py`、`probe_v3.py`（只读，不改策略代码）。
4. **静态逐行扫描**：对 4 个文件中全部 `shift(-n)` / `future` / `label` / `rolling` / `ffill/bfill` /
   截面 `rank/quantile` 用法分类核对（§5）。
5. **回归测试**：`tests/test_m6_research.py`（20 个用例，其中 2 个 bars_200 条件用例）。

---

## 2. 逐项结论与证据

### 2.1 截断不变性（无未来函数金标准）— PASS

- 全量面板：`82,695 行 × 93 列`，50 标的，2019-07-11~2026-10-23，`price_basis=adjclose`，
  `adj_factor_filled=0`（无静默兜底）。
- 把 `bars_50.csv` 截断到 `T=2024-06-28` 重建面板：date≤T 行数 54,743，共同 (date,symbol) 54,743
  （**一行不少**）。
- 32 个 `FEATURES_ALL` 特征逐元素比较：**0 差异**（含把 `index_000300.csv` 同步截断的严格版本）。
- 反向证据：若把指数序列起点人为改到晚于面板起点（2019-08），旧版 `bfill` 会让 2019-07 的
  `market_ret_20` 取到未来值（实测 -0.0698）；该路径已按 F5 修复为 ffill-only。

### 2.2 标签正确性（复权总收益）— PASS

金标准：`adjclose = close × adj_factor`；`future_ret_h = adjclose.shift(-h)/adjclose − 1`。

| h | 比对行数 | 不符行数 | max_abs_diff |
|---|---|---|---|
| 1 | 82,645 | 0 | 0.0 |
| 3 | 82,545 | 0 | 0.0 |
| 5 | 82,445 | 0 | 0.0 |
| 10 | 82,195 | 0 | 0.0 |

- `label_date_h == date.shift(-h)`：全量 0 不符；NaN/NaT 位置与金标准一致。
- **复权因子口径说明**：`bars_200.csv` 的 `adj_factor` 实际为 **hfq-factor（后复权因子）**
  （task 文字曾写 qfq-factor；`build_report_200.json` 记为 `adjust='hfq-factor'`）。
  标签公式 `future_ret_h = (close×factor).shift(-h)/(close×factor) − 1` 与因子是 hfq 还是 qfq 无关，
  QA 对 bars_50 全量 + bars_200 抽样均以 `close×adj_factor` 独立复算，0 不符。
  hfq 因子以历史为锚、不随新除权重算，理论上更接近 point-in-time；但仍无法本地验证数据源是否
  回溯修订历史因子（见 §6 残余风险 1）。
- **标签尾部**：每只标的最后 h 行 `future_ret_h` 与 `label_*` 全为 NaN、`label_date` 全为 NaT（0 违规）
  ——证明没有用"数据之外的未来"凑标签。
- 分类标签：`ret3/5/10_ge2`、`gap_up_1d`（`next_gap≥0.5%`）、`three_class_5d`（±2% 分档）、
  `cs_top20_5d`（逐日截面分位 Top20%）与金标准逐元素一致；截面 Top 标签中位数占比 = 20.0%。
- **修复前对照（F1）**：旧版用不复权 `close`，h=1/3/5/10 分别 353/1,054/1,752/3,478 行与复权金标准
  不符，最大偏差 50.7%/49.5%/54.9%/65.6%（除权日），会系统性污染标签与 IC。

### 2.3 purge / embargo — PASS

配置：`ret10_ge2 / logistic / train_window=120 / test_window=40`，样本 2019-07-11~2022-06-30，15 折。

- 每折：`max(train label_date) < test_start`（purge）且 `max(train date) ≤ 测试开始前第 5 个交易日`
  （embargo，`embargo_date` 与独立交易日历重算一致）；15/15 全通过。
- 例（fold 1）：`train_end=2019-12-19`，`train_label_max=2020-01-03 < test_start=2020-01-06`，
  `embargo_date=2019-12-27`；`purge_days=10 = max(5,horizon)`。
- 折测试窗口互不重叠（不同折日期集合无交集）；预测日期严格落在 `[test_start, test_end]`。

### 2.4 可复现性 — PASS

同配置连续两次 `run_m6_config`：`rank_ic/hit_rate/base_rate/hit_rate_lift/net_expectancy/
top_excess_vs_passive/long_short_spread/folds/oos_samples` 与被动基准 total_return 全部相等；
folds 元数据（含 `train_label_max`、`top_features` 排序）逐字段一致。

### 2.5 被动基准对齐 — PASS

- `passive.start = 首个 OOS 预测日`、`passive.end = 最后一个 OOS 预测日`，与 `metrics.oos_start/oos_end` 一致。
- `passive.symbols = 45 = OOS 内实际出现过的预测标的数`；`window_matched.holding_days = label horizon`。
- **修复前（F7）**：旧版按"窗口内 bars 出现过的标的"统计（46 只），多出 `688072.SH`
  （该标的 bars 在 OOS 窗口有 2022-04~06 数据，但面板 2022-10 才满足特征历史）——属于
  "未来才上市标的混入被动基准"，已按 `predictions` 标的集合过滤修复。

### 2.6 gate_check 行为 — PASS

用构造的假 `M6Result.metrics` 验证 6 种组合：

| 构造场景 | usable | 未过项 |
|---|---|---|
| 三项达标 | True | — |
| RankIC=0.01、基准超额=-0.02、命中不显著 | False | `rank_ic_gt_0.03`、`top_excess_gt_0`、`hit_rate_significant`（同时列出） |
| RankIC=0.05、**绝对收益 +3%**、**基准超额 -2%** | False | `top_excess_gt_0`（不再把绝对收益当超额） |
| 仅 RankIC=0.02 | False | `rank_ic_gt_0.03` |
| 仅基准超额=-0.1% | False | `top_excess_gt_0` |
| 仅命中 p=0.4 | False | `hit_rate_significant` |

- 全部场景 `usable == all(criteria.values())` 严格等价；`failed` 列表与 `criteria` 一致；
  `note` 写清全部未过项名称。
- 口径：命中率 p 为**合并样本二项检验**（非重叠再平衡窗口，偏保守）并另给分折 t；
  "超额"= Top 组扣费后相对**同期全池等权 h 日收益**的差（`top_excess_vs_passive`）。

### 2.7 数据来源 — PASS

`bars_50.csv`：89,000 行、50 标的、2019-01-02~2026-10-23；`source` 唯一值 `{akshare}`、
无 `mock` 标记；`(date,symbol)` 无重复；`adj_factor` 无缺失；`normalize_bars` 可直接摄入并保留
`adj_factor`（M6 复权依赖）。指数 `index_000300.csv` 覆盖 2018-06-01 起（早于面板起点，市场特征无前向填充需求）。

### 2.8 性能守卫 — PASS

`build_m6_panel(bars_50)` 实测 3.5–4.7s（含面板 rank/标签/市场合并），远低于 120s 预算；
测试中作为硬断言记录实际耗时。

### 2.9 bars_200 全量抽查 — PASS

`runtime/short_term/bars_200.csv` 全量产物（`build_report_200.json`：candidates=280、picked=200、
耗时 918.2s）经独立抽查：

| 检查项 | 结果 |
|---|---|
| 标的数 / 总行数 | 200 只 / **349,466 行**（Σ 每标的行数一致，行数自洽） |
| 区间 | 2019-01-02 ~ 2026-10-23 |
| 每标的行数 | 最少 831 行（要求 ≥800） |
| 末根 bar | 全部 ≥2026-10-16（实际最小 2026-10-23） |
| `source` | 唯一值 `{akshare}`，无 mock |
| `(date,symbol)` 重复 | 0 |
| `adj_factor` 缺失 | 0（实际因子口径为 **hfq_factor**，见 §2.2） |
| 与 `data/universe_200.json` | 标的集合完全一致（200/200） |
| OHLCV 完整性 | 无 OHLC≤0、无 high<low、无 high<close、无负量/负额（0 坏行） |
| 5 只抽样截断不变性 | PASS（因 `MIN_CROSS_SECTION=40`，抽样集取最小可建截面 40 只，其中前 5 只重点核对；32 特征 0 差异、复权标签 0 不符） |

> 数据侧历史：data-engineer 覆盖率审计曾发现 `688143.SH 2024-11-06` 一条停牌坏行
> （OHLC=0/volume=0，11/13 FAIL）；本轮复核时该行已修复，13/13 PASS
> （`runtime/exports/universe_coverage_200.md`）。QA 的行完整性断言已固化，坏行回归会直接失败。

---

## 3. 发现的问题与修复闭环

审计期间对 `strategy/m6_research.py` 的 22:40 版与两次重写版本分别复核；所有缺陷已由
modeling-engineer 修复，并由 QA 独立复测通过（不采信"已修复"声明，只看复现结果）。

| ID | 严重度 | 问题（证据） | 位置（修复前） | 修复 | 复测 |
|---|---|---|---|---|---|
| F1 | 高 | 标签用不复权 close：h=5 有 1,752/82,723 行与复权金标准不符，max 偏差 54.9% | `future_ret_h=close.shift(-h)/close-1` | 改为 `adjclose=close×adj_factor` 口径，保留 raw 对照 | 0 不符 |
| F2 | 中 | `M6Fold` 未暴露 `train_label_max`，QA 无法断言 purge | 旧 M6Fold 字段 | 新增 `train_label_max/train_date_max/train_label_min/embargo_date/purge_days` | 15/15 折通过 |
| F3 | 高 | gate 把 Top 绝对收益当"超额"；命中率 p 用分折 t 而非合并样本二项 | 旧 `gate_check` | 改用 `top_excess_vs_passive` + 合并样本二项 p；多项未过逐项列出 | §2.6 全通过 |
| F4 | 高 | 持有期固定 5 日，ret10 标签按 5 日重叠持有 | 旧 `portfolio_holding` | 强制 `holding=label horizon`，非重叠再平衡、按换手扣费 | `holding_days=10` 断言通过 |
| F5 | 低（潜在） | 市场列 `ffill().bfill()`：指数起点晚于行情时会把未来值回填到早期日期（探针复现 -0.0698） | 旧 `build_m6_panel` 市场填充 | 改 ffill-only + 中性兜底（0/0.5），并在面板/测试中断言无 bfill | 截断不变性通过 |
| F6 | 高 | 分折 hit/base/net_expectancy/long_short 用"整折 top5"（~1000 stock-day 里挑 5 个）而非逐日 top5；分折 t 符号翻转（见 §4） | 旧 `run_m6_config` 折内统计 | 新增 `_fold_stats`：逐日 top-k 再按日聚合，与 `_daily_stats` 同口径 | folds 逐字段回归测试通过 |
| F7 | 低 | 被动基准标的集合=窗口内 bars 出现过的标的，多 1 只（688072.SH，panel 2022-10 才有） | 旧 `passive_oos_benchmark` | 按 `predictions` 标的集合过滤 | symbols=45==45 |
| F8 | 高 | 三分类标签命中率按 `label` **求和**（-1 抵消）：`three_class_5d` 的 hit 从 38.2% 变成 2.6%，影响 hit_lift/二项 p/gate 命中率门槛；矩阵 s3 分片首跑即命中 | `_daily_stats` 的 `top_hits = label.sum()` | 改 `(label==1.0).sum()` 计数；s3 分片 kill 后用修后代码重跑 | QA 新增 `test_three_class_hit_rate_is_count_based` 独立计数回归；bars_50 复测 hit=38.19%/base=36.39% |
| F9 | 高（结论层面） | **gate 缺可成交性条件**：36 组中 gate=True 的 `gap_up_1d/gbdt_rank`，Top5 有 17.4% 是 t 日收盘涨停（不可买入），剔除后扣费净超额 -0.076%/-0.094%、命中率≈基础率 → 报告若直接引用“gate=True”会误判为可用 | `gate_check` / `_simulate_topk` / `passive_oos_benchmark` 均假设收盘价可成交，无涨停/停牌约束 | 未改策略代码（研究框架增强留待下一阶段）；**结论层面修正**：报告标注 tradable=false，不得接入推荐 | QA 独立复核（§12、`runtime/qa/gap_audit.md`）+ modeler 自查（17.41%、-0.074%）+ task-7 复核中 |
| L1 | 低（潜在） | `amount_pct = amount.rank(pct=True)` 为**全样本时间序列分位**（非按日截面）——当前未被 `_weighted_score` 使用，未触发泄漏 | `screener_validation.py:132`、`screener.py:490` | 未改（不属 M6 核心路径，Lead 指示仅记录） | 建议后续改按日截面 rank 或删除 |
| L2 | 低（legacy） | `short_term_v2.py` 标签用不复权 close，且 `walk_forward_v2` 只有 purge 无 embargo | legacy v2 研究模块 | 未改（M6 已取代，不进入默认流程） | 仅记录，M6 不依赖 |

---

## 4. 分折口径 bug 实证：实现 bug 会改变统计结论（Lead 要求写入）

同一份数据（`ret10_ge2 / logistic / v1 / 2019-07~2022-06`，15–16 折），仅把折内统计口径从
"整折 top5"改为"逐日 top5 后按日聚合"：

| 口径 | 命中率 | 命中率提升 | 分折 t（metrics.hit_rate_t） |
|---|---|---|---|
| 旧：整折 top5（bug） | 0.3750 | **-0.0235** | **-0.3577** |
| 新：逐日 top5 再按日聚合 | 0.4272 | **+0.0287** | **+1.631** |

- 方向与显著性同时反转：旧口径会给出"分折命中率跑输基础概率"的结论，新口径为"小幅跑赢但远未显著"。
- 根因：整折 top5 只从约 1,000 个 stock-day 中取 5 个样本，与日度选股/组合口径不一致，噪声极大；
  且 `net_expectancy`、`long_short_spread` 同样受影响。
- **启示**：最终报告不能只看单折 t 或单一口径指标；QA 的回归用例
  `test_fold_stats_use_per_date_topk` 已把该口径锁死（逐日 top-k 聚合，误差 ≤1e-6）。
- **同类实证（F8）**：三分类命中率用 `label.sum()` 会被 -1 抵消，同一配置 hit 从 38.2% 变 2.6%
  （base 36.4%），会直接改变"命中率是否显著高于基础概率"的门槛判定。QA 已加
  `test_three_class_hit_rate_is_count_based` 用独立计数复算锁死。两例都说明：
  **实现层面的口径 bug 可以单独制造"显著/不显著"的结论，必须由独立审计复算**。

---

## 5. 静态前视审计（逐文件逐类）

扫描目标：所有 `shift(-n)`、`future`、`label`、`rolling`、`ffill/bfill`、截面 `rank/quantile` 用法。

### 5.1 `strategy/m6_research.py` — PASS

| 类别 | 出现位置 | 用途 | 判定 |
|---|---|---|---|
| `shift(-n)` | L387/388（future_ret/label_date）、L390/391/393（raw 对照/next_gap） | **仅标签/标签日与对照列** | PASS：不进入特征；`label_date` 仅用于 purge |
| `future` | L387 future_ret_*；IC/命中率评估用 `target_return` | 仅监督目标与评估 | PASS：`FEATURES_ALL` 无 future/label 列（有测试断言） |
| `label` | label_* / label_date_*；训练集过滤 `label_date < test_start` | 监督 + purge | PASS：训练只用 `label_date<test_start`，测试期只预测 |
| `rolling` | L395 `adj_high.rolling(250,min_periods=120).max()`；市场 20/60/250 日 | 全部 trailing 窗口 | PASS：只用 ≤t 数据 |
| 截面 `rank/quantile` | `_rank_normalize`（按 date 分组 rank）；标签 `cs_top20_5d`（按 date 分组 rank） | 当日截面 | PASS：无全样本分位 |
| `ffill/bfill` | 市场列 ffill-only（L453）；被动基准 closes ffill（L1440） | 停牌/节假日前值填充 | PASS：无 bfill；不复用未来 |
| 其他 | `_fold_feature_subset`（只统计训练集方差剔除近零特征）；`gate_check` 用基准超额；`feature_rank_ic_table`（全样本单因子描述性 IC） | — | PASS：`feature_rank_ic_table` 仅用于评估/描述（future_ret 仅作评估目标，不进入训练/特征）；但它是**全样本描述性统计**，不能替代 walk-forward OOS，报告引用时须注明 |

### 5.2 `strategy/short_term_v2.py`（legacy v2，M6 复用特征函数）— PASS（带 legacy 限制）

- `shift(-n)`：L376-378 仅构造 `future_return_h/label_date/next_gap`（标签），不进入特征。
- 特征：`_symbol_raw_features` 全部 trailing（`shift(1)`、`rolling(min_periods)`）；`_per_date_norm`
  按 `date` 分组做截面 rank/z-score → 无未来。
- 市场/板块聚合按 `date` → 同日截面，无未来。
- **限制（L2）**：标签用不复权 close（除权日污染）；`walk_forward_v2` 仅 purge 无 embargo。
  属 legacy 研究模块，M6 已用 `m6_research.py` 取代，不进入默认推荐/交易链路。

### 5.3 `strategy/factors.py` — PASS

- 无 `shift(-n)`；`normalize_bars` 只做列规范化/去重/排序，保留 `adj_factor`。
- 因子函数为"截至最后一根 bar 的 trailing 窗口"；`zscore_map`/`value_quality_components` 对
  **同一时点截面**做 z-score，不参与历史回测的特征构造 → 无未来数据。

### 5.4 `strategy/screener_validation.py`（历史基线来源）— PASS（带潜在泄漏记录）

- 打分：每个交易日 `day_frame` 内截面 z-score（`_weighted_score`），只用当日及以前特征 → 无未来。
- `shift(-n)`：L142 仅 `forward_h` 标签，用于评估（IC/分组收益/Top 超额）→ 合规。
- **潜在泄漏（L1）**：L132 `amount_pct = amount.rank(pct=True)` 为全样本时间序列分位；
  当前未被 `_weighted_score` 使用（未触发），标记"潜在泄漏/建议后续修"。
- 另：历史基线用不复权价（文档已声明为旧口径），不影响 M6 复权结论；对比时需注明口径差异。

---

## 6. 残余风险与限制

1. **复权因子的时点性**：`adj_factor` 实际为 akshare **hfq-factor**（后复权）。对任一标签窗口
   `[t, t+h]`，若数据源对历史因子做过非均匀修订，标签会出现不可检测的轻微漂移；本地无法构造
   point-in-time 因子快照，故列为残余风险。缓解：复权标签用 `close×adj_factor` 独立金标准复算
   （bars_50 全量 + bars_200 抽样均 0 不符）+ 截断不变性通过 + `label_summary` 报告复权/不复权均值差。
2. **存活者偏差 / 大盘股偏差**：标的池按 2026 年流动性与上市年限筛选（task-1/历史文档已声明），
   会抬高绝对收益；判定只应以基准超额/RankIC/gate 为准。
3. **bars_200 数据已抽查 PASS**（350k 行、0 坏行、universe 一致）；但"200 只全样本矩阵结论"仍由
   modeling-engineer 的 OOS 输出与 gate 决定，QA 只背书数据与框架口径，不背书收益结论。
4. **统计显著性与多重比较**：QA 验证的是"口径/机制正确"，不对 36 组配置的最终有效性背书；
   报告必须同时引用 `gate_check`、合并样本二项 p、block bootstrap CI 与 max-statistic 多重比较。
5. **gate 缺可成交性条件（F9）**：`gate_check`/组合模拟假设收盘价可成交，未剔除收盘涨停/停牌。
   本报告的最佳配置 `gap_up_1d/gbdt_rank` 即被该口径判定为“统计 PASS、不可交易”
   （详见 §12）；若未来接入推荐，必须先把可成交性纳入 gate 与回测约束。
6. **legacy 模块限制（L2）**：`short_term_v2.py` 的复权/embargo 缺口仅记录，不在 M6 路径上；
   若未来复用必须迁移到 M6 口径。
7. **并发编辑**：审计期间他人仍在改 `strategy/*`、`scripts/*`。本报告结论绑定 §1.1/§11 的最终冻结哈希；
   文件变更后须重跑 `tests/test_m6_research.py` 复核。

---

## 7. 测试资产与用例清单

### 7.1 `tests/test_m6_research.py`（21 用例，全部 PASS）

| # | 用例 | 覆盖 |
|---|---|---|
| 1 | `test_panel_build_performance_and_contract` | 性能<120s、面板形状、复权口径、adj_factor 无兜底 |
| 2 | `test_feature_list_has_no_future_or_label_columns` | FEATURES_ALL 不含未来/标签列 |
| 3 | `test_truncation_invariance_all_features` | 截断不变性（bars-only / bars+index） |
| 4 | `test_truncated_panel_loses_no_common_rows` | date≤T 行不丢失 |
| 5 | `test_future_returns_match_adjusted_close` | 复权标签 + label_date 金标准 |
| 6 | `test_label_tail_is_nan` | 尾部无标签 |
| 7 | `test_label_columns_follow_returns` | 6 种标签定义 + 截面 Top 占比 |
| 8 | `test_label_summary_matches_panel` | 标签矩阵样本量/正样本率 |
| 9 | `test_fold_purge_and_embargo` | purge/embargo 逐折断言 |
| 10 | `test_fold_test_windows_are_out_of_sample` | 测试窗口 OOS/不重叠 |
| 11 | `test_fold_stats_use_per_date_topk` | 分折统计逐日 top-k（F6 回归） |
| 12 | `test_same_config_is_reproducible` | 二次运行一致 |
| 13 | `test_three_class_hit_rate_is_count_based` | 三分类命中率按 label==1 计数（F8 回归） |
| 14 | `test_passive_benchmark_aligned_with_oos` | 被动基准起止/标的集合/持有期 |
| 15 | `test_gate_check_usable_iff_all_criteria_passed` | usable≡criteria、多项未过 |
| 16 | `test_gate_check_reports_multiple_failed_items` | 多项同时列出 |
| 17 | `test_gate_check_rejects_positive_absolute_but_negative_excess` | 绝对收益≠超额 |
| 18 | `test_gate_check_accepts_passing_fake_result` | 达标→usable=True |
| 19 | `test_bars_50_source_is_akshare_and_ingestible` | 数据来源/复权因子/可摄入 |
| 20 | `test_bars_200_quality_audit` | bars_200 全量抽查（行数/重复/akshare/adj_factor/universe/OHLCV 坏行） |
| 21 | `test_bars_200_sample_truncation_invariance` | 200 只抽样（最小截面 40 只，重点 5 只）截断不变性 + hfq 复权标签 |

### 7.2 探针（`runtime/qa/`，只读）

- `probe_core.py`：初版框架的标签/前视/bfill 探针（历史证据）。
- `probe_v2.py`：当前版全项复测（截断/标签/purge/repro/passive/gate）。
- `probe_v3.py`：被动基准标的集合与分折口径对照（F6/F7 证据）。
- `gap_audit.py` / `gap_audit2.py` / `best_audit.py`：best 配置（gap_up_1d）可执行性专项
  （对齐/前视排查、涨停不可买、价格分层），产物 `gap_audit.md` + `gap_predictions.csv`。
- `matrix_consistency.py`：36 组矩阵分片 vs merge vs JSON 一致性断言。
- `replay_best.py`：best 配置同 panel 复跑，比对 metrics/gate/folds 元数据。

---

## 8. 运行命令与结果

```powershell
cd C:\Users\shanghao\Documents\deepseek-harness\default-workspace\quantdesk

# 1) M6 QA 回归（21 用例，含 bars_200 全量数据审计与三分类命中率回归）
.venv\Scripts\python.exe runtime\_run_pytest.py tests\test_m6_research.py -q
#   -> 21 passed（约 35–50s，视 CPU 竞争）

# 2) 全量回归（最终冻结版）
.venv\Scripts\python.exe runtime\_run_pytest.py tests -q
#   -> 405 passed, 60 deselected in 56.09s（矩阵完成后无竞争；基线 41.64s）
#   基线（新增测试前）：378 passed, 60 deselected in 41.64s

# 3) 探针/专项复算（证据落盘）
.venv\Scripts\python.exe runtime\qa\probe_v2.py
.venv\Scripts\python.exe runtime\qa\gap_audit.py      # best/异常配置可成交性（复核 F9）
.venv\Scripts\python.exe runtime\qa\matrix_consistency.py  # 矩阵合并一致性
.venv\Scripts\python.exe runtime\qa\replay_best.py    # best 配置复跑（15/15 metrics PASS）
```

| 项目 | 结果 |
|---|---|
| 单文件测试 | **21 passed / 0 skipped**（约 35–50s） |
| 全量回归 | **405 passed / 0 skipped / 60 deselected / 56.09s**（≥ 基线 378；含本任务新增 21 用例） |
| `build_m6_panel(bars_50 / bars_200)` | 3.5–4.7s / ~10–16s |
| 截断不变性 | bars_50：54,743 行 × 32 特征 0 差异；bars_200 抽样（40 只）同 |
| 复权标签 | bars_50 全量 0 不符（h=1/3/5/10）；bars_200 抽样 0 不符；best 预测 target_return 与独立重建 next_gap 0 不符 |
| purge/embargo | 15/15 折通过（bars_50 子样本）；200 只全量矩阵 77 折元数据由 replay 复核通过 |
| bars_200 数据 | 200 只 / 349,466 行 / 0 坏行 / universe 一致 |
| 矩阵一致性 | 36 行 = 3 分片并集；分片/merge/JSON 核心指标 0 差异 |
| best replay | 15/15 核心指标与 JSON 完全一致 |
| best 可执行性 | Top5 涨停 17.39%；剔除后扣费净超额 -0.0756%（可买池）→ **不可成交 artifact** |

> 环境说明：本机 DSH 沙箱禁止管道，sklearn `HistGradientBoosting` 的 joblib 线程池会报
> `PermissionError: WinError 5`。测试文件顶部已 `os.environ.setdefault("OMP_NUM_THREADS","1")`，
> 框架侧亦有同样保护；该问题属环境限制，不是框架缺陷（当前 QA 用例均用 logistic）。
>
> 说明：全量回归数字中除本任务新增 21 个用例，还包含其他成员并行新增的
> `tests/test_backtest_passive.py`（6 用例，P1 task-6）与其回归结果；本报告只对 M6 相关用例负责。

---

## 9. 写范围合规

- 本任务仅写入：`tests/test_m6_research.py`、`docs/M6_QA_AUDIT.md`、`runtime/qa/*`（探针）。
- 未修改 `strategy/*`、`scripts/*`、`data/*`、`runtime/m6/*` 及他人负责文件；
  发现的缺陷全部通过 `send_message` 通知 modeling-engineer + Lead 由其修复。

---

## 10. 复现清单

1. 读数据：`runtime/short_term/bars_50.csv`（89,000 行，source 全 akshare）。
2. 建面板：`build_m6_panel(bars, index_bars=load_index_bars())`（3.5–4.7s；bars_200 全量约 15.8s）。
3. 跑测试：命令见 §8（必须用 `runtime/_run_pytest.py` 包装器，规避本机沙箱 tmp 目录 0700 问题）。
4. `bars_200.csv` 可用（349,466 行）；200 只全矩阵由 modeling-engineer 产出，QA 用
   `runtime/qa/replay_best.py` 抽 1 个最佳配置复跑核对（同 panel/同配置，比对 metrics/gate/folds 元数据）。
5. 复算证据：`runtime/qa/probe_v2.py`（截断/标签/purge/repro/passive/gate）。
6. 矩阵跑完后：核对 `m6_experiments.json` 的 `data.bars_sha256` / `code_sha256`（复现元数据）。

---

## 11. 复审记录（版本漂移 / F8 / 矩阵分片代码版本）

> 本节记录审计过程中 `strategy/m6_research.py` 与 `scripts/m6_research.py` 的版本漂移及复测结果，
> 用于说明"审计结论绑定哪个代码版本"，避免报告引用过期结论。

| 时间（2026-10-26） | `strategy/m6_research.py` | `scripts/m6_research.py` | 事件/复测 |
|---|---|---|---|
| 22:40 | 39,583 B（初版） | 11,239 B | QA 首轮发现 F1–F5（标签未复权/gate 口径/bfill 等） |
| 22:54 / 22:59 | 67,796 → 71,171 B | 重写 | F1–F5 修复；QA 复审发现 F6（分折整折 top5）、F7（被动标的集合） |
| 23:14:56 | 74,680 B，sha `20148186…` | 23:15:10 | F6/F7 修复；QA 20 用例 + 全量 404 passed |
| 23:19:58 | 77,776 B，sha `A95E7F34…` | B988F1(23:21:45) | **F8**：three_class 命中率由 label.sum() 改为 (label==1).sum()；QA 新增 3 分类回归，21 用例 |
| merge 完成 | **A95E7F34…（77,776 B，23:19:58，最终冻结）** | **E4EB2034…（37,920 B，23:35:10，最终冻结）** | 矩阵完成：36 组矩阵 + 合并产物落盘，`final_verdict.tradable=false`；QA 复核见下 |

**最终冻结版核对（merge 后）**：

1. **代码指纹已闭环**：`m6_experiments.json → data.code_sha256` 已写入 strategy（`a95e7f34…`，77,776 B）
   与 scripts（`e4eb2034…`，37,920 B）；`data.bars_sha256 = ea35970c…`；`data.shard_code_versions`
   记录 s1/s2=A421EE（F8 前）、s3=A95E7F（F8 后）并附等价性说明。§11 原“缺 code_sha256”缺口已修复。
2. **矩阵合并一致性 PASS**（`runtime/qa/matrix_consistency.py`）：`m6_matrix.csv` 36 行 =
   `shards/s1..s3/m6_matrix.csv` 各 12 行的并集（36/36 unique、full cartesian 36/36）；
   同配置核心指标“分片原值 vs merge vs JSON matrix”逐字段 **0 差异（1e-6）**。
3. **best 配置 replay PASS**（`runtime/qa/replay_best.py`，独立重建 325,385 行面板后重跑
   77 折 gbdt_rank）：15/15 核心指标与 `m6_experiments.json → best.metrics` 完全一致
   （rank_ic 0.146079 / hit 0.286071 / base 0.215279 / net_expectancy 0.002033 /
   top_excess_net 0.002310 / folds 77 / oos 2020-07-13~2026-10-22）。
4. **F8 影响面与等价性**：F8 仅影响 `three_class_5d` 的 hit 类指标（hit_rate/hit_rate_lift/
   hit_rate_binom_p/gate 命中率门槛），不影响 rank_ic / top_excess / 组合收益；修复前后同一配置
   hit=-0.025428 → 0.337549、p=None → 0.869831。s1/s2 在修复前启动但标签均为 0/1
   （sum==count 等价），QA 用旧码产物 `ret3_ge2|v1|logistic` 在当前码复跑 **15/15 metrics 一致**。
5. **审计绑定**：最终代码 = A95E7F34 / E4EB2034；若后续再改，须重跑 `tests/test_m6_research.py`
   并更新本报告。审计时冻结态：`tests/test_m6_research.py` **21 passed**、全量 **405 passed / 60 deselected**。

---

## 12. best 配置（gap_up_1d）可执行性专项复核

> 36 组矩阵中 gate=True 的配置均为 `gap_up_1d + gbdt_rank`（v1/all）。Lead 要求先证伪。
> 完整报告：`runtime/qa/gap_audit.md`；复算脚本：`gap_audit.py` / `gap_audit2.py` / `best_audit.py`。

**结论：不可成交 artifact（非前视泄漏、非统计假象，但不可交易）。**

| 检查 | 结果 |
|---|---|
| 前视/对齐 | `target_return` 与独立重建 next_gap 逐行 0 差异（287,654 行，maxdiff 1e-16）；label_date 0 mismatch；训练目标确为 next_gap 截面分位 |
| 独立重算 | scipy/pandas 独立实现与报告值 ≤1e-6（rank_ic 0.146079、hit 0.286071、base 0.215279、excess_net 0.002310）→ 非指标实现 bug |
| 其他证伪 | 剔除极端 gap（\|gap\|>10%/20%）IC 不变；价格中性化后 IC=0.1346；分年 IC 0.126–0.168 → 统计信号真实稳定 |
| **可成交性（决定性）** | Top5 中 **17.39%** 为 t 日收盘涨停（全样本 1.04%，17 倍富集；44.4% 交易日至少 1 只）；涨停票次日 gap **+1.78%** vs 可买票 +0.12% |
| 剔除不可买后 | 扣费净超额 **-0.0756%/窗口**（vs 可买池，t=10.6）/ **-0.0935%**（vs 全池）；命中率 **21.54% vs 基础 21.12%**（lift 仅 +0.41pp）→ 不可交易 |
| v1 配置 | Top5 涨停 12.81%，剔除后扣费净超额 **-0.126%/-0.144%**，命中率 19.46% < 基础 21.18% |

**原因**：`gate_check` 与组合模拟假设“t 日收盘价可成交”，未建模“收盘涨停不可买”。该配置的超额
几乎全部来自收盘封涨停、次日高开（+1.78%）的票；剔除后扣除 23bp 成本为负。与 modeling-engineer
独立自查（17.41%、-0.074%）一致。**报告结论：不得将该 gate=True 写为“可用/可接入”**；后续若继续研究，
需在 gate 与回测中加入涨停/停牌可成交性约束（F9）。

---

## 13. 复审记录（末尾更新）

- 2026-10-27 00:15 左右：矩阵 36 组完成、merge 产物齐备；QA 复核 matrix 一致性（PASS）与 best replay（PASS）；
  结论：框架/数据/复现性 PASS，**最佳配置判定为不可成交 artifact（tradable=false）**。
- 本报告所有数字均为最终冻结版（strategy A95E7F34 / scripts E4EB2034 / bars_200 EA35970C）实测。
