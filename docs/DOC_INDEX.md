# QuantDesk 文档索引与阅读顺序（DOC_INDEX）

> 版本：v1.11 · 2026-10-27 · 作者：PM（pm-quant）；发布登记：release-engineer
> 用途：**任何人（用户、新成员、后来的智能体）拿到项目后，知道先读什么、后读什么、哪份是权威、哪份还在写。**
> 维护：文档有新增/改名/状态变化时更新本文件；文档内容本身由各自 owner 维护（见 §4）。

---

## 1. 30 秒结论

- **唯一事实来源**：`quantdesk/BRIEF.md` **v0.2**（用户意图、范围 F1–F13、技术栈、契约）。任何文档与它冲突，以它为准。
- **要写代码**：先读 `docs/SPEC.md`（口径与接口）+ `docs/TASKS.md`（你的任务、文件、验收）+ `docs/ARCHITECTURE.md`（为什么这样设计）。
- **要用系统**：读 `quantdesk/README.md`（怎么装怎么启）+ `docs/GLOSSARY.md`（术语）+ `docs/ROADMAP.md`（现在到哪一步）；**某个页面空白是设计还是缺陷** → 见 §3.5「UI 页面状态表」。
- **要验收/接手**：读 `docs/TASKS.md`（契约冻结清单）+ `docs/ROADMAP.md`（每个里程碑的验收演示脚本）+ `docs/ARCHITECTURE.md` §8/§9（降级与安全）。
- **查版本/发布**：先看 `CHANGELOG.md` 与 `docs/RELEASE_NOTES_v2.0.1.md`（当前） / `docs/RELEASE_NOTES_v2.0.0.md`（历史）；确认「M6 是否已并入项目1」看 `docs/M6_INTEGRATION.md`。

---

## 2. 文档权威顺序（冲突时从上到下）

| 顺序 | 文档 | 说明 |
|---|---|---|
| 1 | `quantdesk/BRIEF.md` | 用户意图与范围，**最高** |
| 2 | `docs/SPEC.md` | 需求、A 股口径、数据模型、接口签名、验收标准 |
| 3 | `docs/TASKS.md` | 任务、依赖、文件归属、**契约冻结清单 CF-1…CF-13** |
| 4 | `docs/ARCHITECTURE.md` | 架构解释（分层/数据流/失败/扩展），**不覆盖契约** |
| 5 | `docs/ROADMAP.md` / `docs/DOC_INDEX.md` / `docs/GLOSSARY.md` | 计划、索引、术语（解释性） |
| 6 | `docs/BORROW_MAP.md`、`docs/borrow_map.json` | 借鉴代码来源与许可判定（**设计参考**，含非 MVP 内容，见 ARCHITECTURE §12） |
| — | 代码与实际行为 | 若文档与代码不一致，以 `SPEC.md` 为准开缺陷修复，不允许“代码跑成什么样就改文档成什么样” |

**变更规则**：冻结契约（CF-1…CF-13）变更须 Lead 审批 → PM 更新 SPEC/TASKS 版本 → 通知全员 → 同步改测试。文档不得绕过审批擅自改契约。

---

## 3. 文档清单

### 3.1 项目级文档

| 文档 | 一句话说明 | 面向谁 | 状态 | 维护者 | 对应任务 |
|---|---|---|---|---|---|
| [BRIEF.md](../BRIEF.md) | 项目简报：用户意图转译、合规口径（含不承诺收益/UBA）、**范围 F1–F15（MVP F1–F9 + Phase 2 F10–F15）**、技术栈（CPU-only/不引入 DL）、目录与接口契约、里程碑 M0–M6、已知风险——**团队唯一事实来源** | 全员、后来的智能体 | ✅ **v0.3** 已完成 | Lead | — |
| [README.md](../README.md) | 项目门面：一句话介绍、快速开始（一键启动/环境自检/手动启动）、目录结构、当前进度（含 **§4.1 M6 研究成果**、§4.2 版本与文档）、FAQ、安全免责、文档索引入口（**终态 12 页**） | **用户**、新成员 | ✅ **v2.0.1** 已完成（v2.0.1 补丁发布） | 发布工程（release-engineer） | M0-08 起 / v2.0.1 |
| [CHANGELOG.md](../CHANGELOG.md) | 变更日志（Keep a Changelog + SemVer）：**[2.0.1]**（干净克隆测试健壮性补丁）、**[2.0.0]**（M6 研究框架并入 + 版本化）、**[1.0.0]**（MVP M0–M5 历史摘要） | 全员、发布/升级者 | ✅ **v2.0.1** 已建立（2026-10-27） | 发布工程（release-engineer） | v2.0.1 |
| [VERSION](../VERSION) | 当前版本号：`2.0.1`（发布与脚本读取的单一版本文件） | 发布/脚本 | ✅ **2.0.1** 已建立（2026-10-27） | 发布工程（release-engineer） | v2.0.1 |
| [RELEASE_NOTES_v2.0.1.md](RELEASE_NOTES_v2.0.1.md) | **v2.0.1 补丁说明**：干净克隆缺 `runtime/` 数据时测试 error → skip 的问题、修复、验证证据、升级/兼容性、复现、免责 | **用户**、升级者、Lead | ✅ **v1.0** 已完成（2026-10-27） | 发布工程（release-engineer） | v2.0.1 |
| [RELEASE_NOTES_v2.0.0.md](RELEASE_NOTES_v2.0.0.md) | **v2.0.0 发布说明（历史）**：亮点、M6 结论摘要（含「不可成交 artifact」）、新增文件清单、验证证据、升级说明、已知问题、复现命令、免责声明 | **用户**、升级者、Lead | ✅ **v1.0** 已完成（2026-10-27） | 发布工程（release-engineer） | v2.0 |
| [RELEASE_VERIFICATION_v2.0.0.md](RELEASE_VERIFICATION_v2.0.0.md) | **v2.0.0 发版验证报告**：全链路验证结论与证据（测试 / 数据 / 环境 / 发版自检 / 界面 / M6 冒烟）；**§9 为 v2.0.0 → v2.0.1 干净源码复核** | **用户**、Lead | ✅ **v1.0** 已完成（2026-10-27，含 §9 复核记录） | verify-engineer / release-engineer（§9） | v2.0 / v2.0.1 |
| [SPEC.md](SPEC.md) | 需求规格：用户画像、A 股规则口径（含**不复权价格核算**与 `backtest.price_adjust` 配置键）、22+5 张表、接口契约、**F1–F15（MVP 54 + Phase2 29 条 AC）**、风控参数/RK001–RK020、**12 页 UI**、`screen.*`（F14/F15）、12 个 RQ 警告码、8 个开放问题（OQ-6/7/8 已拍板）、14 条设计决策、合规/UBA | 开发、QA、Lead、Integrator | ✅ **v1.7** 已完成（用户目标升级 + F14/F15） | PM | task-4 |
| [TASKS.md](TASKS.md) | 任务分解：**80 项任务（M0–M6）**、依赖与文件归属、MVP 13 项冻结 + Phase 2 扩展已批准、角色负载与 R1–R8 排期、变更流程 | 开发、QA、Integrator、Lead | ✅ **v1.9** 已完成；**任务状态随执行实时更新** | PM | task-4 |
| [ARCHITECTURE.md](ARCHITECTURE.md) | 架构设计：分层与依赖方向、一次自动交易的完整时序、事件主题流转、订单/任务/熔断状态机、部署形态与硬件（**MVP 不需要 GPU**）、扩展点（新增策略/数据源/券商/通知/页面）、故障降级、安全边界、性能设计 | 开发、QA、Integrator、Lead | ✅ v1.0 已完成（M6 架构补充待做） | PM | task-10 |
| [ROADMAP.md](ROADMAP.md) | 路线图：M0–M6 每阶段目标/交付物/**用户视角验收演示脚本**/轮次人力/风险；非目标与未来扩展（含 GPU/深度学习结论）；**M1 验收基线**；**M6 研究阶段暂停（§8.5）**；交付节奏与变更机制 | **用户**、Lead、QA | ✅ **v1.5** 已完成（M6 暂停口径） | PM | task-10 |
| [DOC_INDEX.md](DOC_INDEX.md) | 本文件：文档索引、阅读顺序、权威顺序、状态与维护规则、**12 页 UI 页面状态表** | 全员、后来的智能体 | ✅ **v1.11** 已完成 | PM | task-10 / v2.0.1 |
| [GLOSSARY.md](GLOSSARY.md) | 术语表：A 股规则类、量化指标类、系统概念类、**Phase 2 新增术语**、缩写，面向**非程序员用户** | **用户**、新成员 | ✅ **v1.1** 已完成 | PM | task-10 |

### 3.2 设计与实现参考文档

| 文档 | 一句话说明 | 面向谁 | 状态 | 维护者 | 对应任务 |
|---|---|---|---|---|---|
| [BORROW_MAP.md](BORROW_MAP.md) | 借鉴代码精确索引：38 个借鉴点（表 A 基础设施/执行、表 B 策略/回测、表 C 数据/自动化/UI）+ 许可判定（可直接复用 vs 只重写思路）+ 8 个实现模式速查（伪代码级）+ 执行顺序建议 | 开发、Lead | ✅ v1.0 已完成 | code-scout | task-5 |
| [borrow_map.json](borrow_map.json) | BORROW_MAP 的机器可读版（42 条记录，字段：module/source_repo/source_path/url/license/reuse_policy/what_to_adapt），可被脚本/UI 消费 | 开发、后来的智能体 | ✅ 已完成 | code-scout | task-5 |
| [THIRD_PARTY.md](THIRD_PARTY.md) | 第三方依赖与借鉴代码的许可登记：名称、版本、许可、来源 URL、用途、复用或重写判定、需保留声明 | 全员、Lead（合规） | ✅ v1.0 已完成；依赖锁定后（M4-08）更新版本 | Integrator | M4-04 |
| [RUNBOOK.md](RUNBOOK.md) | 运维手册（**已交付 v1.2**）：安装、启动、常见错误排查、备份恢复、日志位置、停止自动化、升级回滚；附录 A 为本机实测环境基线（备份/恢复脚本 `backup_db.py`/`restore_db.py` 已交付） | 用户、Integrator、值班者 | ✅ **v1.2** 已完成 | 交付文档维护（eval-data）/ Integrator | M4-05 |
| [USER_GUIDE.md](USER_GUIDE.md) | 用户手册（**已交付 v1.0**）：从安装到完成 4 个核心任务的分步说明、12 页逐页说明、FAQ、免责声明 | **用户** | ✅ **v1.0** 已完成（2026-10-26） | 交付文档维护（eval-data） | M5-06 |
| [DEMO_WALKTHROUGH.md](DEMO_WALKTHROUGH.md) | 录屏演示脚本（**已交付 v1.0**）：拉数据→看推荐→跑回测→模拟买入→看持仓→触发自动化，含交易日/非交易日两条路径与参考基线 | **用户**、Lead | ✅ **v1.0** 已完成 | 交付文档维护（eval-data） | M5-06 |
| [STRATEGY_RESEARCH.md](STRATEGY_RESEARCH.md) | A 股短线策略研究手册（**研究输入 v1.0**）：流派规则、可量化信号库、可信度分级、数据现实；不构成收益承诺（部分章节标注“续写中”） | Dev2、PM | 🟡 v1.0（部分章节待续） | 量化策略研究员 | M6 研究 |
| [DATA_SOURCES.md](DATA_SOURCES.md) | 数据源可用性矩阵与口径（**v1.0**）：aksahre/新浪主备、字段单位、不可得字段清单、R6 扩展表与 v2 迁移说明 | Dev1、Dev2、QA | ✅ v1.0 已完成 | 数据研究支持（eval-data） | M1-02/M6 |
| [DATA_QUALITY_NOTES.md](DATA_QUALITY_NOTES.md) | 数据质量坑位说明（**v1.0**）：不复权假暴跌、单位、复权、停牌、缺口等实测证据与正确处理方式 | Dev1、Dev2、QA | ✅ v1.0 已完成 | 数据研究支持（eval-data） | M1/M6 |
| [SCREENER_VALIDATION.md](SCREENER_VALIDATION.md) | 全市场筛选 F15 验证报告（**v1.0**）：2020-01~2026-10、50 只样本、5/10/20 日 RankIC 均为负（-0.029/-0.030/-0.037），结论“无可用正向边际”→ 研究阶段暂停依据 | PM、Lead、Dev2 | ✅ v1.0 已完成 | Dev2 | M6-15 |
| [SHORT_TERM_MODEL_V1.md](SHORT_TERM_MODEL_V1.md) | 短线模型 v1 实验记录（**已交付 v1.0**）：3 标的/48 折 walk-forward、OOS 命中率 24.83%≈基础概率、扣费期望 -0.22%、MDD -32.41%、`usable=False`；含 v2 实验框架与筛选器负结果、**§7 M6 扩样本复核（结论未改变）** → **研究阶段暂停依据** | Dev2、QA、PM | ✅ **v1.0** 已完成（2026-10-26，§7 追加 M6 复核） | Dev2 | M6-13/M1 |
| [M6_RESEARCH_REPORT.md](M6_RESEARCH_REPORT.md) | **M6 研究报告（v1.0，2026-10-26）**：200 只 A 股 / 2019–2026 / 36 组 walk-forward OOS（6 标签 × 2 特征集 × 3 模型）；预注册门槛 RankIC>0.03 + Top 10 日扣费超额>0 + 命中率显著>基础概率；含多重比较、bootstrap CI、分层、单因子预检、命中率陷阱检验、与 v1/筛选器基线对照、复现命令 | Lead、PM、QA | ✅ v1.0 已完成（2026-10-26） | Lead | M6-18…M6-22 |
| [M6_QA_AUDIT.md](M6_QA_AUDIT.md) | **M6 QA 独立审计（v1.0）**：无未来函数金标准（截断不变性）、复权标签、purge/embargo、可复现、被动基准对齐、gate 行为、bars_200 数据审计；闭环 F1–F8 缺陷 + F9 结论修正（不可成交 artifact）与证据 | QA、Lead | ✅ v1.0 已完成（2026-10-26） | QA | M6-22 |
| [M6_HANDOFF_PROMPT.md](M6_HANDOFF_PROMPT.md) | M6 研究阶段接续提示词（**v1.0**）：新会话可直接粘贴的上下文与任务清单、复查命令、常见陷阱 | Lead、后继会话 | ✅ v1.0 已完成（2026-10-26，已加状态更新） | Lead | M6 恢复 |
| [M6_INTEGRATION.md](M6_INTEGRATION.md) | **M6 成果并入说明（v1.0，2026-10-27）**：M6 与项目1 的关系、已并入文件清单（代码/测试/报告/审计/P1）、复现命令、负结论摘要、合规边界、对项目1 的影响 | **用户**、Lead、后继会话 | ✅ **v1.0** 已完成（v2.0.0 发布物料） | 发布工程（release-engineer） | v2.0 |
| `TEST_REPORT.md` | 测试报告（**待交付**）：全量用例统计、失败/xfail 说明、性能基线；当前口径见 `REVIEW_FINAL.md` §1（377 passed / 60 deselected / 1 xfailed） | QA、Lead | ⏳ 待交付（QA） | QA | M4-06/M5-07 |

### 3.3 测试与验收

| 文档 | 一句话说明 | 面向谁 | 状态 | 维护者 | 对应任务 |
|---|---|---|---|---|---|
| `quantdesk/tests/`（pytest 用例） | 单元/集成/端到端测试；文件按模块命名（`test_data_layer.py`、`test_backtest_rules.py`、`test_risk_gate.py`、`test_paper_broker.py`、`test_automation_pipeline.py`、`e2e_smoke.py` 等） | QA、开发 | 🚧 随任务交付逐步增加 | QA（各 Dev 可加自己模块用例） | M0-10、M1-14…16、M2-11/12、M3-09/10、M4-06 |
| [REVIEW_M0_R1.md](REVIEW_M0_R1.md) | M0/R1 阶段评审 + **M0 退出复核** + **Phase 2 范围变更评审/批准落地**：AC 对照、P0/P1/P2 处置（含回归用例名）、M0 退出检查清单（C1–C6）、M1 开工顺序、M6 风险与前置检查 | Lead、开发、QA | ✅ **v1.5（含 Phase 2 批准 + M1 修订指针）** 已完成 | PM | — |
| [REVIEW_M1.md](REVIEW_M1.md) | **M1 研究闭环验收评审（v1.3）**：不复权真实数据基线、4 策略 vs 同仓位被动、**ML OOS AUC 0.4456 负结果**、**短线模型 v1 `usable=False`（48 折 OOS 命中率 24.83%≈基础概率、扣费期望 -0.22%、MDD -32.41%）**、M1 退出结论（功能通过/有效性未证明）、M6 条件核对、**用户目标升级 + F14/F15 登记**、8 项遗留核验 | Lead、开发、QA、**用户** | ✅ **v1.3** 已完成 | PM | — |
| [REVIEW_FINAL.md](REVIEW_FINAL.md) | **项目最终状态评审（v1.1）**：M0–M5 完成度、已验证能力与证据（28 表校验、真实回测基线、M2/M3/M4 实测）、研究阶段暂停项、**最终交付清单**、上线使用须知与风险声明、**§9 M6 本轮进展（200 只 / 36 组 OOS，结论仍无可用配置）** | Lead、**用户**、QA | ✅ **v1.1** 已完成（2026-10-26，M6 进展登记） | PM | v2.0 登记 |
| [ACCEPTANCE_CHECKLIST.md](ACCEPTANCE_CHECKLIST.md) | 验收清单（**v2.0 项目终态版**）：52 条 AC 逐项实际状态（42 ✅ / 8 🟡 / 1 ☐ / 1 ⊘）+ 证据命令；G1–G6 与里程碑门禁；10 项未通过/未覆盖原因 | QA、Lead、用户 | ✅ **v2.0** 已完成（2026-10-26） | PM | M5-07 |

### 3.4 项目外部的调研证据（只读，不在 `quantdesk/` 内）

> 这些是立项依据与需求来源，实现时不需要读，验收/追溯时可查。

| 文档/目录 | 一句话说明 | 状态 |
|---|---|---|
| `../../quant_github_projects.md`（工作区根目录） | 30 个参考项目 + 10 个避坑清单 + 推荐组合（star/许可/活跃度快照） | 📚 已完成 |
| `../../data/eval/report_A.md` | A 批 15 个回测/实盘框架的深度评估与选型建议 | 📚 已完成 |
| `../../data/eval/report_B.md` | B 批 15 个交易机器人/AI 智能体的评估（含“高星虚高/营销型”判定） | 📚 已完成 |
| `../../data/eval/report_C.md` | C 批 15 个数据源/因子/ML/组合优化项目的评估 | 📚 已完成 |
| `../../data/eval/<owner>__<name>.md`（54 个） | 每个仓库的 GitHub API 原始快照（元数据、提交、README 摘要） | 📚 已完成 |
| `../../data/trees/*.txt`（14 个） | 14 个参考仓库的完整文件树，BORROW_MAP 路径真实性的证据 | 📚 已完成 |

### 3.5 UI 页面状态表（12 页 = MVP 9 + Phase 2 3）

> 用途：让用户判断“页面空白是**设计如此**还是**缺陷**”。状态定义：
> **已接线**＝页面已调用真实服务层并有可交互功能；**需先拉数据**＝功能可用，但首次使用要先在行情页/回测页拉取数据；**部分/占位**＝页面可用但部分接口未接线（属**设计内空白**，不是缺陷）；**待开发**＝文件尚未创建（**当前 12 页均已创建**）。

| # | 页面 | 文件 | 状态 | 说明 / 用户该怎么做 |
|---|---|---|---|---|
| 1 | 总览 | `ui/pages/overview.py` | ✅ 已接线（部分统计占位） | 账户/持仓/状态灯/上手指引可用；今日管线统计等区块显示“开发中” |
| 2 | 行情 | `ui/pages/market.py` | ✅ 已接线（需先拉数据） | 首次点“拉取/刷新数据”写入本地缓存；无数据时按页面引导操作 |
| 3 | 手动交易 | `ui/pages/manual_trade.py` | ✅ 已接线 | 风控预检→二次确认→下单→成交/持仓；非交易时段被风控拦截（RK005），属保护 |
| 4 | 策略 | `ui/pages/strategies.py` | 🟡 部分 | 策略列表可看；实例创建/参数保存接口待接（按钮提示“接口待接入”） |
| 5 | 回测 | `ui/pages/backtest.py` | ✅ 已接线（需先拉数据） | 选策略/区间→拉数据→运行→KPI/曲线/交易明细/导出 |
| 6 | 持仓 | `ui/pages/positions.py` | ✅ 已接线 | 持仓/成本/市值/清仓/重置可用（破坏性操作二次确认） |
| 7 | 自动化 | `ui/pages/automation.py` | ✅ 已接线 | 调度器启停（默认关闭）/立即运行/最近记录；非交易日 SKIPPED |
| 8 | 设置 | `ui/pages/settings.py` | 🟡 部分 | 环境/版本/免责/备份恢复可用；风控/通知等保存类接口待接 |
| 9 | 日志 | `ui/pages/logs.py` | 🟡 部分 | 运行日志/审计/数据校验可查过滤；分页/查询接口待接 |
| 10 | 自选池（Phase 2） | `ui/pages/watchlist.py` | ✅ 已接线 | 增删改/分组/CSV 导入预览/导出可用；存储优先 SQLite，缺失时 JSON 兜底 |
| 11 | 今日推荐（Phase 2） | `ui/pages/recommend.py` | ✅ 已接线（需先拉数据；研究阶段） | 远期/近期两榜 + 异动雷达已接 `strategy/recommend.py`；需先拉行情，无结果时展示引导 |
| 12 | 全市场筛选（F14/F15，Phase 2） | `ui/pages/screener.py` | ✅ **已交付（研究阶段）** | 全市场扫描（总数/过滤后/扫描数/数据时间）/导出 CSV/研究阶段声明可用；`app_services.screen_market` 正式接线待验收，F15 未通过（RankIC 负）→ 不构成买入建议 |

> 维护：页面状态变化（占位→已接线）时由对应页面 owner 更新本表；验收时以本表 + `REVIEW_M0_R1.md` 为准。

### 3.6 配置与入口文件（非文档，但常被文档引用）

| 文件 | 一句话说明 | 状态 |
|---|---|---|
| `../config.yaml` | 默认配置（键名冻结 CF-9）：数据源/费用/风控/回测/自动化/通知；**Phase 2**：`recommend.*`/`research.*`（已批准，默认关闭） | 🚧 初版已交付（Phase 2 键待补） |
| `../requirements.txt` | 依赖清单与版本区间；M4-08 锁定版本；**sklearn 为 Phase 2 可选依赖**（已批准） | 🚧 初版已交付（可选依赖待登记） |
| `../app.py` | Streamlit 入口（动态发现 **12 页**，D-9） | ✅ 已交付（M0-07） |
| `../scripts/start.bat`、`check_env.py` | 一键启动与环境自检 | ✅ 初版已交付 |
| `../BRIEF.md` | 唯一事实来源（**v0.2：F1–F13**） | ✅ 已更新 |

**图例**：✅ 已完成（版本号）· 🚧 进行中/随进度更新 · ⏳ 待写/待交付 · 📚 外部只读证据

---

## 4. 阅读路径（按角色）

### 4.1 用户（不写代码，想用起来）— 约 30 分钟

```
1. README.md            → 怎么安装、怎么双击启动、常见问题（重点：Q1–Q7）
2. GLOSSARY.md          → 看不懂的词先查这里（T+1、涨跌停、夏普、回撤…）
3. ROADMAP.md §2–§7     → 现在做到哪一步、每个阶段你能验证什么（照“验收演示脚本”点一遍）
4. USER_GUIDE.md（未交付，后续轮次）→ 等交付后按手册完成 4 个核心任务
5. 免责声明（README §7）→ 回测不代表真实收益，MVP 不接真实资金
```

### 4.2 开发（Dev1/Dev2/Dev3）— 开工前 1 小时

```
1. BRIEF.md             → 范围、技术栈、目录归属（必读，尤其 §5 文件契约、§2 合规）
2. TASKS.md §0/§2/§3    → 你的任务编号、可写文件、验收标准、依赖；契约冻结清单
3. SPEC.md 对应章节     → 你模块的规则细节：Dev1 读 §6.1/§8.1；Dev2 读 §6.2/§6.4/§8.2/§8.3；Dev3 读 §4/§6.3/§8.4–8.9/§10
4. ARCHITECTURE.md §2/§4/§8/§9 → 依赖方向（别 import 错层）、数据流、失败降级、安全边界
5. BORROW_MAP.md 你的模块 → 借鉴点 + 许可判定（GPL 只重写思路，不复制代码）；不要照抄非 MVP 内容（见 ARCHITECTURE §12）
6. THIRD_PARTY.md       → 复用的代码要登记来源与许可
```

### 4.3 QA / Integrator — 开工前 1 小时

```
1. SPEC.md §8（AC-F1…F9 全部验收标准）+ §12（DoD）
2. TASKS.md §1.1/§4/§5  → 任务与轮次、文件归属、变更流程
3. ROADMAP.md 对应里程碑 → “用户视角验收演示脚本”直接转成手工验收步骤
4. ARCHITECTURE.md §8/§9 → 故障降级与安全边界，用于设计异常用例
5. RUNBOOK.md（已交付 v1.2）   → 排障手册
```

### 4.4 后来的智能体（接手/继续开发）— 30 分钟

```
第 1 步：读本文件 DOC_INDEX.md（你在这里）→ 了解有哪些文档、谁是权威
第 2 步：BRIEF.md → 快速建立范围感（尤其“第一版明确不做”）
第 3 步：SPEC.md §0/§1/§5/§6/§7 → 当前契约（表、接口、配置、原因码）
第 4 步：TASKS.md §1.2 + 团队任务板 → 哪些任务已完成/进行中/可领
第 5 步：ARCHITECTURE.md → 你要动的地方在哪一层、能依赖谁、不能依赖谁
第 6 步：GLOSSARY.md 扫一眼（统一用语，别自造词）
规则：不改冻结契约；不扩大 MVP 范围；改完更新对应文档并走 §5 的更新流程。
```

---

## 5. 文档维护规则

| 规则 | 说明 |
|---|---|
| 谁写谁维护 | 见各表“维护者”列；`SPEC.md`/`TASKS.md`/`ARCHITECTURE.md`/`ROADMAP.md`/`DOC_INDEX.md`/`GLOSSARY.md` = PM；`BRIEF.md` = Lead；`BORROW_MAP*` = Scout；`THIRD_PARTY/RUNBOOK/USER_GUIDE` = Integrator；验收清单 = QA |
| 何时更新 | ①契约变更获批后（改 SPEC/TASKS，升版本号并写变更记录）；②里程碑完成/验收通过后（改 ROADMAP 状态、README 进度）；③新文档/改名（改 DOC_INDEX）；④任务状态（改 TASKS 或任务板） |
| 怎么改 | 只改自己 owner 的文件；跨文件改动先 `send_message` 给 owner + Lead 报备；**版本号 + 变更记录**是硬要求（每份文档末尾都有“变更记录”表） |
| 不写什么 | 不写业务代码；不复制 GPL/AGPL 源码片段；不编造尚未存在的文件路径/API（规划中的要标“规划/待写”）；不把密钥/账号写入任何文档 |
| 一致性检查 | QA/Lead 在里程碑验收时抽查：文档描述 vs 实际文件 vs 测试结果（三方一致）；发现不一致按“以 SPEC 为准开缺陷”处理 |
| 状态图例 | ✅ 已完成 / 🚧 进行中 / ⏳ 待写 / 📚 外部只读；状态与版本号必须与实际一致，不允许“文档写着已完成但文件不存在” |

---

## 6. 常见问题

**Q1：文档太多，先看哪一份？**
先看 §1、§2，然后按 §4 找自己的角色路径。只关心“怎么用”就只读 README + GLOSSARY + ROADMAP。

**Q2：SPEC 和 BORROW_MAP 说法不一样怎么办？**
以 SPEC 为准（BORROW_MAP 是设计参考，含非 MVP 内容）。`ARCHITECTURE.md` §12 列了已知差异清单，可直接查。

**Q3：我要改一个接口签名，该改哪几份文档？**
不能直接改。先按 `TASKS.md` §6 提交变更申请 → Lead 审批 → PM 更新 `SPEC.md`（接口签名）与 `TASKS.md`（契约冻结项）→ 通知受影响的开发与 QA → 同步改测试 → 在 ROADMAP/README 的变更记录里留痕。

**Q4：某份文档里写着“待写”，但我现在需要里面的信息怎么办？**
先看 SPEC/TASKS 是否已覆盖；若确实缺失（例如 RUNBOOK 的排障步骤），找对应 owner 要最小可用信息，同时提醒其补文档；不要把临时结论写进别的文档造成二次冲突。

**Q5：我要新增一份文档放哪里？**
`quantdesk/docs/` 下（用户手册类可用中文名后缀 `.md`），符合 `TASKS.md` §5 文件归属；新增后必须更新本文件的文档清单（§3）与维护规则（§5），并在文末加变更记录。

**Q6：从哪里能看到项目现在做到哪了？**
三个地方同步看：`quantdesk/README.md` §4（里程碑进度表）、`docs/ROADMAP.md` §1（总体状态）、团队任务板 + `docs/TASKS.md`（任务级状态）。

---

## 7. 变更记录

| 版本 | 日期 | 变更 | 变更人 |
|---|---|---|---|
| v1.0 | 2026-10-25 | 初版：30 秒结论、权威顺序、5 类文档清单（含状态/维护者/任务映射）、4 条角色阅读路径、维护规则、FAQ | pm-quant |
| v1.1 | 2026-10-25 | 新增 `REVIEW_M0_R1.md` 索引（3.3）；RUNBOOK/USER_GUIDE/ACCEPTANCE_CHECKLIST 状态标注“已安排交付文档维护”；同步 SPEC/TASKS v1.1 口径更新 | pm-quant |
| v1.2 | 2026-10-25 | 按 Lead 口径统一：①RUNBOOK（v1.2）、ACCEPTANCE_CHECKLIST（v1.2）改为**已交付**，路径统一为 `quantdesk/docs/`；②登记 README v1.3 / RUNBOOK v1.2 / ACCEPTANCE v1.2 / SPEC v1.1 / TASKS v1.1 版本；③USER_GUIDE 明确“未交付、后续轮次”；④阅读路径同步（RUNBOOK 已交付） | pm-quant |
| v1.3 | 2026-10-25 | ①REVIEW_M0_R1 升级 v1.1（M0 退出复核版）；②SPEC 登记 v1.2（Dev3 口径：快照扩展/自动交易授权/参数来源/数据缺失 fail-closed）、TASKS 同步 v1.2；③页面文件名统一 `strategies.py`（SPEC §10.4、TASKS M3-05、ROADMAP） | pm-quant |
| v1.4 | 2026-10-25 | **Phase 2 批准落地 + 页面状态表**：①版本同步 BRIEF v0.2、SPEC v1.4、TASKS v1.5、ROADMAP v1.2、GLOSSARY v1.1、REVIEW v1.3；②新增 §3.5「11 页 UI 页面状态表」（已接线/需先拉数据/占位待接 + 用户操作提示），原 §3.5 顺延为 §3.6；③配置/入口文件状态更新（app.py 已交付 11 页、Phase 2 配置与可选依赖待补） | pm-quant |
| v1.5 | 2026-10-25 | **M1 验收登记**：①新增 `REVIEW_M1.md` 索引（M1 退出结论/M6 条件/5 项遗留核验）；②版本同步 SPEC v1.5（不复权价格口径 + 3 个新 RQ 警告码）、TASKS v1.6、REVIEW_M0_R1 v1.4 | pm-quant |
| v1.6 | 2026-10-25 | **M1 修订登记**：①`REVIEW_M1.md` 升级 **v1.1**（不复权真实数据基线、同仓位被动—仅 ma_cross/ml_ranker 略好、ML OOS AUC 0.4456 负结果、M1 结论修订、7 项遗留）；②版本同步 SPEC v1.6（`backtest.price_adjust` 配置键）、TASKS v1.7、REVIEW_M0_R1 v1.5 | pm-quant |
| v1.7 | 2026-10-26 | **用户目标升级 + F14/F15**：①版本同步 BRIEF v0.3（F1–F15）、SPEC v1.7（F14/F15 + `screen.*` + 12 页）、TASKS v1.9（80 项，M6-14…17 P0）、ROADMAP v1.4（M6 17 项，P0 扩为 5 件）、REVIEW_M1 v1.3；②§3.5 页面状态表扩为 **12 页**（新增 `screener.py` = ⏳ 待开发 M6-16）；③登记用户目标“以盈利为目标的选股与自动交易系统”与合规红线（不承诺收益/未过验证不得宣称有效） | pm-quant |
| v1.8 | 2026-10-26 | **收尾对齐（Lead 校准）**：①新增 `REVIEW_FINAL.md`（v1.0，最终状态评审）与 README v1.4、ROADMAP v1.5（M6 暂停 §8.5）、ACCEPTANCE v2.0（52 AC 实际状态）索引；②补录 `USER_GUIDE/DEMO_WALKTHROUGH/STRATEGY_RESEARCH/DATA_SOURCES/DATA_QUALITY_NOTES/SCREENER_VALIDATION/SHORT_TERM_MODEL_V1`，登记 `TEST_REPORT.md` 为待交付（QA）；③§3.5 页面表更新为 **12 页实际状态**（screener 已交付·研究阶段；总览/手动交易/持仓/自动化 已接线；策略/设置/日志 部分），`app.py` 页面数改 12；④RUNBOOK 备份恢复脚本状态更新 | pm-quant |
| v1.9 | 2026-10-26 | **M6 恢复后本轮交付登记**：①新增 `M6_RESEARCH_REPORT.md`（200 只/36 组 OOS 矩阵/统计检验/可执行性/无效判定）、`M6_QA_AUDIT.md`（无未来函数/复权标签/purge-embargo/可复现/gate/数据审计，闭环 F1–F8）、`M6_HANDOFF_PROMPT.md`；②版本同步 `REVIEW_FINAL.md` **v1.1**（M6 进展 + 状态更新）、`SHORT_TERM_MODEL_V1.md` 追加 §7（扩样本复核结论未改变）、`REVIEW_FINAL.md` §2 M6 行由“暂停”改为“本轮完成（仍无可用配置）”；③新增代码 `strategy/m6_research.py`、`scripts/m6_research.py`、`scripts/build_universe_m6.py`、`backtest/validation.py`；新增测试 `tests/test_m6_research.py`（21 用例）、`tests/test_backtest_passive.py`（6 用例）；④P1 回测报告强制“同仓位被动基准 + 样本外标记” | Lead（研究负责人） |
| v1.10 | 2026-10-27 | **v2.0.0 发布登记**：①新增 `CHANGELOG.md`、`VERSION`、`docs/RELEASE_NOTES_v2.0.0.md`、`docs/M6_INTEGRATION.md`、`docs/RELEASE_VERIFICATION_v2.0.0.md`（验证报告，verify-engineer）索引；②README 升 **v2.0**（徽章 Version 2.0.0 / Tests 405 passed、§4.1 M6 研究成果、§4.2 版本与文档）；③登记 `REVIEW_FINAL.md` **v1.1**、`SHORT_TERM_MODEL_V1.md`（§7 扩样本复核）、`M6_QA_AUDIT.md`（F1–F8 闭环 + F9 结论修正）；④版本常量链路统一 2.0.0（config.yaml / core / UI meta 兜底） | 发布工程（release-engineer） |
| v1.11 | 2026-10-27 | **v2.0.1 补丁发布登记**：①新增 `docs/RELEASE_NOTES_v2.0.1.md` 索引；②登记 `docs/RELEASE_VERIFICATION_v2.0.0.md` §9「v2.0.0 → v2.0.1 干净源码复核」（v2.0.0 tarball 干净目录 383 passed/17 skipped/5 errors → 修复后 0 error，最终数字以 §9 复核记录为准）；③`CHANGELOG.md` 升 **v2.0.1**（[2.0.1] 补丁 + [2.0.0] 历史保留）、`VERSION`/README/SPEC 示例同步 2.0.1；④`tests/test_backtest_passive.py` fixture 缺数据由 error 改 skip；不移动已发布 `v2.0.0` tag，M6 负结论不变 | 发布工程（release-engineer） |
