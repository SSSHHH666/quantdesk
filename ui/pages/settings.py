"""页面：设置（M3-07 骨架）。

职责：6 个 Tab —— 数据源 / 风控 / 通知 / 存储与备份 / 环境 / 关于。
所有破坏性操作（保存风控参数、恢复默认、备份恢复、清理回测记录）均二次确认，
保存失败需回滚旧值并给中文提示（M3-07 验收）。

数据来源（待接入；本文件不写业务逻辑，不直接写库、不直接改配置文件）：
    core.config.load_config / save_config（M0-02 / M3-07）
    DataProvider.health_check（连通性测试）
    RiskConfig 读写（保存需写审计 before/after）
    Notifier.send（发送测试）
    core.db.backup / restore / 清理回测记录 / 数据库大小
    scripts.check_env.py（依赖检查；页面只做只读探测）
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

import pandas as pd
import streamlit as st

from . import _common as C

PAGE_TITLE = "设置"
PAGE_ICON = "⚙️"
PAGE_ORDER = 8

TODO_ITEMS = [
    "core.config.load_config / save_config（配置文件读写 + 校验，M0-02/M3-07）",
    "DataProvider.health_check()（连通性测试按钮）",
    "RiskConfig 保存 + 审计 before/after + 即时生效（M2-04/M3-07）",
    "Notifier.send（发送测试）",
    "core.db 备份/恢复/清理回测记录/数据库大小（M0-04）",
    "scripts/check_env.py 依赖检查结果（页面只读 env_check 探测，不执行脚本）",
]

RISK_ROWS = [
    ("max_position_per_symbol_pct", "单票仓位上限", "%", "超过则拒单 RK007"),
    ("max_total_position_pct", "总仓位上限", "%", "超过则拒单 RK008"),
    ("max_daily_loss_pct", "单日亏损熔断线", "%", "触发后禁止买入 RK009"),
    ("max_price_deviation_pct", "最大价格偏离", "%", "近似价格笼子，超过拒单 RK010"),
    ("max_single_order_value_pct", "单笔金额上限", "%", "超过则拒单 RK012"),
    ("max_orders_per_minute", "每分钟最多委托", "笔", "超过则拒单 RK013"),
    ("min_listed_days", "最少上市交易日", "日", "不足则拒单 RK004"),
    ("min_order_amount", "单笔最小金额", "元", "低于则拒单 RK012（防最低佣金侵蚀）"),
]


def _render_risk_table(risk: dict) -> None:
    """只读展示风控参数（默认值来自 config.yaml；编辑/保存由 M2-04/M3-07 接入）。"""
    rows = []
    for key, label, unit, effect in RISK_ROWS:
        value = risk.get(key, C.DASH)
        rows.append({"参数": f"{label}（{key}）", "当前值": f"{value} {unit}", "说明": effect})
    C.data_table(pd.DataFrame(rows))



# ---------------------------------------------------------------------------
# M3-07 最小可用：风控参数写回 config.yaml（行内保留注释）+ 热生效
# ---------------------------------------------------------------------------
RISK_EDIT_FIELDS = (
    ("max_position_per_symbol_pct", "单票仓位上限", 1.0, 100.0, 1.0, "%", "单只个股市值占总资产上限（超过拒单 RK007）"),
    ("max_total_position_pct", "总仓位上限", 1.0, 100.0, 1.0, "%", "全部持仓市值占总资产上限（超过拒单 RK008）"),
    ("max_daily_loss_pct", "单日亏损熔断线", 0.5, 20.0, 0.5, "%", "当日亏损达到该比例触发熔断（RK009）"),
    ("max_price_deviation_pct", "最大价格偏离", 0.1, 20.0, 0.1, "%", "限价与最新价最大偏离（RK010）"),
    ("max_single_order_value_pct", "单笔金额上限", 0.5, 100.0, 0.5, "%", "单笔委托金额占总资产上限（RK012）"),
    ("max_orders_per_minute", "每分钟最多委托", 1, 1000, 1, "笔", "下单频率限制（RK013）"),
    ("min_listed_days", "最少上市交易日", 0, 2500, 1, "日", "上市不足该天数不允许交易（RK004）"),
)
DEFAULT_RISK_VALUES = {
    "max_position_per_symbol_pct": 20.0, "max_total_position_pct": 80.0,
    "max_daily_loss_pct": 3.0, "max_price_deviation_pct": 2.0,
    "max_single_order_value_pct": 10.0, "max_orders_per_minute": 10, "min_listed_days": 60,
}


def _config_file() -> Path:
    """当前生效的 config.yaml 路径（支持 QD_CONFIG_FILE 覆盖）。"""
    try:
        from core.config import get_config

        cfg = get_config()
        if getattr(cfg, "source_path", None):
            return Path(cfg.source_path)
    except Exception:
        pass
    return ROOT / "config.yaml"


def _save_risk_values(updates: dict) -> tuple[bool, str]:
    """校验 → 行内改写 config.yaml 的 risk 段（保留注释）→ reload + 重置服务热生效。"""
    try:
        from core.config import get_config, reload_config
        from core.models import RiskConfig

        cfg = get_config()
        try:
            RiskConfig(**{**cfg.risk.model_dump(), **updates})  # pydantic 范围校验（复用配置校验）
        except Exception as exc:
            return False, f"风控参数校验失败：{exc}"
        path = _config_file()
        original = path.read_text(encoding="utf-8")
        lines = original.splitlines()
        out: list[str] = []
        in_risk = False
        written: set[str] = set()
        for line in lines:
            if re.match(r"^risk:\s*$", line):
                in_risk = True
                out.append(line)
                continue
            if in_risk and line.strip() and not line.startswith((" ", "	")):
                in_risk = False
            if in_risk:
                match = re.match(r"^(\s+)([A-Za-z_][\w]*):\s*([^#]*?)(\s*#.*)?$", line)
                if match and match.group(2) in updates:
                    key = match.group(2)
                    value = updates[key]
                    text = str(int(value)) if isinstance(value, int) else f"{float(value):g}"
                    out.append(f"{match.group(1)}{key}: {text}{match.group(4) or ''}")
                    written.add(key)
                    continue
            out.append(line)
        missing = set(updates) - written
        if missing:
            return False, f"配置文件未找到字段 {sorted(missing)}，未保存（请检查 config.yaml 结构）"
        backup_dir = cfg.data_dir_path / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup = backup_dir / f"config_{__import__('datetime').datetime.now():%Y%m%d_%H%M%S}.yaml"
        shutil.copy2(path, backup)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text("\n".join(out) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        reload_config()
        try:
            import app_services

            app_services.reset_services()
        except Exception:
            pass
        return True, f"风控参数已保存并热生效（原配置备份：{backup.name}）"
    except Exception as exc:
        return False, f"保存失败：{type(exc).__name__}: {exc}"


def render() -> None:
    C.page_header("设置", "数据源、风控、通知、备份与环境（M3-07 目标页，当前为 M0 骨架）",
                  badge="M0 骨架")
    meta = C.app_meta()
    tabs = st.tabs(["数据源", "风控", "通知", "存储与备份", "环境", "关于"])

    # =====================================================================
    # 1. 数据源
    # =====================================================================
    with tabs[0]:
        if not meta.get("config_ok"):
            C.render_error_card(
                "配置加载失败（已降级为默认值）",
                message=str(meta.get("config_error") or "配置未就绪"),
                suggestion="检查 config.yaml 是否存在、YAML 语法与键名是否符合 SPEC §7；"
                           "修复后点击侧边栏「清缓存并刷新」。页面其余功能不受影响。",
            )
        else:
            st.caption(f"配置来源：`{meta.get('config_source') or meta.get('config_path')}`"
                       "（只读展示，保存能力待 M3-07 接入）")
        with st.container(border=True):
            st.markdown("##### 数据源与自选池")
            d1, d2 = st.columns([1, 1.4])
            with d1:
                st.selectbox(
                    "主数据源（provider）",
                    ["akshare", "mock"],
                    index=0 if meta.get("provider") == "akshare" else 1,
                    key="qd_set_provider",
                    help="主源失败后按 config.data.fallbacks 自动降级（默认 mock）。",
                )
                st.number_input("历史数据年限", min_value=1, max_value=20,
                                value=int(meta.get("history_years") or 5),
                                key="qd_set_history_years")
                st.number_input("刷新间隔（毫秒）", min_value=0, max_value=10000,
                                value=int(meta.get("request_interval_ms") or 300),
                                step=50, key="qd_set_interval")
                st.number_input("行情过期阈值（小时）", min_value=1, max_value=240,
                                value=int(float(meta.get("stale_threshold_hours") or 24)),
                                key="qd_set_stale", help="超过则风控 RK006 / 数据校验 DQ006")
            with d2:
                st.text_area(
                    "自选池（每行一个代码，或用逗号分隔）",
                    value="\n".join(meta.get("universe") or []),
                    height=150, key="qd_set_universe",
                    help="默认 3 只示例：600519.SH / 000001.SZ / 300750.SZ；保存后写入 config.data.universe。",
                )
                q1, q2 = st.columns(2)
                with q1:
                    if st.button("🔌 连通性测试", use_container_width=True, key="qd_set_health"):
                        C.not_ready(
                            "连通性测试未执行",
                            "DataProvider.health_check() 尚未接入，无法确认数据源状态。",
                            todo="data/provider.py::DataProvider.health_check（M1-01）",
                        )
                with q2:
                    st.caption(f"最后更新时间：{C.DASH}")
            st.button("保存数据源设置", key="qd_set_save_data", disabled=True,
                      help="core.config.get_config 为只读单例；写回接口（save_config + 校验 + 审计）待 M3-07 接入。")

    # =====================================================================
    # 2. 风控
    # =====================================================================
    with tabs[1]:
        if st.session_state.pop("qd_set_restore_defaults", False):
            for key, value in DEFAULT_RISK_VALUES.items():
                st.session_state[f"qd_set_risk_{key}"] = value
        risk_now = meta.get("risk") or {}
        with st.container(border=True):
            st.markdown("##### 风控参数（SPEC §9.1，可编辑并写回 config.yaml）")
            st.caption("保存后会校验参数范围、备份原配置、热生效（无需重启）；风控为纯函数，参数即时用于下一笔下单。")
            cols = st.columns(3)
            for index, (key, label, lo, hi, step, unit, help_text) in enumerate(RISK_EDIT_FIELDS):
                with cols[index % 3]:
                    is_int = isinstance(step, int)
                    kwargs = {"min_value": int(lo), "max_value": int(hi), "step": int(step),
                              "value": int(risk_now.get(key, DEFAULT_RISK_VALUES[key]) or DEFAULT_RISK_VALUES[key]),
                              "help": help_text, "key": f"qd_set_risk_{key}"} if is_int else {
                        "min_value": float(lo), "max_value": float(hi), "step": float(step),
                        "value": float(risk_now.get(key, DEFAULT_RISK_VALUES[key]) or DEFAULT_RISK_VALUES[key]),
                        "help": help_text, "key": f"qd_set_risk_{key}"}
                    st.number_input(f"{label}（{unit}）", **kwargs)
            st.caption("黑名单与风控总开关仍以 config.yaml 为准（本页暂不修改，避免误关风控）。")
            b1, b2 = st.columns(2)
            with b1:
                if st.button("保存风控参数", type="primary", key="qd_set_risk_save_btn"):
                    st.session_state["qd_set_show_risk_confirm"] = True
            with b2:
                if st.button("恢复默认值", key="qd_set_risk_reset"):
                    st.session_state["qd_set_restore_defaults"] = True
                    st.rerun()
        if st.session_state.get("qd_set_show_risk_confirm"):
            if C.confirm_action(
                "risk_save",
                "保存风控参数会写入 config.yaml 并即时生效（参数放宽会提高风险敞口）。是否确认？",
                button_label="确认保存",
                details=["保存前自动备份原配置到 runtime/backups/。", "保存后回测/下单立即使用新参数。"],
            ):
                st.session_state.pop("qd_set_show_risk_confirm", None)
                updates = {key: st.session_state.get(f"qd_set_risk_{key}", DEFAULT_RISK_VALUES[key])
                           for key, *_ in RISK_EDIT_FIELDS}
                ok, message = _save_risk_values({k: (int(v) if isinstance(DEFAULT_RISK_VALUES[k], int) else float(v))
                                                 for k, v in updates.items()})
                if ok:
                    st.success(message)
                    C.clear_caches()
                    st.rerun()
                else:
                    C.render_error_card("风控参数保存失败", message=message,
                                        suggestion="检查 config.yaml 是否可写、字段是否完整；失败时原文件未被修改。",
                                        trace_id=C.new_trace_id())

    # =====================================================================
    # 3. 通知
    # =====================================================================
    with tabs[2]:
        with st.container(border=True):
            st.markdown("##### 通知渠道")
            st.toggle("通知总开关", value=bool(meta.get("notify_enabled")), key="qd_set_notify_on",
                      help="关闭时只写日志与 notifications(DISABLED)，不发送任何渠道。")
            channels = meta.get("notify_channels") or {}
            n1, n2, n3 = st.columns(3)
            with n1:
                st.checkbox("桌面通知", value=bool(channels.get("desktop", True)),
                            key="qd_set_ch_desktop",
                            help="Windows 原生 toast，无网络也可用。")
            with n2:
                st.checkbox("邮件", value=bool(channels.get("email", False)),
                            key="qd_set_ch_email",
                            help="SMTP；密码只从环境变量 QD_SMTP_PASSWORD 读取，禁止写入配置。")
            with n3:
                st.checkbox("Webhook", value=bool(channels.get("webhook", False)),
                            key="qd_set_ch_webhook",
                            help="POST JSON：{event_type,title,body,ts,trace_id}，适配企微/飞书/钉钉。")
            e1, e2 = st.columns(2)
            with e1:
                st.text_input("SMTP 主机", value="", key="qd_set_smtp_host")
                st.number_input("SMTP 端口", min_value=1, max_value=65535, value=465,
                                key="qd_set_smtp_port")
                st.text_input("SMTP 用户名", value="", key="qd_set_smtp_user")
                st.caption("密码：从环境变量 QD_SMTP_PASSWORD 读取（页面不显示明文）。")
            with e2:
                st.text_input("收件人", value="", key="qd_set_smtp_to")
                st.text_input("Webhook URL", value="", key="qd_set_webhook_url")
                st.caption("配置缺失时对应渠道不启用（不影响其它渠道与主流程）。")
            st.button("发送测试通知", key="qd_set_notify_test", disabled=True,
                      help="Notifier.send 待接入（M3-04）")

    # =====================================================================
    # 4. 存储与备份
    # =====================================================================
    with tabs[3]:
        with st.container(border=True):
            st.markdown("##### 存储与备份")
            b1, b2, b3 = st.columns(3)
            with b1:
                if st.button("💾 立即备份", use_container_width=True, key="qd_set_backup"):
                    C.not_ready(
                        "备份未执行（接口待接入）",
                        "core.db 备份接口尚未接入，本次点击未创建备份文件。",
                        todo="core/db.py 在线备份 + 保留策略（M0-04）",
                    )
            with b2:
                st.button("♻️ 恢复备份", use_container_width=True, key="qd_set_restore",
                          disabled=True, help="恢复前自动先备份当前库 + 二次确认（M3-07）")
            with b3:
                st.button("🧹 清理回测记录", use_container_width=True, key="qd_set_clean_bt",
                          disabled=True, help="清理历史回测记录（二次确认，M3-07）")
            backup_rows: list[dict] = []
            try:
                from core.config import get_config as _get_cfg

                backup_dir = _get_cfg().backup_dir
            except Exception:
                backup_dir = ROOT / "runtime" / "backups"
            try:
                if backup_dir.exists():
                    for path in sorted(backup_dir.glob("**/*"), key=lambda p: p.stat().st_mtime, reverse=True):
                        if path.is_file():
                            backup_rows.append({
                                "文件名": path.name,
                                "创建时间": C.fmt_datetime(path.stat().st_mtime),
                                "大小": f"{path.stat().st_size / 1024:.1f} KB",
                                "路径": str(path),
                            })
            except Exception as exc:
                st.caption(f"读取备份目录失败：{type(exc).__name__}: {exc}")
            if backup_rows:
                C.data_table(pd.DataFrame(backup_rows), height=220)
                st.caption(f"备份目录：{backup_dir}（来自 scripts/backup_db.py 生成）")
            else:
                C.not_ready(
                    "暂无备份",
                    "备份目录为空。可运行 `python scripts/backup_db.py` 生成备份（备份文件会出现在这里）。",
                    todo=f"备份目录：{backup_dir}",
                    icon="💾",
                )
            st.caption(f"数据库大小：{C.DASH}　备份保留份数：{C.DASH}（config.database.backup_keep）")

    # =====================================================================
    # 5. 环境
    # =====================================================================
    with tabs[4]:
        with st.container(border=True):
            st.markdown("##### 运行环境")
            st.caption(C.python_version_text())
            rows = [
                {"依赖": name, "状态": "已安装" if ok else "缺失", "说明": desc}
                for name, ok, desc in C.env_check()
            ]
            C.data_table(pd.DataFrame(rows))
            st.caption("依赖检查为只读探测；完整环境自检请运行 `scripts/check_env.py`（M0-08）。")
            c1, c2 = st.columns(2)
            with c1:
                levels = ["DEBUG", "INFO", "WARNING", "ERROR"]
                current_level = str(meta.get("log_level") or "INFO").upper()
                st.selectbox("日志级别", levels,
                             index=levels.index(current_level) if current_level in levels else 1,
                             key="qd_set_log_level",
                             help="当前值来自 core.config.get_config().app.log_level；"
                                  "修改后写回配置待 M3-07 接入。")
            with c2:
                st.caption("TA-Lib 缺失时策略层自动使用 pandas 兜底实现（M1-06），结果容差 ≤1e-6。")
                st.caption("ccxt 未安装时 CcxtBroker.is_available()=False，UI 提示「未配置」，全流程仍走模拟盘。")

    # =====================================================================
    # 6. 关于
    # =====================================================================
    with tabs[5]:
        with st.container(border=True):
            st.markdown("##### 关于 QuantDesk")
            st.markdown(
                f"- **版本**：v{meta.get('version', '2.0.1')}\n"
                f"- **模式**：模拟盘（实盘未接入；QMT 仅骨架）\n"
                f"- **配置文件**：`{meta.get('config_source') or meta.get('config_path', 'config.yaml')}`\n"
                "- **许可证**：个人非商业使用；第三方依赖清单见 `docs/THIRD_PARTY.md`\n"
            )
            C.render_info_card(
                "数据与合规声明",
                "本工具仅用于个人学习与研究，不构成任何投资建议；行情数据来自公开数据源，"
                "存在延迟与误差可能；回测/模拟盘结果不代表未来收益。请勿用于未经许可的商业用途。",
            )
            st.markdown("相关文档：`README.md` · `docs/SPEC.md` · `docs/ARCHITECTURE.md` · "
                        "`docs/THIRD_PARTY.md` · `docs/GLOSSARY.md`")

    C.todo_expander(TODO_ITEMS)
