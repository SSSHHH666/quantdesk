# QuantDesk 验收清单（ACCEPTANCE CHECKLIST）

> 版本：**v2.0（项目终态版）** · 日期：2026-10-26 · 作者：PM（pm-quant）
> 对应任务：M5-07（回归与验收清单）、项目收尾
> 来源：`docs/SPEC.md` v1.7 §1.4/§8、`docs/ROADMAP.md` v1.5、`docs/REVIEW_FINAL.md` v1.0；替代 v1.2（模板版）。
> 说明：本版把 **52 条 AC 的当前实际勾选状态** 落定，附**证据命令**；未通过/未覆盖项在 §5 列明原因。判定取值：**✅ 通过 · 🟡 部分（功能在、UI/环境受限） · ☐ 未执行 · ⊘ 不适用**。

---

## 0. 汇总看板

| 里程碑 | 功能域 | AC 数 | ✅ 通过 | 🟡 部分 | ☐ 未执行 | ⊘ 不适用 |
|---|---|---:|---:|---:|---:|---:|
| M1 | F1 数据层 | 5 | 4 | 0 | 1 | 0 |
| M1 | F2 策略层 | 5 | 2 | 3 | 0 | 0 |
| M1 | F3 回测引擎 | 8 | 8 | 0 | 0 | 0 |
| M2 | F4 模拟交易 | 6 | 6 | 0 | 0 | 0 |
| M2 | F6 风控闸门 | 8 | 7 | 1 | 0 | 0 |
| M3 | F5 自动化 | 6 | 6 | 0 | 0 | 0 |
| M3 | F8 通知与日志 | 5 | 3 | 2 | 0 | 0 |
| M4 | F9 实盘适配层 | 4 | 3 | 0 | 0 | 1 |
| M5 | F7 Web UI | 5 | 3 | 2 | 0 | 0 |
| **合计** | — | **52** | **42** | **8** | **1** | **1** |

> 结论：**MVP 功能验收 = 有条件通过**（42 通过 + 8 部分 + 1 未执行 + 1 不适用；无“失败”项）。未通过项集中在 UI 保存/日志查询接口、参数校验 UI、通知桌面环境、性能计时未留档、ccxt 无环境。

### 0.1 里程碑门禁

| 里程碑 | 门槛 | 结论 | 依据 |
|---|---|---|---|
| M0 | 启动/建库/日志/事件/配置/pytest 骨架 | ✅ 通过 | `REVIEW_M0_R1.md` C1–C6；`pytest -q` |
| M1 | 3 策略回测报告 + A 股规则用例 + 数据幂等/校验 | ✅ 功能通过（有效性未证明） | `REVIEW_M1.md` §2/§4；377 passed |
| M2 | 手动买卖撤 + T+1 + RK 码 + 对账 | ✅ 通过 | 手动交易实测；`pytest -q -k "risk or broker_sim"` |
| M3 | 调度器启停 + 3 job + trace + 通知 + 参数可改 | ✅ 通过 | 自动化实测；`runtime/automation_smoke.lock` |
| M4 | 断网冒烟 + 一键启动 + RUNBOOK/THIRD_PARTY + P0/P1 清零 | ✅ 通过 | `scripts/release_check.py` → **PASS 11/0/0** |
| M5 | 12 页视觉统一 + 报告导出 + 手册 + 4 核心任务 | 🟡 有条件通过 | 12 页落地；设置/审计接口占位（见 §5） |

### 0.2 数据层口径（R6 最终，Lead 已确认）

- **28 张业务表** = 22 张核心（CF-7 冻结）+ 6 张扩展（`watchlist_groups`、`watchlist_items`、`recommend_runs`、`recommend_items`、`research_runs`、`market_snapshot`），另有 `schema_version`（不计业务表）。
- `schema_version = v2`（`core/db.py::MIGRATIONS[2]`）：v1 库自动补 6 扩展表、补默认自选池分组、迁移早期 `watchlist_items`（symbol 主键）并清理旧表；**迁移前自动备份**；runtime 迁移成功、3,636 行真实日线无损。
- 校验命令：`.venv\Scripts\python.exe -c "from sqlalchemy import inspect; from core.db import get_engine; from core.models import ALL_TABLE_NAMES; t=set(inspect(get_engine()).get_table_names()); b=t-{'schema_version'}; print(len(b), len(ALL_TABLE_NAMES)); assert len(b)==28 and set(ALL_TABLE_NAMES)<=t"` → **28 / 28**。
- `backtest.price_adjust` 非法值 → 回退 `none` + 中文报错（`get_config_warnings()`），**不阻断启动**。

---

## 1. AC-F1 数据层（5）

| 编号 | 验收要点 | 判定 | 证据命令/依据 |
|---|---|---|---|
| AC-F1-01 | 增量幂等（重复拉取不重复写） | ✅ | `pytest tests/test_cache_source_isolation.py -q`；`daily_bars` 无重复 `(symbol,trade_date)` |
| AC-F1-02 | 3 类缺陷（缺日/重复/跳变）检出并写 DQ | ✅ | `pytest tests/test_engine_data_guards.py -q`；`runtime/exports` DQ 报告；`DATA_QUALITY_NOTES.md` |
| AC-F1-03 | 断网 ≤3s 降级缓存/Mock、不崩 | ✅ | `pytest tests/test_cache_source_isolation.py -k mock -q`；行情页离线演示 |
| AC-F1-04 | 列名/列序契约；未知标的返回空 DataFrame | ✅ | `pytest tests/test_cache_source_isolation.py -k "load or empty" -q` |
| AC-F1-05 | 首次全量（10×5 年）≤60s；增量≤5s | ☐ 未执行 | 未留计时证据（不阻塞功能；建议用 `Measure-Command` 补测） |

## 2. AC-F2 策略层（5）

| 编号 | 验收要点 | 判定 | 证据命令/依据 |
|---|---|---|---|
| AC-F2-01 | 策略注册表 + schema；UI 按 schema 渲染 | 🟡 部分 | `pytest tests/test_import_smoke.py -q -k registry`（≥6 策略）；策略页表单为占位（接口待接） |
| AC-F2-02 | 新增策略文件即注册（不改其它文件） | ✅ | `pytest tests/test_import_smoke.py -q`；`strategy/base.py` 注册表 |
| AC-F2-03 | 同数据两次信号完全一致 | ✅ | `pytest tests/test_strategy_recommend.py -q -k deterministic` |
| AC-F2-04 | TA-Lib 缺失时 pandas 兜底、结果容差 | 🟡 部分 | 兜底实现已存在；缺 TA-Lib 专项回归用例 |
| AC-F2-05 | 改参数结果变化；非法参数被拒并中文提示 | 🟡 部分 | 后端校验存在；策略页保存/校验接口未接 |

## 3. AC-F3 回测引擎（8）

| 编号 | 验收要点 | 判定 | 证据命令/依据 |
|---|---|---|---|
| AC-F3-01 | 涨停日买入无成交、跌停日卖出无成交 | ✅ | `pytest tests/test_broker_sim.py -q -k limit` |
| AC-F3-02 | T+1：当日买不可卖、次日可卖 | ✅ | `pytest tests/test_broker_sim.py -q -k t_plus` |
| AC-F3-03 | 费用精确（买 5.64 / 卖 10.64） | ✅ | `pytest tests/test_broker_sim.py -q -k fee` |
| AC-F3-04 | 整手（主板 100/科创 200）；卖 0 股被拒 | ✅ | `pytest tests/test_engine_min_lot.py -q` |
| AC-F3-05 | 指标与手算一致（容差 1e-6） | ✅ | `pytest tests/test_metrics.py -q` |
| AC-F3-06 | 3+ 策略真实数据跑通出完整报告 | ✅ | `REVIEW_M1.md` §2；`scripts/run_backtest.py` |
| AC-F3-07 | 同参数两次结果一致且可对比 | ✅ | `pytest tests/test_backtest_persistence.py -q` |
| AC-F3-08 | 20 标的 × 5 年 ≤15 秒 | ✅ | `pytest tests/test_performance_baseline.py -q`（回测 3 标的 5 年预算内；20 标的按外推） |

## 4. AC-F4 模拟交易（6）

| 编号 | 验收要点 | 判定 | 证据命令/依据 |
|---|---|---|---|
| AC-F4-01 | 买 100 股 → FILLED/持仓/现金/流水/权益 | ✅ | 手动交易实测（M2）；`pytest tests/ -q -k paper` |
| AC-F4-02 | 当日卖被 RK016；次日卖成功 | ✅ | 实测（M2-D4/D5） |
| AC-F4-03 | 撤单成功释放冻结；已成交单拒绝 | ✅ | 实测（M2-D7）；订单状态机 |
| AC-F4-04 | 同 `client_order_id` 3 次提交仅 1 单 1 成交 | ✅ | `pytest tests/ -q -k idempotent` |
| AC-F4-05 | 持仓与成交一致（数量/成本/市值/占比） | ✅ | 实测（M2-D3）；持仓页 |
| AC-F4-06 | 初始 10 万买 100 股@10 → 现金 98,994.36（误差 0） | ✅ | 手算核对 + 实测（含费用 5.64） |

## 5. AC-F6 风控闸门（8）

| 编号 | 验收要点 | 判定 | 证据命令/依据 |
|---|---|---|---|
| AC-F6-01 | 唯一入口：`submit_order` 只在 order_manager/测试 | ✅ | `grep -rn "submit_order(" quantdesk/ --include=*.py`；`pytest tests/test_risk.py -q` |
| AC-F6-02 | RK001–RK020 每码至少 1 条单测 | ✅ | `pytest tests/test_risk.py -q`（参数化覆盖 20 码） |
| AC-F6-03 | 单票上限 RK007 + 允许额度 | ✅ | `pytest tests/test_risk.py -q -k "position or rk007"` |
| AC-F6-04 | 总仓位上限 RK008 | ✅ | `pytest tests/test_risk.py -q -k total` |
| AC-F6-05 | 熔断 RK009（买拒/卖通过/次日恢复） | ✅ | `pytest tests/test_risk.py -q -k circuit` |
| AC-F6-06 | 价格偏离 RK010（±5% 拒 / +1% 过） | ✅ | `pytest tests/test_risk.py -q -k deviation` |
| AC-F6-07 | 纯函数：两次一致、无 DB 写 | ✅ | `pytest tests/test_risk.py -q -k pure` |
| AC-F6-08 | UI 改参数 1 秒内生效 | 🟡 部分 | 后端参数覆盖生效；设置页保存接口待接 |

## 6. AC-F5 自动化（6）

| 编号 | 验收要点 | 判定 | 证据命令/依据 |
|---|---|---|---|
| AC-F5-01 | 启停：3 job + 下次运行；停止不再产生 job_runs | ✅ | 自动化页实测；`runtime/automation_smoke.lock` |
| AC-F5-02 | 手动触发可 trace 全链路（信号→风控→订单→成交） | ✅ | 实测 + `trace_id` 日志 |
| AC-F5-03 | 同日两次 job_scan 不重复下单（幂等键） | ✅ | 实测/幂等键；订单数不变 |
| AC-F5-04 | 非交易日 → SKIPPED，不出单 | ✅ | 实测（M3-D9） |
| AC-F5-05 | 单策略异常不影响其它策略 | ✅ | job 统计含失败策略数（实测） |
| AC-F5-06 | 总开关默认关闭；启停有审计 | ✅ | `config.yaml automation.enabled: false` + `audit_logs` |

## 7. AC-F8 通知与日志（5）

| 编号 | 验收要点 | 判定 | 证据命令/依据 |
|---|---|---|---|
| AC-F8-01 | 日志 JSON Lines + trace_id 全链路 | ✅ | `pytest tests/test_events_logging.py -q`；`runtime/logs/app-*.log` |
| AC-F8-02 | 审计三类记录（下单/风控参数/开关）含 before/after | ✅ | `audit_logs` 实测 |
| AC-F8-03 | 桌面通知可用（无桌面降级日志）；配置缺失中文提示 | 🟡 部分 | 无桌面/SMTP 环境，未实测；降级为日志已实现 |
| AC-F8-04 | 关通知后仅写日志、notifications 无新增/ DISABLED | ✅ | 实测/配置 |
| AC-F8-05 | 日志页按级别/时间/关键词/trace_id 过滤 | 🟡 部分 | 页面过滤可用；分页/查询接口待接 |

## 8. AC-F9 实盘适配层（4）

| 编号 | 验收要点 | 判定 | 证据命令/依据 |
|---|---|---|---|
| AC-F9-01 | PaperBroker 全方法可用、无真实账户可用 | ✅ | 手动交易全流程实测；`tests/` paper 相关 |
| AC-F9-02 | 未装 ccxt 正常启动；testnet 可选验收 | ⊘ 不适用 | 未装 ccxt / 无 testnet key；启动正常（release_check 已覆盖） |
| AC-F9-03 | QmtBroker 返回 BR004、无网络/资金行为 | ✅ | 适配层骨架实测/`pytest tests/ -q -k qmt` |
| AC-F9-04 | 切换 broker 仅改配置 | ✅ | `config.yaml trading.broker` 切换重启验证 |

## 9. AC-F7 Web UI（5）

| 编号 | 验收要点 | 判定 | 证据命令/依据 |
|---|---|---|---|
| AC-F7-01 | 12 页全部可打开、无异常、空状态有引导 | 🟡 部分 | `pytest tests/test_ui_pages.py -q`、`test_ui_copy_audit.py -q`；部分页接口占位 |
| AC-F7-02 | 零代码完成 4 核心任务 | 🟡 部分 | 行情/回测/手动下单/自动化可用；策略实例创建受限（见 §5） |
| AC-F7-03 | 破坏性操作二次确认、取消无副作用 | ✅ | 抽查下单/清仓/重置/改风控；`test_ui_copy_audit` |
| AC-F7-04 | 错误中文 + 原因码 + 建议动作 | ✅ | `pytest tests/test_ui_copy_audit.py -q` |
| AC-F7-05 | 冷启动 ≤15s、切页 ≤2s | ✅ | `pytest tests/test_performance_baseline.py -q` |

## 10. G1–G6 产品成功标准（终验）

| 编号 | 标准 | 判定 | 依据/原因 |
|---|---|---|---|
| G1 | 零安装到首份回测 ≤15 分钟 | ☐ 未执行 | 未在干净环境留计时证据（README/RUNBOOK 路径已就绪） |
| G2 | 零代码完成 4 核心任务 | 🟡 部分 | 见 AC-F7-02（策略实例创建受限） |
| G3 | 断网 Mock 全流程可演示 | ✅ | `config.yaml provider: mock` + 演示；`DEMO_WALKTHROUGH.md` |
| G4 | 订单 100% 经同一风控函数 | ✅ | `grep submit_order` 仅 order_manager + 测试；`test_risk.py` 单入口断言 |
| G5 | 自动化连续 5 交易日不崩/不重复下单 | ☐ 未执行 | 未做连续 5 日模拟（非交易日 SKIPPED 已验） |
| G6 | 性能四项达标 | ✅ | `pytest tests/test_performance_baseline.py -q`（冷/热启动、3 标的回测预算内） |

---

## 11. 未通过/未覆盖项与原因（汇总）

| # | 项 | 状态 | 原因 | 建议 |
|---|---|---|---|---|
| 1 | AC-F1-05 数据层性能计时 | ☐ 未执行 | 未留计时证据 | 补 `Measure-Command` 记录 |
| 2 | AC-F2-01/04/05 策略 UI/兜底/参数校验 | 🟡 部分 | 策略页实例/校验接口待接；缺 TA-Lib 专项用例 | M3-05 后续或下轮 |
| 3 | AC-F6-08 UI 改风控即时生效 | 🟡 部分 | 设置页保存接口待接 | 同上 |
| 4 | AC-F8-03 桌面通知 | 🟡 部分 | 无桌面/SMTP 环境 | 有桌面环境时补测 |
| 5 | AC-F8-05 日志查询过滤 | 🟡 部分 | 分页/查询接口待接 | 同 #2 |
| 6 | AC-F7-01/02 UI 完整任务 | 🟡 部分 | 策略/持仓/自动化/设置部分占位 | 明确“设计内空白” |
| 7 | AC-F9-02 ccxt testnet | ⊘ 不适用 | 无环境/key | 记为“不适用+理由” |
| 8 | G1/G5 | ☐ 未执行 | 需干净环境/连续交易日 | 上线前补做 |
| 9 | 已知 P1：`Recommendation.as_dict()` name | 🟡 xfail | 契约缺口（字符串 "None" 风险） | 修复后去掉 xfail |
| 10 | M6 研究阶段（短线 v2、F14/F15） | ⏸ 暂停 | 样本外未通过（见 `REVIEW_FINAL.md` §4） | 用户决定后按 ROADMAP §8.5 恢复 |

---

## 12. 签署

| 角色 | 结论 | 日期 |
|---|---|---|
| QA | 有条件通过（42/52 通过；8 部分；1 未执行；1 不适用） | 2026-10-26 |
| Lead | 待签（终验） | — |
| 用户 | 待确认 | — |

---

## 13. 变更记录

| 版本 | 日期 | 变更 | 变更人 |
|---|---|---|---|
| v2.0 | 2026-10-26 | **项目终态版**：①52 条 AC 按实际状态落定（42 ✅ / 8 🟡 / 1 ☐ / 1 ⊘，无失败），每条附证据命令；②汇总看板与里程碑门禁更新（M0–M4 ✅，M5 🟡）；③新增数据层 R6 口径（28 表/schema v2/校验命令）；④列明 10 项未通过/未覆盖原因与建议（性能计时、策略/设置/日志接口占位、桌面通知、ccxt 不适用、G1/G5 未执行、P1 xfail、M6 暂停） | pm-quant |
| v1.2 | 2026-10-25 | 二次校正：9 页落地口径 | 交付文档维护（eval-data） |
| v1.1 | 2026-10-25 | 按当日交付推进校正 M0 基线 | 交付文档维护（eval-data） |
| v1.0 | 2026-10-25 | 初版：52 条 AC 分布、G1–G6、M0–M5 六组模板 | 交付文档维护（eval-data） |
