# QuantDesk v2.0.1 补丁发布说明（RELEASE NOTES）

> **版本**：v2.0.1 · **发布日期**：2026-10-27 · **类型**：补丁（PATCH）· **仓库**：[SSSHHH666/quantdesk](https://github.com/SSSHHH666/quantdesk)（`main`）
> **一句话**：修复从 GitHub 下载的发布包在**干净目录**（未构建 `runtime/` 数据）中运行全量测试时，`tests/test_backtest_passive.py` 因缺数据而报 error 的测试健壮性问题。**不改生产代码、不改任何策略/研究结论。**
> **tag 策略**：`v2.0.0` tag 保持不变（**不移动、不删除**）；`v2.0.1` 为新 tag。

---

## 1. 问题

| 项 | 内容 |
|---|---|
| 现象 | v2.0.0 tarball 解压到**干净目录**跑全量测试 → **383 passed / 17 skipped / 5 errors**，errors **全部**在 `tests/test_backtest_passive.py` |
| 原因 | 该文件的 module 级 fixture `real_bars` 依赖运行期数据 `runtime/short_term/bars_50.csv`，而 `runtime/` 按 `.gitignore` **不入库**；fixture 用 `assert` 检查文件存在，干净克隆下直接抛 `AssertionError` → 5 个用例 **error**（而非 skip） |
| 影响面 | 只影响「干净源码 + 未构建数据」的测试体验；不影响已构建数据的本地/用户环境（本机 405 passed / 60 deselected），也不影响 v2.0.0 的任何功能、数据与 M6 研究结论 |

> 这是**测试健壮性缺陷**，不是功能/数据/研究缺陷；修复方式与项目其余「依赖运行期数据的用例」保持一致（数据缺失 → skip）。

---

## 2. 修复

- `tests/test_backtest_passive.py`：module 级 fixture `real_bars` 在 `runtime/short_term/bars_50.csv` 缺失时改为 `pytest.skip(...)`，并给出构建提示（`scripts/build_universe_m6.py` / `scripts/fetch_data.py`）；**构建数据后这些用例自动启用**。
- **不改生产代码**：`strategy/`、`backtest/`、`trading/`、`automation/`、`ui/` 均未改动。
- 版本号统一为 `2.0.1`：`VERSION`、`config.yaml`、`core/config.py`、`core/__init__.py`、UI meta 兜底值、`docs/SPEC.md` 配置示例。

---

## 3. 验证证据

| 场景 | 结果 |
|---|---|
| v2.0.0 tarball 干净目录（修复前） | 383 passed / 17 skipped / **5 errors**（全部在 `tests/test_backtest_passive.py`，缺 `runtime/` 数据） |
| 修复后 main 干净目录 | 全量收集 **405 项、0 error**（依赖运行期数据的用例按 `skip` 处理；最终数字见验证报告 §9 复核记录） |
| 本机完整数据环境（v2.0.0 基线） | **405 passed / 60 deselected** |
| 发版物料静态一致性 | 版本链（VERSION / config / core / UI）一致、文档相对链接可解析（由 Lead 发布前复跑校验） |

> 详细复核见 [`docs/RELEASE_VERIFICATION_v2.0.0.md`](RELEASE_VERIFICATION_v2.0.0.md) §9「发布后干净源码复核（v2.0.0 → v2.0.1）」。
> 说明：干净目录中依赖运行期数据的用例会 **skip**，属设计行为；按 §6 构建数据（约 15 分钟，需网络）后自动运行。

---

## 4. 升级说明

1. **仅版本号 + 测试健壮性变化**：无需数据库迁移、无需改配置、无运行行为变化。
2. **从 v2.0.0 升级**：拉取/切换到 `v2.0.1` tag 即可；`automation.enabled` 仍默认 `false`；研究阶段不接自动化的边界不变。
3. **从干净源码首次使用**：直接跑全量测试不再 error（数据依赖用例 skip）；要运行这些用例，先按 §6 构建数据。
4. **发布物料**：`CHANGELOG.md` 顶部新增 `[2.0.1]`，其余文档保持 v2.0.0 内容不变（历史不删）。

---

## 5. 兼容性

- **无 API 变更**、**无数据库表结构变更**（沿用 28 张业务表 + `schema_version`）、**无配置键变更**。
- **M6 研究结论不变**：日频免费数据上**无可执行短线选股边际**；不接入推荐/自动化；UI 维持「研究阶段 · 不构成买入建议 · 不承诺收益」。
- v2.0.0 的发布内容与 tag 保持不变；本补丁不重写历史。

---

## 6. 复现 / 验证命令

```powershell
cd <你的路径>\quantdesk

# 1) 干净源码（无 runtime 数据）全量测试：数据依赖用例 skip、0 error
.venv\Scripts\python.exe runtime\_run_pytest.py tests -q

# 2) 构建数据后自动启用依赖运行期数据的用例（约 15 分钟，Sina 通道）
.venv\Scripts\python.exe scripts\build_universe_m6.py --limit 200 --candidates 280 --sleep 0.25

# 3) 版本自检（期望 2.0.1）
.venv\Scripts\python.exe -c "from core.config import get_config; print(get_config().app.version)"
```

> 用户本机（非 DSH 沙箱）可直接 `pytest tests -q`；沙箱下 `runtime/_run_pytest.py` 只处理临时目录权限，不改仓库代码/测试。

---

## 7. 已知问题与限制（与 v2.0.0 相同，不变）

- **研究结论**：M6 为负结果（34/36 未过预注册门槛；唯一统计显著信号为不可成交 artifact）→ 不接入推荐/自动化，UI 维持研究阶段。
- **数据偏差**：股票池存在存活者偏差与「事后选池」；单一免费数据源（akshare/Sina），无资金流/龙虎榜/行业/分钟线。
- **运行期产物不入库**：`runtime/`（`bars_*.csv`、`m6_*`、数据库、日志）需本地重建，这也是本补丁要处理「干净克隆缺数据」的原因。
- **历史占位**：设置页保存类接口、审计/日志查询、策略实例创建仍为占位；`docs/TEST_REPORT.md` 仍待 QA 补交。
- **本地仓库无 git**：版本化由 GitHub API 提交；发布以 GitHub tag/release 为准。

---

## 8. 相关文档

- [CHANGELOG.md](../CHANGELOG.md) — 变更日志（`[2.0.1]` 补丁 + `[2.0.0]` 大版本）
- [VERSION](../VERSION) — 当前版本号（`2.0.1`）
- [docs/RELEASE_NOTES_v2.0.0.md](RELEASE_NOTES_v2.0.0.md) — v2.0.0 发布说明（历史）
- [docs/RELEASE_VERIFICATION_v2.0.0.md](RELEASE_VERIFICATION_v2.0.0.md) — 发版验证报告（§9 干净源码复核）
- [docs/M6_INTEGRATION.md](M6_INTEGRATION.md) · [docs/M6_RESEARCH_REPORT.md](M6_RESEARCH_REPORT.md) · [docs/M6_QA_AUDIT.md](M6_QA_AUDIT.md)
- [README.md](../README.md) — 快速开始与使用说明

---

## 9. 免责声明

1. 本项目为**个人研究/学习用途**，**不是投资建议**，**不承诺收益**（UBA）；回测与模拟盘结果不代表未来表现。
2. **不接真实资金**：仅提供模拟盘与券商适配层骨架；自动化默认关闭，可一键停止。
3. M6 研究结论为**负结果**：日频免费数据上未发现可执行的短线选股边际；未经完整验证与可执行性检验的配置**不得**接入推荐/自动化，UI 不得宣称有效。
4. 数据来自公开接口（akshare/Sina），可能存在缺失、延迟或口径差异；第三方依赖许可见 [docs/THIRD_PARTY.md](THIRD_PARTY.md)；本仓库采用 MIT 许可（见 [LICENSE](../LICENSE)）。

---

## 10. 变更记录

| 版本 | 日期 | 变更 | 作者 |
|---|---|---|---|
| v1.0 | 2026-10-27 | 初版：v2.0.1 补丁说明（问题、修复、验证证据、升级说明、兼容性、复现命令、已知问题、免责声明） | 发布工程（release-engineer） |
