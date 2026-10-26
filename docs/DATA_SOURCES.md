# QuantDesk 数据源实测结论（DATA_SOURCES）

> 实测时间：2026-10-25 · 环境：Windows 本机 / Python 3.12 / akshare **1.19.3** / 项目 `.venv`
> 实测方式：逐个调用 akshare 接口，记录成功、耗时、列名与异常；结论用于 `data/akshare_provider.py` 主备链与降级策略。
> **重要结论**：当前网络下**东方财富行情接口（`push2.eastmoney.com` / `push2his.eastmoney.com`）全部 ProxyError**；
> **新浪、腾讯接口可用**。东财「数据中心」域名（`datacenter-web.eastmoney.com`）部分接口可用（分红送配）。

## 1. 接口实测矩阵

| # | akshare 接口 | 数据源 / 主机 | 用途 | 实测 | 耗时 | 失败原因 / 备注 | 替代方案 |
|---|---|---|---|---|---|---|---|
| 1 | `stock_zh_a_daily` | 新浪 `finance.sina.com.cn` | 个股日线（不复权） | ✅ 可用 | ~0.7–1.5s | 成交量单位：股；`turnover` 为小数 | **主源**（`data/akshare_provider.py`） |
| 2 | `stock_zh_a_daily(adjust="hfq-factor")` | 新浪 | 后复权因子序列 | ✅ 可用 | ~0.7s | 返回离散除权日因子，需按日期向后合并 | 主源复权因子；缺失退化为不复权（DQ005） |
| 3 | `stock_zh_a_hist_tx` | 腾讯 `web.ifzq.gtimg.cn` | 个股日线（raw/hfq） | ✅ 可用 | ~5s | 成交量单位：股；`turnover` 为小数 | **备源**；复权因子由 hfq/raw 收盘比推导 |
| 4 | `stock_zh_index_daily` | 新浪 | 指数日线 | ✅ 可用 | ~0.8s | 返回全量历史（约 6000 行），需按区间切片；无成交额 | **指数主源** |
| 5 | `stock_zh_index_daily_tx` | 腾讯 | 指数日线 | ✅ 可用 | ~27s（慢） | 无成交量字段 | 指数备源（仅新浪失败时使用） |
| 6 | `stock_zh_a_hist` | 东财 `push2his.eastmoney.com` | 个股日线 | ❌ 不可用 | — | ProxyError（当前网络无法连接东财 push2his） | 已被新浪/腾讯替代，**不参与主备链** |
| 7 | `index_zh_a_hist` | 东财 `push2.eastmoney.com` | 指数日线 | ❌ 不可用 | — | ProxyError | 新浪/腾讯指数接口 |
| 8 | `stock_zh_a_spot_em` | 东财 `push2.eastmoney.com` | 全市场实时快照（含 PE/PB/总市值） | ❌ 不可用 | — | ProxyError | 新浪快照（慢）→ 最近收盘价兜底 |
| 9 | `stock_zh_a_spot` | 新浪 | 全市场实时快照 | ✅ 可用 | ~18–24s（慢） | 代码带 `sh/sz/bj` 前缀；无 PE/PB | 快照备源（结果缓存 120s） |
| 10 | `stock_bid_ask_em` | 东财 `push2.eastmoney.com` | 盘口买一/卖一 | ❌ 不可用 | — | ProxyError | 无（MVP 不做 L2） |
| 11 | `stock_individual_info_em` | 东财 | 个股资料（市值/行业/上市时间） | ❌ 不可用 | — | ProxyError | 上市日期改用交易所列表接口（#15） |
| 12 | `stock_individual_spot_xq` | 雪球 | 个股快照/估值 | ❌ 不可用 | — | 需要有效 `xq_a_token` 登录态（400016） | 无 |
| 13 | `stock_individual_fund_flow` | 东财 `push2his.eastmoney.com` | 个股资金流 | ❌ 不可用 | — | ProxyError | **资金流数据当前不可得** |
| 14 | `stock_fhps_detail_em` | 东财 `datacenter-web.eastmoney.com` | 分红送配详情 | ✅ 可用 | ~1.8s | 与行情接口不同域名，故仍可用；每 10 股口径 | **分红主源**（`adjust_events`） |
| 15 | `stock_history_dividend_detail` | 新浪 | 历史分红（送股/转增/派息） | ✅ 可用 | ~0.9s | 每 10 股口径；无 RIGHTS 配股 | **分红备源** |
| 16 | `stock_info_sh_name_code` | 上交所 | 沪市股票列表（含上市日期） | ✅ 可用 | ~0.0s | `主板A股` / `科创板` | 标的主数据主源 |
| 17 | `stock_info_sz_name_code` | 深交所 | 深市 A 股列表（含板块/上市日期） | ✅ 可用 | ~0.0s | `A股列表` | 标的主数据主源 |
| 18 | `stock_info_a_code_name` | 新浪/东财聚合 | 全 A 代码+名称 | ✅ 可用但慢 | ~30s | 分页抓取（18 页进度条） | 仅作最后兜底，不用于主路径 |
| 19 | `tool_trade_date_hist_sina` | 新浪 | 交易日历 | ✅ 可用 | ~0.77s | 约 8797 行 | 交易日历主源；也用于 `health_check` 探测 |
| 20 | `stock_a_indicator_lg` | legulegu | 估值指标（PE/PB 历史） | ❌ 不存在 | — | akshare 1.19.3 无该属性（`AttributeError`） | 待评估其它估值接口 |

## 2. 对 QuantDesk 的影响与落地策略

### 已满足（M1 研究闭环核心）
- **个股日线 + 复权因子**：新浪主源（`stock_zh_a_daily` + `hfq-factor`）→ 腾讯备源（`stock_zh_a_hist_tx` raw+hfq 比值推导）→ Mock 兜底；
  回测/行情页所需的 hfq/qfq/none 三种口径可在本地由 `adj_factor` 换算，**不依赖东财**。
- **指数日线**：新浪 `stock_zh_index_daily`（沪深 300/上证/深证/创业板等）。
- **交易日历**：新浪 `tool_trade_date_hist_sina`。
- **标的主数据**：上交所/深交所列表（含上市日期、板块、ST 标记）→ 用于风控 `min_listed_days`/涨跌停幅度。
- **除权除息事件**：东财 datacenter 分红送配（主）→ 新浪历史分红（备）。

### 降级但仍可用
- **实时快照**：东财快照不可用 → 新浪全市场快照（约 18–24 秒，结果缓存 120 秒）→ 最近收盘价兜底；
  UI 会提示"使用最近收盘价/离线数据"。慢是当前网络下可接受的降级（非高频场景）。
- **基本面**：东财快照不可用 → PE/PB/总市值返回空（NaN），多因子策略若依赖估值因子需等待替代数据源；
  其余字段（报告期/来源）正常。**结论：MVP 的多因子策略应优先使用价量因子**，估值因子标记为"数据缺失"。

### 当前不可得（需后续版本解决）
- **个股资金流**（`stock_individual_fund_flow`）：东财 push2his 不可用，无新浪/腾讯等价接口 → 暂不实现资金流因子。
- **盘口/买卖一**（`stock_bid_ask_em`）：东财不可用；MVP 不做 L2，暂不需要。
- **量比**：依赖实时快照的成交量与历史均量，快照降级到新浪时可用；东财不可用不影响（新浪快照含成交量，
  可本地按"当日累计量 / 过去 5 日同时刻均量"近似，属后续增强）。
- **估值历史**（PE/PB 时间序列）：`stock_a_indicator_lg` 在当前 akshare 版本不存在；候选替代接口需另行实测。

## 3. 代码落点

| 能力 | 实现位置 | 备注 |
|---|---|---|
| 主备链组装 | `data/provider.py::build_provider` + `ProviderRegistry.resolve` | 健康检查 OK 用 akshare，DOWN 自动 mock |
| 新浪/腾讯日线 | `data/akshare_provider.py::_fetch_stock_bars` / `_fetch_stock_bars_tencent` | 东财不参与主备链，错误信息中文说明 |
| 指数日线 | `_fetch_index_bars` | 新浪主 → 腾讯备 |
| 交易日历 | `get_trading_calendar` | 缓存 1 小时 |
| 标的主数据 | `get_symbols`（上交所/深交所列表） | 缓存 24 小时 |
| 实时快照 | `get_snapshot`（东财→新浪→收盘价→mock） | 多源补齐缺失标的（指数走收盘价兜底） |
| 分红送配 | `get_adjust_events`（东财 datacenter→新浪） | 失败只 WARNING 跳过，不影响行情 |
| 数据初始化脚本 | `scripts/fetch_data.py` | 缓存优先、重复运行幂等、输出 DQ 报告 |

## 4. 复测方法（供后续环境变化时验证）

```powershell
cd quantdesk
.venv\Scripts\python.exe - <<'PY'
import akshare as ak, time
for name, fn in [
    ("sina_daily", lambda: ak.stock_zh_a_daily(symbol="sh600519", start_date="20240102", end_date="20240110", adjust="")),
    ("tencent_daily", lambda: ak.stock_zh_a_hist_tx(symbol="sh600519", start_date="20240102", end_date="20240110", adjust="")),
    ("eastmoney_hist", lambda: ak.stock_zh_a_hist(symbol="600519", period="daily", start_date="20240102", end_date="20240110", adjust="")),
    ("sina_index", lambda: ak.stock_zh_index_daily(symbol="sh000300")),
    ("calendar", lambda: ak.tool_trade_date_hist_sina()),
    ("dividend_em", lambda: ak.stock_fhps_detail_em(symbol="600519")),
    ("dividend_sina", lambda: ak.stock_history_dividend_detail(symbol="600519", indicator="分红")),
]:
    t = time.time()
    try:
        r = fn(); print(f"[OK] {name} {getattr(r,'shape',None)} {time.time()-t:.1f}s")
    except Exception as exc:
        print(f"[FAIL] {name} {type(exc).__name__}: {exc}")
PY
```

> 说明：数据源可用性受本机网络/代理影响；若未来东财恢复，可在 `data/akshare_provider.py` 中把东财接口加回备源
> （解析函数 `_normalize_em_stock` 已保留），并按本文档更新实测结论。

## 5. P0 数据完整性修复记录（2026-10-25）

**问题**：历史版本在 akshare 失败时会用 MockProvider 假数据兜底并写入 `daily_bars`，与真实行情混存
（600519.SH mock 262 行 + akshare 436 行），回测跨区间拼接近似价格会算出 **246% 假收益**。

**代码根治**（已交付）：
1. `data/cache.py::BarCache.update`：`provider=MockProvider` 时**直接拒绝写入**（`UpdateReport.mock_blocked`）；
   akshare 单标的降级产生的 mock 行也逐行拦截，绝不进入 `daily_bars`；
2. `data/akshare_provider.py::get_bars` / `build_provider`：降级数据 `source='mock'`，日志明确「离线 Mock，不写缓存、不可用于真实回测」；
3. `BarCache.load`：同一标的请求区间内若存在多个 `source` → **fail-closed 抛中文错误**（不再拼接假收益）；
4. `scripts/purge_mock_cache.py`：一键清理 `daily_bars`/`adjust_events` 中 mock 行（幂等，支持 `--dry-run`）；
5. `docs/RUNBOOK.md` §9：运维章节记录清理与回填步骤。

**清理与回填结果**（`runtime/quantdesk.db`，2026-10-25 22:14）：

| 标的 | 首日 | 末日 | 行数 | 缺口天数 | 数据源 |
|---|---|---|---|---|---|
| 000001.SZ | 2021-10-25 | 2026-10-23 | 1212 | 0 | akshare |
| 300750.SZ | 2021-10-25 | 2026-10-23 | 1212 | 0 | akshare |
| 600519.SH | 2021-10-25 | 2026-10-23 | 1212 | 0 | akshare |

- `daily_bars`：**3636 行，全部 `source=akshare`**（无任何 mock）；`adjust_events`：86 行，全部 `akshare/eastmoney`；
- `validate()`：无 DQ001–DQ007 问题；各标的起始日一致、无缺口、无混源；
- 600519.SH 2024-01-02 ~ 2025-12-31 买入持有：**后复权 -12.14% / 不复权 -18.27%**（真实行情量级，与新浪原始行情可核对，不再出现假收益）；
- 覆盖报告：`runtime/exports/data_coverage.md`（脚本 `scripts/fetch_data.py` 每次运行自动生成）。

## 6. 建表与 Schema v2 迁移校验（R6，Lead 已确认口径 2026-10-26）

**契约口径**：SPEC v1.6 §5.4 实键为准 —— **22 张核心表（CF-7 冻结，字段/语义不变）+ 6 张扩展表 = 28 张业务表**；
另有 `schema_version` 版本表（不计入业务表）。扩展表清单：

```
watchlist_groups, watchlist_items, recommend_runs, recommend_items, research_runs   # SPEC §5.4 五张
market_snapshot                                                                     # R6-P0 全市场快照
```

**`init_db` 期望表数 = 28 的校验命令**（在 quantdesk 目录执行）：

```powershell
.venv\Scripts\python.exe -c "from sqlalchemy import inspect; from core.db import get_engine; from core.models import ALL_TABLE_NAMES; t=set(inspect(get_engine()).get_table_names()); b=t-{'schema_version'}; print('business tables:', len(b), '| expected:', len(ALL_TABLE_NAMES)); assert len(b)==28 and set(ALL_TABLE_NAMES) <= t; print('OK: 28 business tables + schema_version')"
```

**schema_version v2 迁移**（`core/db.py::MIGRATIONS[2]`）：为 v1 库补齐 6 张扩展表、补齐默认自选池分组，
并把早期独立版 `watchlist_items`（symbol 主键）自动迁移为规范结构（保留数据、清理旧表）；
迁移前自动备份到 `runtime/backups/`。实测 runtime 库迁移成功，3636 行真实日线无损。

**mock 污染清理脚本兼容性**：`scripts/purge_mock_cache.py --dry-run` 在 28 表结构下实测通过
（`daily_bars`/`adjust_events` 待清理 0 行，未修改数据库）。

## 7. symbols 元数据修复（P1，2026-10-26）

**问题**：`symbols` 表曾大面积错值（深市 exchange 记 `SH`、创业板/科创板 board 记 `MAIN`），
下游若用 `symbols.board` 判板块会把 ±20% 涨跌停当 ±10%。

**口径**：`exchange/board/price_limit_pct/lot_size` **一律按代码前缀重算**（`data/symbols.py`）：
600/601/603/605/688/689→SH；000/001/002/003/300/301/399/200→SZ；4/8/920→BJ；
300/301→GEM、688/689→STAR、北交所→BSE；涨跌停 MAIN 10%（ST 5%）/GEM·STAR 20%/BSE 30%；
科创板最小 200 股，其余 100 股。`AStockRules`（`backtest/broker_sim.py::guess_board`）同样**只看代码前缀、不读 symbols 表**，
因此撮合/回测/风控链路不受该错值影响，本次修复服务于 UI 展示与未来筛选/统计。

**修复与校验命令**（幂等）：

```powershell
.venv\Scripts\python.exe scripts\fetch_data.py --repair-symbols   # 批量修正（报告修正条数/抽样/剩余不一致）
.venv\Scripts\python.exe -m data.symbols                          # 自检：构造错值→修复→抽样+全表扫描一致率 100%
```

**实测结果（runtime 库）**：修正 **5576 条**（exchange=3259、board=2381、price_limit=5576、lot_size=617），
修复后分布 SH 2317 / SZ 2906 / BJ 353；MAIN 3195 / GEM 1411 / STAR 617 / BSE 353；
抽样 600519.SH(SH/MAIN/10%)、000001.SZ(SZ/MAIN/10%)、300750.SZ(SZ/GEM/20%)、688111.SH(SH/STAR/20%/200 股) 全部正确；
全表扫描与代码前缀一致率 **100%**（剩余不一致 0）。

**stale DQ003 清理**：`scripts/fetch_data.py --purge-stale-dq` 删除 mock 事故残留的
`600519.SH 2025-01-02 DQ003`（按当前真实数据复算涨幅 2.36% < 20% 阈值），并写审计
`audit_logs.action=data.quality.purge`（actor=manual），幂等。

