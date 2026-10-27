# M6 研究成果并入说明（M6_INTEGRATION）

> 版本 v1.0 · 日期 2026-10-27 · 维护：发布工程（release-engineer）/ 研究负责人（Lead）
> 用途：说明 **M6「策略研究增值」阶段（Phase 2）的成果已如何并入项目1（QuantDesk 主仓）**，包括来源、已入库文件、复现方式、结论与合规边界。
> 一句话：**M6 已并入项目1 的 `main` 分支（基线 `b5c575a`）**——研究框架、200 只样本清单、研究报告、QA 独立审计、P1 被动基准与回归测试都已入库；**结论是负结果，不改变产品默认链路**。

---

## 1. M6 与项目1 的关系

| 项 | 说明 |
|---|---|
| **项目1** | QuantDesk 量化交易工作台（MVP：M0–M5）——数据层 / 回测 / 交易 / 自动化 / 12 页 UI / 交付文档 |
| **M6（Phase 2）** | 「策略研究增值」阶段：在 MVP 之上回答「**日频免费数据能否做出可执行的短线选股/自动交易边际**」 |
| **关系** | M6 是**研究阶段**，不是新产品功能。成果以「可复现实验框架 + 研究报告 + 独立 QA 审计 + 回归测试」的形式并入主仓；未通过验证的配置**不进入**推荐 / 自动化 / UI 有效性标注 |
| **范围依据** | `BRIEF.md` v0.3（F10–F15）、`docs/ROADMAP.md` §8.5（M6 恢复条件）、`docs/SPEC.md` v1.7（F14/F15）、`docs/TASKS.md` v1.9（M6-01…M6-22） |

> 结论先行：**M6 已完成、可复现、审计通过，但没有找到可执行的短线选股边际**（详见 §5）。因此项目1 的产品链路与 v1 行为保持不变：自动化默认关闭、推荐/筛选固定标注「研究阶段」。

---

## 2. 来源工作流（本轮如何产出并并入）

1. **恢复 M6**：按 `docs/ROADMAP.md` §8.5 的恢复条件启动，先修历史缺陷（复权标签、持有期错配、分折统计口径、gate 口径）。
2. **数据构建**：`scripts/build_universe_m6.py` 从全市场快照选 280 只候选 → **200 只** A 股，拉取 2019-01-02~2026-10-23 日线（349,466 行，零 mock），产出 `data/universe_200.json` 与 `runtime/short_term/bars_200.csv`；覆盖审计 **13/13 PASS**。
3. **研究实验**：`strategy/m6_research.py` + `scripts/m6_research.py` 构建 32 特征、6 标签，跑 6 标签 × 2 特征集 × 3 模型 = **36 组**严格 walk-forward OOS（76–77 折、purge=max(5,h)、embargo=5）。
4. **独立验证**：QA 独立审计（截断不变性 / 复权标签 / purge-embargo / 可复现 / gate / 数据审计；闭环 F1–F8 + F9 结论修正）；backtest-engineer 独立数值复核（`runtime/verify/m6_verify.json`，`all_ok=true`）；Lead 自算；**四路证据一致**。
5. **并入主仓**：研究报告、QA 审计、P1 被动基准、回归测试提交到 `main`；`docs/REVIEW_FINAL.md` v1.1 §9 与 `docs/SHORT_TERM_MODEL_V1.md` §7 登记进展；v2.0.0 发布物料完成版本化。

---

## 3. 已并入项目1 的文件清单

| 类别 | 文件 | 作用 | 是否默认链路 |
|---|---|---|---|
| 研究代码 | `strategy/m6_research.py` | 面板/特征/标签、walk-forward、purge/embargo、gate、统计与可执行性工具 | ❌ 否（仅被研究脚本/测试调用） |
| 研究 CLI | `scripts/m6_research.py` | 36 组矩阵、分片合并、统计检验、产出落盘 | ❌ 否（手动运行） |
| 数据构建 | `scripts/build_universe_m6.py` | 200 只样本构建 + 覆盖审计 | ❌ 否（手动运行） |
| 样本清单 | `data/universe_200.json` | 200 只 A 股清单（代码/名称/板块） | ❌ 否 |
| 回测口径（P1） | `backtest/validation.py`、`scripts/run_backtest.py` | 同仓位被动基准 + 样本外标记 + 标准报告输出 | ✅ 是（回测报告标准输出） |
| 回归测试 | `tests/test_m6_research.py` | 21 用例：截断不变性、复权标签、purge/embargo、可复现、gate、数据来源、三分类命中率 | ✅ 是（全量测试） |
| 回归测试 | `tests/test_backtest_passive.py` | 6 用例：被动基准口径与对齐 | ✅ 是（全量测试） |
| 研究报告 | `docs/M6_RESEARCH_REPORT.md`（v1.0） | 36 组 OOS 矩阵、统计可信度、可执行性检验、结论判定 | — |
| QA 审计 | `docs/M6_QA_AUDIT.md`（v1.0） | 独立审计方法、逐项结论、F1–F9 证据与闭环 | — |
| 接续提示词 | `docs/M6_HANDOFF_PROMPT.md` | 下一轮研究（换数据/微观结构/时点股票池）的上下文模板，已加状态更新 | — |
| 状态登记 | `docs/REVIEW_FINAL.md` v1.1 §9 | M6 本轮进展与最终判断 | — |
| 状态登记 | `docs/SHORT_TERM_MODEL_V1.md` §7 | 扩样本复核：结论未改变 | — |

> **运行期产物不入库**：`runtime/short_term/bars_200.csv`、`runtime/m6/*`（矩阵/预测/净值/特征 IC）、`runtime/qa/*`、`runtime/verify/*`、`runtime/exports/universe_coverage_200.md` 均按 `.gitignore` 排除，属**可复现的中间结果**；需要时按 §4 重新生成。

---

## 4. 如何复现

```powershell
cd C:\Users\shanghao\Documents\deepseek-harness\default-workspace\quantdesk

# 0) 环境自检（期望退出码 0）
.venv\Scripts\python.exe scripts\check_env.py

# 1) 数据构建（约 15 分钟；产出 data/universe_200.json + runtime/short_term/bars_200.csv）
.venv\Scripts\python.exe scripts\build_universe_m6.py --limit 200 --candidates 280 --sleep 0.25

# 2) 数据覆盖审计（离线重算，期望 13/13 PASS）
.venv\Scripts\python.exe scripts\build_universe_m6.py --audit-only

# 3) 研究框架冒烟（bars_50，4 组）
.venv\Scripts\python.exe scripts\m6_research.py --quick

# 4) 36 组实验矩阵（按标签分 3 片并行；单组 logistic≈49s / gbdt≈380-520s / gbdt_rank≈90-120s）
.venv\Scripts\python.exe scripts\m6_research.py --labels ret3_ge2,ret5_ge2 --shard-dir runtime/m6/shards/s1 --log-file runtime/m6/matrix_run_s1.log
.venv\Scripts\python.exe scripts\m6_research.py --labels ret10_ge2,cs_top20_5d --shard-dir runtime/m6/shards/s2 --log-file runtime/m6/matrix_run_s2.log
.venv\Scripts\python.exe scripts\m6_research.py --labels three_class_5d,gap_up_1d --shard-dir runtime/m6/shards/s3 --log-file runtime/m6/matrix_run_s3.log
.venv\Scripts\python.exe scripts\m6_research.py --merge --shard-dir runtime/m6/shards

# 5) 全量测试（DSH 沙箱用包装器；用户环境可直接 pytest tests -q）
.venv\Scripts\python.exe runtime\_run_pytest.py tests -q
```

> 复现依赖网络（akshare/Sina）与约 1 GB 磁盘；数据快照以 2026-10-26 抓取为准，之后重抓可能因接口改版产生差异。

---

## 5. 结论摘要（负结果）

**在日频免费数据上，3/5/10 日选股没有可执行的、统计上可信的边际。**

| 检查 | 结果 |
|---|---|
| 预注册门槛 | RankIC > 0.03 **且** Top 组 10 日扣费后超额 > 0 **且** 命中率显著 > 基础概率（实验前固定，未放宽） |
| 36 组配置 | **34/36 未过门槛**；非「隔夜缺口」族最佳 RankIC=0.0305，低于 max-statistic 零分布 95% 分位 0.0320 |
| 唯一统计显著信号 | `gap_up_1d × gbdt_rank`（1 日隔夜缺口方向）：RankIC 0.146、CI [0.138, 0.154]、多重比较校正 p=0.001 |
| 可成交性（决定性） | 入选票 **17.4%** 收盘已封涨停（全样本 1.04%）→ 买不进；剔除后扣费净超额 **−0.074%/日**（全池 −0.094%/日），命中率回落到 21.54%（≈基础率） |
| 判定 | **不可成交 artifact**（非前视、非统计假象）；`gate=True` 不得写为「可用/可接入」 |
| 处置 | **不接入**推荐/自动化；UI 维持「研究阶段 · 不构成买入建议 · 不承诺收益」 |

> 完整证据：`docs/M6_RESEARCH_REPORT.md` §7；审计证据：`docs/M6_QA_AUDIT.md` §12；独立复核：`runtime/verify/m6_verify.json`（`all_ok=true`）与 `runtime/qa/gap_audit.md`。

---

## 6. 合规边界（并入后仍然有效）

1. **不承诺收益（UBA）**：M6 是研究记录，不是投资建议；历史表现不代表未来收益。
2. **未过门槛不得宣称有效**：任何未通过 §5 预注册门槛 + 多重比较 + 可执行性检验的配置，不得接入推荐/自动化，UI 不得改为「已验证」。
3. **研究阶段不自动下单**：`automation.enabled` 默认关闭；推荐/筛选不自动下单；不接真实资金。
4. **数据口径**：回测撮合/手数/资金一律用不复权真实价（`backtest.price_adjust=none`）；复权价仅用于研究标签与价格类特征。
5. **零 mock**：Mock 数据不入库、不参与 M6 研究结论；缓存混源 fail-closed。
6. **变更需重走验证**：未来若新增数据或模型，须重新执行「三项门槛 + 多重比较 + 可执行性检验 + 独立 QA/回测复核」，并更新研究报告后才能讨论接入。

---

## 7. 对项目1 的影响

| 项 | 影响 |
|---|---|
| 产品默认链路 | **无变化**：推荐/筛选仍为「研究阶段」，自动化默认关闭 |
| 代码接口 | **无破坏性变更**；M6 研究模块不被默认流程 import/调用 |
| 数据库 | **无表结构变更**（沿用 28 张业务表 + `schema_version`） |
| 回测报告 | 新增「同仓位被动基准 + 样本外标记」标准输出（P1，向后兼容地追加字段） |
| 版本 | 版本常量链路统一为 `2.0.0`（`config.yaml` / `core/config.py` / `core/__init__.py` / UI meta 兜底） |
| 测试 | 新增 27 个用例；全量基线 378 → **405 passed / 60 deselected** |

---

## 8. 变更记录

| 版本 | 日期 | 变更 | 作者 |
|---|---|---|---|
| v1.0 | 2026-10-27 | 初版：M6 与项目1 的关系、来源工作流、已并入文件清单、复现命令、负结论摘要、合规边界、影响面 | 发布工程（release-engineer） |
