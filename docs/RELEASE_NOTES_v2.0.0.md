# QuantDesk v2.0.0 发布说明（RELEASE_NOTES）

> **版本**：v2.0.0 · **发布日期**：2026-10-27 · **仓库**：[SSSHHH666/quantdesk](https://github.com/SSSHHH666/quantdesk)（`main`）
> **定位**：Phase 2（M6「策略研究增值」）研究框架并入主仓，并把仓库正式版本化为 v2.0.0；MVP（M0–M5）能力保持不变。
> **一句话**：M6 研究已完整落地并通过独立审计，但结论是**负结果**——日频免费数据上**没有可执行的短线选股边际**，因此不接入推荐/自动化，UI 维持「研究阶段」。

---

## 1. 发布亮点

| # | 亮点 | 说明 |
|---|---|---|
| 1 | **M6 研究框架并入主仓** | `strategy/m6_research.py` + `scripts/m6_research.py`：面板/标签/特征构建、严格 walk-forward（purge/embargo）、36 组实验矩阵、bootstrap/多重比较/分层检验、gate 判定与可执行性检查。 |
| 2 | **200 只 A 股 · 349,466 行日线** | 2019-01-02~2026-10-23，akshare（Sina 通道），**零 mock**；覆盖审计 **13/13 PASS**；样本清单入库（`data/universe_200.json`）。 |
| 3 | **36 组严格样本外验证** | 6 标签 × 2 特征集 × 3 模型 = 36 组，76–77 折滚动 walk-forward、purge=max(5,h)、embargo=5。 |
| 4 | **QA 独立审计 PASS** | 无未来函数（截断不变性 0 差异）、复权标签金标准、purge/embargo 全折合规、可复现、gate 行为、bars_200 数据审计；闭环 9 项缺陷（F1–F8 + F9 结论修正）。 |
| 5 | **P1 同仓位被动基准标准化** | 所有回测报告强制输出「同仓位被动基准 + 样本外标记」，避免只看绝对收益误判策略。 |
| 6 | **测试基线 405 passed** | 新增 `tests/test_m6_research.py`（21 用例）与 `tests/test_backtest_passive.py`（6 用例）；全量 405 passed / 60 deselected。 |
| 7 | **正式发布物料** | `VERSION`、`CHANGELOG.md`、本发布说明、`docs/M6_INTEGRATION.md`，版本常量链路统一为 2.0.0。 |

---

## 2. M6 研究结论摘要（必须如实阅读）

**结论：在日频免费数据上，3/5/10 日选股没有可执行的、统计上可信的边际。**

| 项 | 结果 |
|---|---|
| 预注册门槛（实验前固定，未放宽） | RankIC > 0.03 **且** Top 组 10 日扣费后超额 > 0 **且** 命中率显著 > 基础概率；三项全过才可讨论接入 |
| 36 组结果 | **34/36 组未过预注册门槛**；非「隔夜缺口」族最佳 RankIC 仅 **0.0305**，低于 36 组 max-statistic 零分布 95% 分位 **0.0320**，与噪声不可区分 |
| 唯一统计显著的信号 | `gap_up_1d × gbdt_rank`（1 日隔夜缺口方向，**不是 3/5/10 日选股**）：RankIC **0.146**、5 日块 bootstrap 95% CI **[0.138, 0.154]**、36 组 max-statistic 校正 **p=0.001**（仍显著） |
| 可执行性检验（决定性） | 该信号入选票 **17.4%** 在 t 日收盘已封涨停（全样本仅 1.04%，约 17 倍）→ **买不进**；被锁票次日缺口均值 +1.909% vs 可买票 +0.117% → 超额主体来自不可成交样本 |
| 剔除不可买样本后 | 现实口径扣费净超额 **−0.074%/日（负）**；全池口径 **−0.094%/日**；命中率回落到 **21.54%**（基础率 21.12%–21.53%，lift ≤0.4pp）；t+1 开盘买入口径净超额 **−0.206%/日** |
| 判定 | **不可成交 artifact（非前视、非统计假象）**；报告中的 `gate=True` 不得写为「可用/可接入」 |
| 处置 | **无可执行短线选股边际** → **不接入** `strategy/recommend.py` / 自动化；UI 维持「研究阶段 · 不构成买入建议 · 不承诺收益」 |

> 四路独立证据一致：建模工程师自查、QA 独立专项审计（`runtime/qa/gap_audit.md`）、backtest-engineer 独立数值复核（`runtime/verify/m6_verify.json`，`all_ok=true`）、Lead 自算。
> 完整实验与统计细节：[`docs/M6_RESEARCH_REPORT.md`](M6_RESEARCH_REPORT.md)；独立审计：[`docs/M6_QA_AUDIT.md`](M6_QA_AUDIT.md)。

---

## 3. v2.0.0 新增 / 变更文件清单

### 3.1 发布与版本物料（本次新增）

| 文件 | 说明 |
|---|---|
| `VERSION` | 版本号 `2.0.0`（根目录，供发布/脚本读取） |
| `CHANGELOG.md` | 变更日志（Keep a Changelog + SemVer，含 [2.0.0] 与 [1.0.0] 历史摘要） |
| `docs/RELEASE_NOTES_v2.0.0.md` | 本发布说明 |
| `docs/M6_INTEGRATION.md` | M6 成果并入项目1 的说明：来源、文件清单、复现、结论、合规边界 |

### 3.2 M6 研究成果（并入 `main`，基线 `b5c575a`）

| 类别 | 文件 | 说明 |
|---|---|---|
| 研究代码 | `strategy/m6_research.py` | 面板/特征/标签/walk-forward/gate/统计框架 |
| 研究 CLI | `scripts/m6_research.py` | 36 组矩阵、合并、统计检验、可执行性产出 |
| 数据构建 | `scripts/build_universe_m6.py` | 200 只样本构建 + 覆盖审计 |
| 样本清单 | `data/universe_200.json` | 200 只 A 股清单 |
| 回测口径（P1） | `backtest/validation.py`、`scripts/run_backtest.py` | 同仓位被动基准 + 样本外标记 + 标准报告 |
| 回归测试 | `tests/test_m6_research.py`（21）、`tests/test_backtest_passive.py`（6） | 无前视/复权/purge-embargo/可复现/gate/被动基准 |
| 研究报告 | `docs/M6_RESEARCH_REPORT.md` | 36 组 OOS 矩阵、统计检验、可执行性、结论 |
| QA 审计 | `docs/M6_QA_AUDIT.md` | 独立审计与 F1–F9 证据 |
| 接续提示词 | `docs/M6_HANDOFF_PROMPT.md` | 下一轮研究上下文模板（已加状态更新） |
| 状态登记 | `docs/REVIEW_FINAL.md` v1.1 §9、`docs/SHORT_TERM_MODEL_V1.md` §7 | M6 进展与「结论未改变」记录 |

> 运行期产物（`runtime/short_term/bars_200.csv`、`runtime/m6/*`、`runtime/qa/*`、`runtime/verify/*`）按 `.gitignore` **不入库**，属可复现的中间结果。

### 3.3 变更（Changed）

| 文件 | 变更 |
|---|---|
| `README.md` | 徽章 Version 2.0.0 / Tests 405 passed；当前阶段改为 v2.0.0（2026-10-27）；新增 §4.1 M6 研究成果、§4.2 版本与文档入口；文档索引补 CHANGELOG/RELEASE_NOTES/M6_INTEGRATION/M6_RESEARCH_REPORT；§8 变更记录加 v2.0 行 |
| `docs/DOC_INDEX.md` | 升 v1.10（2026-10-27），新增发布物料索引行，登记 M6 相关文档状态 |
| `config.yaml`、`core/config.py`、`core/__init__.py`、`ui/pages/_common.py`、`ui/pages/settings.py` | 版本字符串常量 `1.0.0` → `2.0.0`（仅字符串，无逻辑/结构变更） |
| `docs/REVIEW_FINAL.md`（v1.1）、`docs/SHORT_TERM_MODEL_V1.md`（§7）、`docs/M6_HANDOFF_PROMPT.md` | 已在 `main` 中登记 M6 本轮进展 |

---

## 4. 验证证据

| 验证项 | 结果 / 证据 |
|---|---|
| 全量测试 | **405 passed / 60 deselected**（`runtime\_run_pytest.py tests -q`；M6 矩阵并行期耗时较长，属正常） |
| M6 专项测试 | `tests/test_m6_research.py` **21 passed**；`tests/test_backtest_passive.py` 6 用例 |
| QA 独立审计 | **PASS**：截断不变性（54,743 行 × 32 特征逐元素 0 差异）、复权标签（h=1/3/5/10 全量 0 不符）、purge/embargo（15/15 折）、可复现（58 项指标 0 差异）、被动基准对齐、gate 行为、bars_200（349,466 行）数据审计 |
| 数据覆盖审计 | **13/13 PASS**（`runtime/exports/universe_coverage_200.md`：无重复、source 唯一、adj_factor 无缺失、每只 ≥800 行、零 mock） |
| 独立数值复核 | `runtime/verify/m6_verify.json` → `all_ok=true`、`failures=[]`；核心指标与报告 diff ≤1e-6；gap 配置 `tradable=false` |
| 可执行性专项复核 | `runtime/qa/gap_audit.md`：17.4% 收盘涨停不可买，剔除后扣费净超额为负 → **不可成交 artifact** |
| 版本链路 | `.venv\Scripts\python.exe -c "from core.config import get_config; print(get_config().app.version)"` → **2.0.0** |
| 发版自检 / 全链路验证 | `release_check.py` 完整模式 **PASS 11 / WARN 0 / FAIL 0**；`verify_all.py --with-m6` **PASS 6 / FAIL 0**；逐项证据见 [`docs/RELEASE_VERIFICATION_v2.0.0.md`](RELEASE_VERIFICATION_v2.0.0.md) |

---

## 5. 升级说明

1. **无破坏性 API 变更**：本版本不修改既有模块接口、配置键与数据库表结构（沿用 28 张业务表 + `schema_version`）。
2. **版本常量更新**：`config.yaml`、`core/config.py`、`core/__init__.py` 及 UI meta 兜底值统一为 `2.0.0`；启动页/设置页会显示 v2.0.0。
3. **回测报告口径**：报告新增「同仓位被动基准 + 样本外标记」标准输出（P1）；`scripts/run_backtest.py` 是**单区间回测**，报告中的样本外标记固定为「否」，不得据此宣称样本外结果。
4. **研究阶段不接自动化**：M6 未通过预注册门槛的配置不进入推荐/自动化；`automation.enabled` 默认 `false`。升级后无需修改既有配置。
5. **数据与运行期**：`runtime/` 不随仓库分发；首次使用仍按 README 第 2 节完成环境准备与数据拉取；M6 复现需重新构建数据（见 §7）。

---

## 6. 已知问题与限制

| 类别 | 问题 | 影响 / 说明 |
|---|---|---|
| 研究结论 | M6 未找到可执行短线选股边际；唯一显著信号为不可成交 artifact | **不得**将任何 M6 配置宣传为可用/可跟单；推荐/筛选维持研究阶段 |
| 数据偏差 | 股票池来自 2026-10-25 仍存活、仍活跃的股票 | 存在**存活者偏差**与事后选池；所有有效性判定以 RankIC/相对被动超额为准，不看组合绝对净值 |
| 数据源 | 单一免费源（akshare/Sina），无资金流/龙虎榜/行业/分钟线 | 接口可能改版/限流；复现需重抓快照 |
| 运行期 | `runtime/` 产物不入库 | `bars_200.csv`、`m6_*`、数据库与日志需本地重建；跨机器复现依赖网络 |
| 仓库形态 | **本地无 git**，版本化产物由 GitHub API 提交 | 无本地 commit/tag 历史；以 GitHub tag `v2.0.0` 与 Release 为准 |
| 历史占位 | 设置页保存类接口、审计/日志查询、策略实例创建仍为占位 | 不影响研究与模拟盘使用；见 `docs/DOC_INDEX.md` §3.5 |
| 测试报告 | `docs/TEST_REPORT.md` 仍待 QA 补交 | 当前测试口径以本说明与 `docs/RELEASE_VERIFICATION_v2.0.0.md` 为准 |

---

## 7. 复现命令

```powershell
cd C:\Users\shanghao\Documents\deepseek-harness\default-workspace\quantdesk

# 0) 环境自检（期望退出码 0）
.venv\Scripts\python.exe scripts\check_env.py

# 1) M6 数据构建（约 15 分钟，Sina 通道；产出 data/universe_200.json / runtime/short_term/bars_200.csv）
.venv\Scripts\python.exe scripts\build_universe_m6.py --limit 200 --candidates 280 --sleep 0.25

# 2) 数据覆盖审计（离线重算，期望 13/13 PASS）
.venv\Scripts\python.exe scripts\build_universe_m6.py --audit-only

# 3) 研究框架冒烟（bars_50，1 标签 × 2 模型 × 2 特征集 = 4 组）
.venv\Scripts\python.exe scripts\m6_research.py --quick

# 4) 36 组实验矩阵（按标签分片并行；单组耗时 logistic≈49s / gbdt≈380-520s / gbdt_rank≈90-120s）
.venv\Scripts\python.exe scripts\m6_research.py --labels ret3_ge2,ret5_ge2 --shard-dir runtime/m6/shards/s1 --log-file runtime/m6/matrix_run_s1.log
.venv\Scripts\python.exe scripts\m6_research.py --labels ret10_ge2,cs_top20_5d --shard-dir runtime/m6/shards/s2 --log-file runtime/m6/matrix_run_s2.log
.venv\Scripts\python.exe scripts\m6_research.py --labels three_class_5d,gap_up_1d --shard-dir runtime/m6/shards/s3 --log-file runtime/m6/matrix_run_s3.log
.venv\Scripts\python.exe scripts\m6_research.py --merge --shard-dir runtime/m6/shards

# 5) 全量测试（DSH 沙箱用包装器；用户环境可直接 pytest tests -q）
.venv\Scripts\python.exe runtime\_run_pytest.py tests -q
```

> 环境基线：Python 3.12.7（项目 `.venv`）；依赖见 `requirements.txt` 与 `docs/RUNBOOK.md` 附录 A。

---

## 8. 免责声明

1. 本项目为**个人研究/学习用途**，**不是投资建议**，**不承诺收益**（UBA）；回测与模拟盘结果不代表未来表现。
2. **不接真实资金**：MVP 仅提供 `BrokerAdapter` 抽象与 `PaperBroker` 模拟盘；自动化默认关闭，任何自动动作都可一键停止。
3. M6 研究结论为**负结果**：日频免费数据上未发现可执行的短线选股边际；未经完整验证与可执行性检验的配置**不得**接入推荐/自动化，UI 不得宣称有效。
4. 数据来自公开接口（akshare/Sina），可能存在缺失、延迟或口径差异；请遵守数据源使用条款，不要高频并发抓取。
5. 第三方依赖与借鉴代码的许可信息见 [`docs/THIRD_PARTY.md`](THIRD_PARTY.md)；本仓库采用 MIT 许可（见 [`LICENSE`](../LICENSE)）。

---

## 9. 相关文档

- [CHANGELOG.md](../CHANGELOG.md) — 版本变更日志（2.0.0 / 1.0.0）
- [VERSION](../VERSION) — 当前版本号
- [docs/M6_INTEGRATION.md](M6_INTEGRATION.md) — M6 并入项目1 说明（文件清单/复现/合规边界）
- [docs/M6_RESEARCH_REPORT.md](M6_RESEARCH_REPORT.md) — M6 研究报告（v1.0）
- [docs/M6_QA_AUDIT.md](M6_QA_AUDIT.md) — M6 QA 独立审计（v1.0）
- [docs/REVIEW_FINAL.md](REVIEW_FINAL.md) — 项目最终状态评审（v1.1，§9 M6 进展）
- [docs/DOC_INDEX.md](DOC_INDEX.md) — 文档索引（v1.10）
- [docs/RELEASE_VERIFICATION_v2.0.0.md](RELEASE_VERIFICATION_v2.0.0.md) — v2.0.0 发布验证报告（verify-engineer 生成）
- [README.md](../README.md) — 快速开始与使用说明

---

## 10. 变更记录

| 版本 | 日期 | 变更 | 作者 |
|---|---|---|---|
| v1.0 | 2026-10-27 | 初版：v2.0.0 发布说明（亮点、M6 结论摘要、新增文件清单、验证证据、升级说明、已知问题、复现命令、免责声明） | 发布工程（release-engineer） |
