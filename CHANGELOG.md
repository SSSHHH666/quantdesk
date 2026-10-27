# 变更日志（CHANGELOG）

> 项目：QuantDesk 量化交易工作台 · 仓库：[SSSHHH666/quantdesk](https://github.com/SSSHHH666/quantdesk)
> 格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循 [Semantic Versioning](https://semver.org/lang/zh-CN/)（MAJOR.MINOR.PATCH）。
> 本项目为个人研究用途，**不构成投资建议、不承诺收益**。历史版本记录只增不删。

## [未发布]

- 暂无。

## [2.0.1] - 2026-10-27

**补丁主题**：修复**干净克隆**（从 GitHub 下载发布 tarball、未构建 `runtime/` 数据）下 `tests/test_backtest_passive.py` 因缺数据而报 error 的测试健壮性问题。不涉及生产代码，不改变任何策略/交易/研究结论，M6 负结果不变。

### Fixed

- **干净克隆测试健壮性（`tests/test_backtest_passive.py`）**：module 级 fixture `real_bars` 原先对运行期数据 `runtime/short_term/bars_50.csv` 使用 `assert`；`runtime/` 按 `.gitignore` 不入库，干净克隆下相关用例直接 **error**。现改为**数据缺失时 `pytest.skip(...)`**（构建数据后自动启用），与其余依赖运行期数据的用例行为一致。

### Changed

- **版本号**：`VERSION`、`config.yaml`、`core/config.py`、`core/__init__.py`、UI meta 兜底值、`docs/SPEC.md` 配置示例统一为 `2.0.1`（仅字符串常量，无逻辑变更）。
- 已发布的 `v2.0.0` tag **不移动、不删除**；本补丁以新提交/新 tag `v2.0.1` 发布。

### 验证

- **v2.0.0 tarball 干净目录（修复前）**：`383 passed / 17 skipped / 5 errors`，errors 全部在 `tests/test_backtest_passive.py`（缺 `runtime/` 数据）。
- **修复后 main 干净目录**：全量收集 **405 项、0 error**（依赖运行期数据的用例按 `skip` 处理）；最终数字见 [`docs/RELEASE_VERIFICATION_v2.0.0.md`](docs/RELEASE_VERIFICATION_v2.0.0.md) §9 复核记录。

### Security / Compliance

- M6 研究结论不变（**无可执行短线选股边际**，不接入推荐/自动化，UI 维持研究阶段）；不承诺收益（UBA）。

## [2.0.0] - 2026-10-27

**主题**：Phase 2（M6「策略研究增值」）研究框架并入主仓，并把仓库正式版本化为 **v2.0.0**。
本版本的 M6 结论是**负结果**：未找到可执行的短线选股边际，因此**不接入推荐/自动化**，UI 维持「研究阶段」。

### Added

- **M6 研究框架**（`strategy/m6_research.py`、`scripts/m6_research.py`）：面板/特征/标签构建、严格 walk-forward（purge/embargo）、36 组实验矩阵、bootstrap / 多重比较 / 分层检验、gate 判定、可执行性检查与产出落盘。
- **200 只 A 股日线样本**（`scripts/build_universe_m6.py`、`data/universe_200.json`）：200 只（沪主板 56 / 深主板 56 / 创业板 48 / 科创板 40）、2019-01-02~2026-10-23、**349,466 行**、不复权 OHLCV + 后复权因子；source 全为 akshare、**零 mock**；覆盖审计 **13/13 PASS**。
- **M6 研究报告与审计文档**：`docs/M6_RESEARCH_REPORT.md`（v1.0，36 组 OOS 矩阵、统计检验、可执行性结论）、`docs/M6_QA_AUDIT.md`（v1.0，独立审计与 F1–F9 证据）、`docs/M6_HANDOFF_PROMPT.md`（接续提示词，已加状态更新）。
- **P1 同仓位被动基准**：`backtest/validation.py` + `scripts/run_backtest.py` 把回测报告标准输出统一为「**同仓位被动基准 + 样本外标记**」；新增 `tests/test_backtest_passive.py`（6 用例）。
- **测试**：新增 `tests/test_m6_research.py`（21 用例：截断不变性、复权标签、purge/embargo、可复现、gate、数据来源、三分类命中率）与 `tests/test_backtest_passive.py`（6 用例）；全量测试基线 378 → **405 passed / 60 deselected**。
- **发布物料**：`VERSION`、`CHANGELOG.md`（本文件）、`docs/RELEASE_NOTES_v2.0.0.md`、`docs/M6_INTEGRATION.md`。

### Changed

- **回测/撮合价格口径统一**为不复权真实价（`backtest.price_adjust=none`）：回测 CLI（`scripts/run_backtest.py`）与回测页的撮合、手数、资金与被动基准一律用不复权真实价；复权价仅用于研究标签与价格类特征（研究口径，不进入默认交易链路）。
- **README/文档版本统一**为 v2.0.0：README 徽章（Version 2.0.0 / Tests 405 passed）与「当前阶段」更新，新增「M6 研究成果（v2.0 新增）」小节；`docs/DOC_INDEX.md` 升 v1.10；`docs/REVIEW_FINAL.md` v1.1、`docs/SHORT_TERM_MODEL_V1.md` §7、`docs/M6_HANDOFF_PROMPT.md` 已同步 M6 进展。
- **版本常量链路统一**：`config.yaml`、`core/config.py`、`core/__init__.py` 与 UI meta 兜底值由 `1.0.0` 统一为 `2.0.0`（仅字符串常量，无逻辑变更）。
- **发布口径**：推荐/筛选页面继续标注「研究阶段 · 不构成买入建议 · 不承诺收益」。

### Fixed

M6 QA 独立审计发现并闭环的缺陷（F1–F8 为代码缺陷，F9 为结论层面问题；详见 `docs/M6_QA_AUDIT.md` §3）：

- **F1 标签未复权（高）**：`future_ret_h` 由不复权 close 改为复权总收益价 `adjclose = close × adj_factor`。修复前 h=5 有 1,752/82,723 行与金标准不符（最大偏差 54.9%），修复后 0 不符。
- **F2 折元数据缺失（中）**：`M6Fold` 暴露 `train_label_max / train_date_max / embargo_date / purge_days`，使 purge/embargo 可断言；15/15 折通过。
- **F3 gate 口径错误（高）**：不再把 Top 绝对收益当超额，改用 `top_excess_vs_passive` + 合并样本二项 p。
- **F4 持有期错配（高）**：强制「持有期 = 标签 horizon」、非重叠再平衡、按换手扣费；`ret10` 不再按 5 日重叠持有。
- **F5 市场状态 bfill（潜在）**：市场列改为 ffill-only + 中性兜底，避免把未来指数值回填到早期日期。
- **F6 分折统计口径（高）**：折内统计由「整折 top-k」改为「逐日 top-k 再按日聚合」，与日度选股/组合口径一致。
- **F7 被动基准标的集合（低）**：按 `predictions` 标的集合过滤，修复多 1 只（688072.SH）问题。
- **F8 三分类命中率（高）**：命中率由 `label.sum()` 改为 `(label==1.0).sum()`，消除 -1 抵消（曾把 hit 从 38.2% 变成 2.6%）。
- **F9 gate 缺可成交性条件（高·结论层面）**：研究框架暂未把「涨停/停牌不可买」纳入 gate（框架增强留待下一阶段）；本轮在**结论层面**修正：唯一 gate=True 的 `gap_up_1d × gbdt_rank` 判定 `tradable=false`，不得写为可用/可接入。

### Security / Compliance

- **不承诺收益（UBA）**：全项目与文档统一口径「不构成投资建议、不承诺收益、回测/模拟结果不代表未来表现」。
- **研究阶段不接自动化**：M6 未通过预注册门槛的配置**不接入** `strategy/recommend.py` 与自动化链路；`automation.enabled` 默认关闭。
- **数据合规**：M6 数据全部来自 akshare（Sina 通道），零 mock、零合成；Mock 数据不入库；回测撮合用不复权真实价。
- **本地单用户**：默认只监听 localhost；密钥只从环境变量（`QD_` 前缀）读取，不写入配置/日志。
- **可复现与审计留痕**：walk-forward 折元数据、gate 判定、bootstrap/多重比较结果与 QA 审计证据落盘。

### Known limitations

- **M6 研究结论为负结果**：34/36 组未过预注册门槛；唯一统计显著的是 `gap_up_1d × gbdt_rank`（RankIC 0.146、CI [0.138, 0.154]、多重比较 p=0.001），但入选票 **17.4%** 在 t 日收盘已封涨停（买不进），剔除后扣费净超额 **−0.07%~−0.09%/日** → **不可成交 artifact；无可执行短线选股边际；不接入推荐/自动化，UI 维持研究阶段**。
- **数据偏差**：股票池来自 2026-10-25 当日仍存活、仍活跃的股票，存在**存活者偏差**与「事后选池」；单一免费数据源（akshare/Sina），无资金流/龙虎榜/行业/分钟线。
- **运行期产物不入库**：`runtime/`（含 `bars_200.csv`、`m6_*` 结果、数据库、日志）按 `.gitignore` 排除；复现需重新构建数据（约 15 分钟，依赖网络）。
- **本地仓库无 git**：本版本化产物由 GitHub API 提交（无本地 tag/commit 历史），发布后以 GitHub tag/release 为准。
- **历史占位项**：设置页部分保存类接口、审计/日志查询、策略实例创建仍为占位（不影响 v2.0.0 的研究与模拟盘使用）；`docs/TEST_REPORT.md` 仍待 QA 补交。

## [1.0.0] - 2026-10-26

MVP（M0–M5）交付基线（历史摘要，非本次发布新增）：

### Added

- **数据层**：akshare 真实行情 + 本地 SQLite 缓存 + 断网 Mock 降级；复权、数据质量校验、数据源健康检查。
- **回测层**：事件驱动回测引擎、A 股撮合规则与费用模型、收益/回撤/夏普/胜率等指标、`price_adjust` 口径。
- **交易层**：唯一风控闸门（RK001–RK020）、`OrderService` 唯一订单入口、PaperBroker 模拟盘、持仓/资金曲线。
- **自动化**：APScheduler 调度、盘前/盘中/盘后任务、桌面/邮件/Webhook 通知，默认关闭。
- **12 页 UI**：总览 / 行情 / 手动交易 / 今日推荐 / 全市场筛选 / 自选池 / 策略 / 回测 / 持仓 / 自动化 / 设置 / 日志。
- **交付文档**：README、RUNBOOK、USER_GUIDE、DEMO_WALKTHROUGH、ACCEPTANCE_CHECKLIST、REVIEW_FINAL 等；一键启动/停止、环境自检、备份恢复、发版自检。
- **测试**：377 passed / 60 deselected / 1 xfailed（M0–M5 阶段基线）。

### Known limitations

- 无已验证可盈利策略：短线模型 v1（`usable=False`）与全市场筛选器（RankIC 为负）均为负结果；不接真实资金。

---

## 版本对照

| 版本 | 日期 | 定位 |
|---|---|---|
| 2.0.1 | 2026-10-27 | 补丁：干净克隆（未构建 runtime/ 数据）下测试 error → skip；仅测试健壮性 + 版本号，无 API/数据/研究结论变化 |
| 2.0.0 | 2026-10-27 | Phase 2（M6）研究框架并入 + 正式版本化；M6 结论为负结果（无可执行短线选股边际） |
| 1.0.0 | 2026-10-26 | MVP（M0–M5）交付基线（历史摘要） |
