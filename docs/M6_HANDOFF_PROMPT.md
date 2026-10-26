# QuantDesk · M6 研究阶段接续提示词

> 用法：在新会话中把「一、可直接粘贴的提示词」整段发给智能体即可继续 M6。
> 本文档存放完整上下文，便于新会话按需查阅。
>
> **状态更新（2026-10-26 本轮已执行）**：下文提示词中的 P0-1…P0-5 与 P1 已在本轮完成——
> 股票池扩到 **200 只**（349,466 行、覆盖审计 13/13 PASS）、36 组 walk-forward OOS 矩阵完成、
> QA 独立审计 PASS、P1 被动基准标准化；**结论：无可执行短线选股边际**（34/36 未过门槛；
> 2 组统计 PASS 均为“隔夜缺口”且经涨停不可买检验判定为不可成交 artifact）。
> 最终结论与复现命令见 **`docs/M6_RESEARCH_REPORT.md`**，审计见 `docs/M6_QA_AUDIT.md`。
> 本文件保留为“下一轮研究（换数据/微观结构/时点股票池）”的上下文模板。

---

## 一、可直接粘贴的提示词（复制这一段）

```
我是 QuantDesk 项目的负责人。请继续推进 M6「策略研究增值」阶段。项目在本机：
C:\Users\shanghao\Documents\deepseek-harness\default-workspace\quantdesk
GitHub 仓库：https://github.com/SSSHHH666/quantdesk（已上传，main 分支）
GitHub token 存在 C:\Users\shanghao\.dsh\github_token.dpapi（DPAPI 加密，勿打印）。

【先读这些文件（按顺序）】
1. quantdesk/docs/REVIEW_FINAL.md（项目最终状态、已验证能力、研究暂停项）
2. quantdesk/docs/SHORT_TERM_MODEL_V1.md（短线模型 v1 负结果与暂停原因）
3. quantdesk/docs/SCREENER_VALIDATION.md（全市场筛选器验证：RankIC -0.029/-0.030/-0.037，Top 组无超额）
4. quantdesk/docs/STRATEGY_RESEARCH.md（交易员/流派信号库与优先级）
5. quantdesk/docs/ROADMAP.md §8.5（M6 恢复条件与 P0 顺序）
6. quantdesk/docs/BRIEF.md v0.3、SPEC v1.7（F14/F15 验收标准）、TASKS v1.9（M6-01..M6-17）
7. quantdesk/docs/GLOSSARY.md（术语）

【硬约束（违反即作废）】
- 只用日频、免费、可获取的数据：新浪（主）/腾讯（备）；东方财富接口在本机网络不可用（ProxyError），不要依赖。
- 严禁未来函数：所有特征与标签必须用 as_of 当日及以前数据；标签用未来 N 日收益时必须严格 shift，walk-forward 训练窗口加 purge/embargo（≥5 个交易日）。
- Mock 数据绝不入库、绝不参与真实回测；缓存混源必须 fail-closed。
- 回测撮合/手数/资金一律用不复权真实价（config.backtest.price_adjust=none）。
- 不承诺收益、不美化结果；无效就写无效。未通过样本外（RankIC>0.03 且 Top 组 10 日扣费后超额>0 且命中率显著高于基础概率）不得接入推荐/自动化，也不得在 UI 宣称有效。
- 个人研究用途，不接真实资金。

【当前基线（必须作为对照）】
- 短线模型 v1：3 只股票、48 折 walk-forward，OOS 命中率 24.83%（= 基础概率，零排序能力），扣费期望 -0.22%，最大回撤 -32.41%，usable=False。
- 全市场筛选器：RankIC -0.029/-0.030/-0.037（5/10/20 日），Top 组超额不显著 → 无效。
- 样本内真实回测（2025-01~2026-10，3 标的、100 万）：ma_cross -0.11%、value_quality -2.74%、breakout_abnormal -2.63%、ml_ranker -1.53%；同仓位被动 -1.98%。仅 ma_cross 与 ml_ranker 略好于被动，全部小幅负收益。
- 测试基线：378 passed / 0 failed（`pytest tests -q`，约 40 秒）。

【已有数据资产（直接用，不要重复下载）】
- quantdesk/data/universe_50.json：50 只流动性好的 A 股（含 board/名称）
- quantdesk/runtime/short_term/bars_50.csv：50 只 × 2019-01-02~2026-10-23 的日线（约 8.9 万行，含不复权 OHLCV + 后复权因子）
- quantdesk/runtime/quantdesk.db：daily_bars 3,639 行（3 只标的全量真实日线，2021-10~2026-10，零 mock）
- 数据层工具：data/cache.py（增量/校验/混源拒绝）、data/akshare_provider.py、data/market_snapshot.py（全市场快照 5576 只）、data/symbols.py（名称/板块）、scripts/fetch_data.py、scripts/verify_data.py、scripts/purge_mock_cache.py

【M6 任务（按优先级）】
P0-1 扩样本与数据：把股票池从 50 只扩到 150~300 只（流动性过滤，排除 ST/次新/北交所），补齐 2019 起日线（不复权+复权因子），覆盖审计（缺口/复权/停牌/来源唯一）；产出 runtime/short_term/bars_N.csv 与覆盖报告。
P0-2 标签工程（至少试 4 种并对比）：未来 3/5/10 日收益 ≥2%；未来 5 日截面分位 Top20%（排序）；收益三分类（跌/平/涨）；次日跳空方向。必须报告各类别的样本量与不平衡处理。
P0-3 特征工程：在现有 18 个量价特征基础上，增加市场状态（沪深300 20/60 日动量与波动分位）、相对强弱（vs 市场/板块近似）、涨停跌停邻近度、连续放量天数、跳空、ATR 分位、换手率分位、量价背离；严格 as_of 对齐。
P0-4 模型与验证：logistic → HistGradientBoosting / 排序目标（按日期分组）；walk-forward（滚动训练，purge≥5 日，样本外只评估）；输出 OOS：命中率、扣费期望、Top-k 组合净值与最大回撤、多空分层（Top-Bottom）、RankIC/ICIR、特征稳定性。
P0-5 结论与产出：`docs/M6_RESEARCH_REPORT.md`（实验矩阵/负结果也写/结论/下一步）；若有通过的配置，接入 strategy/recommend.py 的「全市场短线雷达」并加 UI 标注（研究阶段 → 已验证），否则维持研究阶段且不得宣称有效。
P1 复用：把「同仓位被动基准 + 样本外」做成回测报告标准输出；补充回归测试（无未来函数、可复现、性能）。

【工作方式】
- 建议用多智能体：研究负责人（需求与验收）/数据工程师（扩样本与校验）/建模工程师（特征+模型+验证）/QA（无前视与可复现测试）。同一文件不要多人并行写。
- 每完成一个阶段就落盘文档与结果文件（不要只留在对话里）。
- 汇报格式：做了什么 / 数据与参数 / OOS 指标 / 结论（可用/无效/边缘）/ 下一步。
- 最终必须回答一个问题：**在日频免费数据上，是否存在统计上可信的短线选股边际？** 有就给证据，没有就明确说没有。
```

---

## 二、给新会话的最小上下文摘要（不给提示词时也可用）

| 项 | 值 |
|---|---|
| 项目根 | `C:\Users\shanghao\Documents\deepseek-harness\default-workspace\quantdesk` |
| 解释器 | `quantdesk\.venv\Scripts\python.exe`（Python 3.12） |
| 启动界面 | 双击 `scripts\start.bat`（端口 8501，被占用自动切换） |
| 测试 | `pytest tests -q` → 378 passed |
| 数据体检 | `python scripts\verify_data.py` → PASS |
| 发版自检 | `python scripts\release_check.py` → PASS 11 |
| GitHub | https://github.com/SSSHHH666/quantdesk |
| 结论基线 | 短线 v1 无效（零排序能力）、筛选器无效（RankIC 负） |

---

## 三、复查清单（新会话第一天建议先跑一遍）

```powershell
cd C:\Users\shanghao\Documents\deepseek-harness\default-workspace\quantdesk
.venv\Scripts\python.exe -m pytest tests -q          # 期望 378 passed
.venv\Scripts\python.exe scripts\verify_data.py      # 期望 PASS
.venv\Scripts\python.exe scripts\release_check.py    # 期望 PASS 11 / FAIL 0
```

确认无误后再动研究代码；任何改动前先 `git` 不存在也没关系（本机未装 git，GitHub 用 API 推送）。

---

## 四、常见陷阱

1. **东财接口不可用**（`push2his.eastmoney.com` / `push2.eastmoney.com` ProxyError）→ 用新浪 `stock_zh_a_daily` / 腾讯 `stock_zh_a_hist_tx`。
2. **akshare 无超时**：批量拉取要设 `socket.setdefaulttimeout(15)` 或加超时包装，否则单只可挂死。
3. **hfq 合成价不能用于手数/资金**：回测默认 `price_adjust=none`。
4. **mock 不落库**：`build_provider()` 降级 mock 时只内存使用；缓存混源会抛 `DataIntegrityError`。
5. **样本太小会得出假结论**：3 只股票、单一区间、样本内结果都不能作为"有效"证据。
6. **公告/财报/龙虎榜/资金流/行业数据当前不可得**：不要设计依赖它们的策略，或明确标注不可回测。
