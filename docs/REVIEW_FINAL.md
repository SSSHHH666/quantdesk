# QuantDesk 项目最终状态评审（REVIEW_FINAL）

> 版本：v1.0 · 日期：2026-10-26 · 评审人：PM（pm-quant）
> 评审对象：**M0–M5 交付终态 + M6 研究阶段现状**（截至 2026-10-26）
> 依据：`BRIEF.md` v0.3、`SPEC.md` v1.7、`TASKS.md` v1.9、`ROADMAP.md` v1.5、`REVIEW_M0_R1.md` v1.5、`REVIEW_M1.md` v1.3、`ACCEPTANCE_CHECKLIST.md` v2.0、`docs/SCREENER_VALIDATION.md`、`docs/DATA_QUALITY_NOTES.md`、`docs/USER_GUIDE.md`、`docs/DEMO_WALKTHROUGH.md`
> 说明：本报告只做**状态与证据登记**，不修改代码；命令均在 `quantdesk/` 目录、`.venv` 下执行。

---

## 0. 一句话结论

> **MVP（M0–M5）功能交付基本完成（M0–M4 通过，M5 有条件通过），可作“研究 + 模拟盘工作台”使用；但 M6 研究阶段（短线模型 v2 / 全市场筛选）因样本外验证未通过而暂停，系统当前没有任何“已验证可盈利”的策略。**
> 完成度判断：**功能交付 ≈ 90%（M0–M4 全通、M5 部分占位）；研究目标 0/2 达标（短线可盈利、筛选有效均未通过）。**
> 上线口径：**只可当研究/模拟盘工具**；不得用于真实资金自动交易；不承诺收益（UBA）。

---

## 1. 证据与命令（可复现）

| 项 | 命令 / 证据 | 结果 |
|---|---|---|
| 全量测试 | `.venv\Scripts\python.exe -m pytest -q` | **377 passed, 60 deselected, 1 xfailed**（45–50s） |
| 发版自检 | `.venv\Scripts\python.exe scripts\release_check.py` | **PASS 11 / WARN 0 / FAIL 0（通过）** |
| 数据层表数 | `.venv\Scripts\python.exe -c "from sqlalchemy import inspect; from core.db import get_engine; from core.models import ALL_TABLE_NAMES; t=set(inspect(get_engine()).get_table_names()); b=t-{'schema_version'}; print(len(b), len(ALL_TABLE_NAMES)); assert len(b)==28 and set(ALL_TABLE_NAMES)<=t"` | **28 / 28**（22 核心 + 6 扩展；另有 `schema_version`） |
| 真实数据 | `runtime/exports/data_coverage.md`、`daily_bars` | 3 标的 × 1,212 行（2021-10-25~2026-10-23），source 全 akshare，零 mock；R6 迁移后 **3,636 行真实日线无损** |
| 真实回测基线 | `REVIEW_M1.md` §2（`scripts/run_backtest.py` / 回测页） | 2025-01-02~2026-10-23、100 万本金、3 标的、单票 10%、不复权：ma_cross -0.11%、value_quality -2.74%、breakout_abnormal -2.63%、ml_ranker -1.53%；同仓位被动 **-1.98%** |
| M2 下单实测 | 手动交易页 + `tests/test_paper_broker` 等价用例（`pytest tests/ -q -k "paper or risk"`） | 买入→持仓→T+1 拒卖→次日卖→对账，全流程可用 |
| M3 调度器 | 自动化页启停 + `runtime/automation_smoke.lock`；`pytest -q -k automation` | 3 job 启停/手动触发/非交易日 SKIPPED 可用 |
| M4 备份恢复 | `.venv\Scripts\python.exe scripts\backup_db.py`、`scripts\restore_db.py`；`pytest tests/test_m4_delivery.py -q` | 备份生成有效快照、恢复回读一致、恢复前自动备份 |
| M4 一键启动 | `scripts/start.bat` / `scripts/check_env.py` | 可用（release_check 校验文档/脚本齐备） |

> 注：测试数由早期 379 演进为 **377 passed + 1 xfailed**（xfaith 为已知 P1 契约缺口：`Recommendation.as_dict()` 的 `name` 字段，见 §5）。

---

## 2. 里程碑完成度（M0–M5 + M6）

| 里程碑 | 状态 | 结论与证据 |
|---|---|---|
| **M0 骨架** | ✅ **已退出** | 启停/建库/日志/事件/配置可用；`pytest` 骨架绿；`REVIEW_M0_R1.md` C1–C6 全通（有条件通过，遗留项已关闭） |
| **M1 研究闭环** | ✅ **功能验收通过（有效性未证明）** | 真实 akshare 数据、4 策略 + ML 报告、379→377 测试；同仓位被动对照仅 ma_cross（+1.87pp）/ml_ranker（+0.45pp）略好，其余跑输；**无显著 alpha**（`REVIEW_M1.md` §2/§4） |
| **M2 交易闭环** | ✅ **通过** | 手动买入/卖出/撤单/T+1 拦截/风控 RK 码/持仓与现金对账全通（`REVIEW_M0_R1.md`、`ACCEPTANCE_CHECKLIST.md` v2.0 M2 段） |
| **M3 自动化** | ✅ **通过** | 调度器 3 job 启停 + 手动触发 + trace 链路 + 通知/日志可用；默认关闭（`automation.enabled: false`） |
| **M4 交付** | ✅ **通过** | 断网 Mock 冒烟、`start.bat`、RUNBOOK/THIRD_PARTY、备份/恢复、发版自检 **PASS 11/0/0** |
| **M5 打磨** | 🟡 **有条件通过** | 12 页 UI 全部落地（含 `screener.py`）、报告导出/CSV、USER_GUIDE/DEMO_WALKTHROUGH；但设置页保存类、审计/日志查询、策略实例等仍为“占位待接” |
| **M6 研究阶段** | 🟡 **本轮完成（2026-10-26 恢复后 P0-1…P0-5）** | 扩样本到 **200 只 / 349,466 行**；36 组严格 walk-forward OOS（6 标签×2 特征集×3 模型）完成；**34/36 未过门槛**，2 组通过但经可执行性检验判定不可交易（涨停封板买不进，剔除后扣费净超额为负）→ **仍无可执行短线选股边际**；详见 `docs/M6_RESEARCH_REPORT.md` / `docs/M6_QA_AUDIT.md` |

---

## 3. 已验证的能力（可直接演示/使用）

1. **数据层**：akshare 真实数据落库（不复权 + `adj_factor`）、增量幂等、数据质量校验（DQ001–DQ007）、Mock 离线兜底；**28 张业务表**（22 核心冻结 + 6 扩展新增，不改已有表）；`schema_version=v2` 迁移（v1 库自动补 6 表 + 默认自选池分组，迁移前备份）。
2. **研究/回测**：4 策略 + ML 排序、walk-forward 框架、分组回测、参数稳健性；回测与绩效指标（含 `RQ_WARN_*` 警告）；**不复权价格撮合**（`backtest.price_adjust`，非法值回退 `none` + 中文报错，不阻断启动）。
3. **交易闭环**：PaperBroker 模拟盘；手动买/卖/撤；T+1、涨跌停、整手（主板 100 / 科创 200）、费用（佣金/印花/过户/规费/滑点）；风控闸门 20 个 RK 码；审计与 `trace_id`。
4. **自动化**：APScheduler 盘前/盘中/盘后 3 job，启停/手动触发/非交易日跳过；通知至少日志渠道可用。
5. **UI**：12 页全部可打开；行情/回测/自选池/推荐可用；策略/持仓/自动化/设置/日志页有空态与中文引导（部分接口待接）。
6. **交付物**：`start.bat` 一键启动、`check_env.py` 环境自检、`release_check.py` 发版自检、RUNBOOK/USER_GUIDE/DEMO_WALKTHROUGH/THIRD_PARTY/GLOSSARY、备份/恢复脚本。

---

## 4. 明确未完成 / 研究阶段项（不得对外宣称“已实现/有效”）

| # | 项 | 状态 | 原因/说明 |
|---|---|---|---|
| 1 | **短线模型 v1** | ❌ 不可用（`usable=False`） | 48 折 walk-forward OOS 命中率 24.83%≈基础概率、扣费期望 -0.22%、MDD -32.41%；**无短线 alpha**（`REVIEW_M1.md` §9；实验细节 `SHORT_TERM_MODEL_V1.md`，含 v2 实验框架同样负结果） |
| 2 | **短线模型 v2（M6-13）** | ⏸ 暂停 | 需扩样本 30–50 只、换标签/中性化、市场状态特征、严格 purge（M6 恢复后 P0） |
| 3 | **全市场筛选 F14/F15** | ⏸ 暂停（研究阶段） | 50 只样本 5/10/20 日 RankIC 均负（-0.029/-0.030/-0.037），Top 组超额≈0 → 未过 F15 门槛（`SCREENER_VALIDATION.md`）；UI 标“研究阶段/不构成买入建议” |
| 4 | **设置页保存类接口** | 🟡 占位待接 | 风控参数保存、通知配置保存等仍提示“接口待接入”（设置页仅展示与备份/恢复可用） |
| 5 | **审计/日志查询接口** | 🟡 部分 | 日志页过滤/审计 Tab 部分可用，分页/查询接口待接 |
| 6 | **策略页实例创建/参数校验** | 🟡 占位待接 | 策略注册表/实例写入接口未接入（M3-05 后续） |
| 7 | **持仓/自动化部分交互** | 🟡 部分 | 清仓/重置/立即运行等按钮走真实接口或提示待接，视当前版本 |
| 8 | **G1 从零安装计时 / G5 连续 5 交易日** | ☐ 未执行 | 需干净环境与连续交易日模拟，本轮未做（不阻塞研究/模拟使用，但不得宣称“已验收”） |
| 9 | **已知 P1 契约缺口（xfail）** | 🟡 记录 | `Recommendation.as_dict()` 的 `name` 字段（字符串 "None"/缺失时 UI 占位）——`tests/test_recommend_contract.py::test_recommendation_name_is_nullable_but_never_string_none` 标记 xfail，待修 |
| 10 | **真实资金/实盘** | ❌ 不提供 | 只有 PaperBroker + Ccxt/Qmt 骨架；真实下单需用户自备券商柜台（QMT 等），本版不提供 |

---

## 5. 上线使用须知与风险声明（用户必读）

1. **定位**：本系统是**个人研究 + 模拟盘工具**，不是投资顾问、不代客理财、**不承诺收益**；历史回测与样本外表现不代表未来收益；**据此交易风险自担（UBA）**。
2. **不得用于真实资金自动交易**：推荐/筛选/自动化均默认不自动下单；`automation.enabled` 默认关闭；`risk.allow_auto_orders` 默认 false（AUTO 单被 RK020 拒绝）。
3. **数据限制**：当前真实数据仅 3 只标的（2021-10 ~ 2026-10）；akshare 免费接口可能缺失/延迟/改版；非交易时段不能下单（风控保护，非故障）。
4. **研究阶段标记**：短线雷达/全市场筛选/未过样本外的策略一律显示“研究阶段/不构成买入建议”；**未通过 F15 验证前不得宣称有效**。
5. **可恢复性**：升级/迁移前自动备份到 `runtime/backups/`；恢复请用 `scripts/restore_db.py`（恢复前自动再备份）。
6. **配置口径**：撮合/手数/资金核算用**不复权真实价**（`backtest.price_adjust: none` 默认）；`hfq` 仅供研究对照（自动告警 + 关闭最小手数校验）。
7. **故障排查**：先看 `docs/RUNBOOK.md`；日志 `runtime/logs/app-*.log`（JSON Lines + trace_id）；`release_check.py` 可一键体检。

---

## 6. 最终判断

| 维度 | 判断 |
|---|---|
| 功能交付（MVP） | ✅ **M0–M4 通过；M5 有条件通过**（12 页落地，部分页面接口占位） |
| 可演示性 | ✅ 可完整演示“拉数据 → 推荐 → 回测 → 模拟下单 → 自动化 → 日志/审计”；断网 Mock 可演示 |
| 研究目标（盈利） | ❌ **未达成**：短线 v1 不可用、筛选 RankIC 为负、无策略显示显著 alpha |
| 合规 | ✅ 不承诺收益/UBA/推荐不自动下单/研究阶段标记已落地；未过验证不得宣称有效 |
| 建议 | **以“研究 + 模拟盘”定位交付**；M6 研究暂停，待用户决定后按 ROADMAP §8.5 恢复；先修 P1 契约缺口 + 补齐设置/审计接口再谈“盈利用户故事” |

---

## 7. 最终交付清单（文件级）

| 类别 | 交付物 | 状态 |
|---|---|---|
| 代码（core） | `core/{config,models,db,logging,events}.py` | ✅ 28 张表 ORM + `schema_version` v2 迁移 |
| 代码（data） | `data/{provider,cache}.py`、`data/watchlist.py`、`data/universe_50.json` | ✅ 真实 akshare + Mock 兜底 + 自选池数据层 |
| 代码（strategy） | `strategy/{base,factors,recommend,ml_ranker,short_term}.py`、`strategy/builtin/*` | ✅ 注册表 + 两榜 + ML + 短线原型（研究阶段） |
| 代码（backtest） | `backtest/{engine,broker_sim,metrics,validation}.py` | ✅ 事件驱动回测 + A 股规则 + walk-forward 框架 |
| 代码（trading） | `trading/{broker,paper,order_manager,risk,portfolio}.py` | ✅ 唯一风控入口 + PaperBroker 全流程 |
| 代码（automation） | `automation/{scheduler,jobs,notify}.py` | ✅ 3 job 启停/触发 |
| 代码（UI） | `app.py`、`ui/pages/*.py`（**12 页**）、`ui/pages/_common.py` | ✅ 12 页落地（策略/设置/日志部分占位，见 `DOC_INDEX` §3.5） |
| 脚本 | `scripts/{start.bat,check_env,init_db,fetch_data,run_backtest,backup_db,restore_db,release_check,validate_screener,purge_mock_cache,build_universe}.py` | ✅ 一键启动/自检/备份恢复/发版自检/筛选验证 |
| 测试 | `tests/`（22 个文件，含 UI/风险/回测/数据/交付验收） | ✅ **377 passed, 60 deselected, 1 xfailed** |
| 文档 | BRIEF/SPEC/TASKS/ARCHITECTURE/ROADMAP/DOC_INDEX/GLOSSARY/RUNBOOK/USER_GUIDE/DEMO_WALKTHROUGH/THIRD_PARTY/BORROW_MAP/ACCEPTANCE/REVIEW_M0_R1/REVIEW_M1/**REVIEW_FINAL**/DATA_SOURCES/DATA_QUALITY_NOTES/STRATEGY_RESEARCH/SCREENER_VALIDATION/SHORT_TERM_MODEL_V1 | ✅ 已交付（`TEST_REPORT.md` 待 QA 补交） |
| 配置 | `config.yaml`（含 `backtest.price_adjust`、`screen.*`）、`requirements.txt` | ✅ 默认安全（自动交易关闭、筛选关闭） |
| 运行期 | `runtime/{quantdesk.db,logs,backups,exports}` | ✅ 已生成；迁移前自动备份 |

---

## 8. 变更记录

| 版本 | 日期 | 变更 | 评审人 |
|---|---|---|---|
| v1.0 | 2026-10-26 | 初版：M0–M5 完成度与证据（377 passed/60 deselected/1 xfailed、release_check PASS 11/0/0、28 表校验、真实回测基线、M2/M3/M4 实测）、已验证能力、未完成/研究阶段清单（短线 v1 不可用、F14/F15 暂停、设置/审计占位、G1/G5 未执行、P1 xfail）、最终交付清单、上线须知与风险声明、最终判断 | pm-quant |
| v1.1 | 2026-10-26 | **M6 恢复后本轮进展（P0-1…P0-5 + P1）**：①数据扩样 50→200 只（349,466 行、13/13 覆盖审计 PASS、零 mock）；②研究框架加固（复权总收益标签、持有期=horizon、严格 gate、bootstrap/多重比较、purge/embargo 折元数据）；③36 组 walk-forward OOS 矩阵完成（34/36 未过门槛；2 组 PASS 均隔夜缺口，但涨停封板不可成交、剔除后扣费净超额为负）；④结论：**仍无可执行短线选股边际**，不接入推荐/UI，维持研究阶段；⑤P1：回测报告强制“同仓位被动基准+样本外标记”；⑥QA 独立审计（截断不变性/复权标签/purge-embargo/可复现/gate/bars_200 数据）PASS，闭环 F1–F8；⑦测试 405 passed。详见 `docs/M6_RESEARCH_REPORT.md`、`docs/M6_QA_AUDIT.md` | Lead（研究负责人） |

---

## 9. M6 研究阶段进展（2026-10-26 恢复后 · 本轮）

> 详细实验与判定见 `docs/M6_RESEARCH_REPORT.md`；独立审计见 `docs/M6_QA_AUDIT.md`。

| 项 | 状态与证据 |
|---|---|
| P0-1 扩样本与数据 | ✅ 200 只 A 股（沪主板 56/深主板 56/创业板 48/科创板 40）、349,466 行、2019-01-02~2026-10-23、不复权 OHLCV + hfq 复权因子、source 全 akshare、零 mock；覆盖审计 13/13 PASS（`runtime/exports/universe_coverage_200.md`）；分年截面 2019 中位 147 只→2026 年 200 只 |
| P0-2 标签工程 | ✅ 6 种标签（3/5/10 日≥2%、5 日截面 Top20%、5 日三分类、次日跳空），全部复权总收益口径；样本量/正样本率/不平衡处理落盘 |
| P0-3 特征工程 | ✅ 32 特征（18 量价 + 14 市场状态/相对强弱/涨跌停邻近/连续放量/跳空/ATR/换手/量价背离）；价格类用复权价、涨跌停距离用不复权；as_of 对齐经截断不变性验证（32 特征逐元素 0 差异） |
| P0-4 模型与验证 | ✅ logistic/gbdt/gbdt_rank × 2 特征集 × 6 标签 = 36 组；76–77 折滚动 walk-forward、purge=max(5,h)、embargo=5；OOS 28.6–28.8 万样本；**34/36 未过门槛**；2 组 PASS 为隔夜缺口，经可执行性检验（收盘涨停不可买）剔除后扣费净超额 −0.074%/日 → 不可执行 |
| P0-5 结论与产出 | ✅ `docs/M6_RESEARCH_REPORT.md` 落盘；**无通过项 → 不接入 recommend/自动化、UI 维持研究阶段** |
| P1 复用 | ✅ 回测报告强制输出“同仓位被动基准 + 样本外标记”（`scripts/run_backtest.py`、`backtest/validation.py`、`tests/test_backtest_passive.py`）；实测 ma_cross −0.11% vs 被动 −2.40%（与 REVIEW_M1 一致） |
| QA 独立审计 | ✅ 无未来函数/复权标签/purge-embargo/可复现/被动基准/gate/数据来源/性能 全 PASS；闭环 F1–F8；`tests/test_m6_research.py` 21 用例；全量 **405 passed / 60 deselected** |
| 已知 P1 契约缺口（xfail） | 🟡 仍在：`Recommendation.as_dict()` 的 `name` 字段（不影响 M6 研究结论） |

**一句话**：M6 研究流程本轮完整跑通、数据与审计达标、结论仍为**无可执行短线选股边际**；系统仍只可作“研究 + 模拟盘”工具，不得宣称有效、不得接入自动化。
