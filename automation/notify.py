"""QuantDesk 通知渠道（M3-05，SPEC §6.5 / §8.8）。

- 至少实现 **桌面通知（Windows 原生）** 与 **Webhook** 两种渠道，失败自动降级为日志；
- 通知全局开关（``notify.enabled``）关闭时只写日志、不发送；
- 每次发送写 ``notifications`` 表（SENT / FAILED / DISABLED），失败不影响主流程；
- 密码/token 只从环境变量读，不进日志与数据库（邮件渠道后续里程碑接入，当前给出明确中文说明）。
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from core.events import Topic, publish as publish_event
from core.models import NotificationStatus, now_local

logger = logging.getLogger(__name__)

CHANNEL_DESKTOP = "DESKTOP"
CHANNEL_EMAIL = "EMAIL"
CHANNEL_WEBHOOK = "WEBHOOK"


@dataclass
class NotifyReport:
    """通知发送结果（SPEC §6.5 ``Notifier.send`` 返回值）。"""

    event_type: str
    title: str = ""
    enabled: bool = True
    results: dict[str, dict[str, Any]] = field(default_factory=dict)
    any_success: bool = False
    message: str = ""

    @property
    def status(self) -> str:
        """汇总状态：SENT / FAILED / DISABLED。"""
        if not self.enabled:
            return NotificationStatus.DISABLED.value
        return NotificationStatus.SENT.value if self.any_success else NotificationStatus.FAILED.value


def _run_powershell(script: str) -> tuple[bool, str]:
    """运行一段 PowerShell（隐藏窗口），返回 (是否成功, 失败摘要)。"""
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-Command", script],
            capture_output=True,
            timeout=10,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except Exception as exc:  # noqa: BLE001 - 通知失败不得影响主流程
        return False, f"{type(exc).__name__}: {exc}"
    if completed.returncode == 0:
        return True, ""
    lines = (completed.stderr or completed.stdout or "").strip().splitlines()
    detail = next((line.strip() for line in lines if line.strip() and not line.strip().startswith("+")), "未知错误")
    return False, detail[:200]


def _send_desktop_windows(title: str, body: str) -> tuple[bool, str]:
    """Windows 原生桌面通知：优先 WinRT Toast，Toast 不可用时降级为托盘气泡通知。

    - Win10/11 的 Toast 需要已注册的 AppUserModelID；便携/未注册用户下可能返回
      ``0x80070490 Element not found``，此时自动降级为 ``NotifyIcon`` 托盘气泡（同样是
      Windows 原生能力，无第三方依赖）；
    - 两种方式都失败才返回 ``False``（由调用方降级为日志）。
    """
    if sys.platform != "win32":
        return False, "当前系统非 Windows，桌面通知不可用（已降级为日志）"
    from xml.sax.saxutils import escape

    # 1) WinRT Toast（文本做 XML 转义；单引号转义后嵌入 PowerShell 单引号字符串）
    toast_xml = (
        '<toast><visual><binding template="ToastGeneric">'
        f"<text>{escape(str(title)[:64])}</text>"
        f"<text>{escape(str(body)[:256])}</text>"
        "</binding></visual></toast>"
    )
    toast_script = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] | Out-Null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] | Out-Null;"
        "$xml = New-Object Windows.Data.Xml.Dom.XmlDocument;"
        f"$xml.LoadXml('{toast_xml.replace(chr(39), chr(39) * 2)}');"
        "$toast = New-Object Windows.UI.Notifications.ToastNotification $xml;"
        # 注意：PowerShell 5 不允许对方法返回值链式调用（CreateToastNotifier().Show() 会解析失败）
        "$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier();"
        "$notifier.Show($toast);"
    )
    toast_ok, toast_detail = _run_powershell(toast_script)
    if toast_ok:
        return True, "已发送 Windows Toast 桌面通知"

    # 2) 托盘气泡通知（Toast 需要注册 AppUserModelID；此方案对便携/未注册环境也可用）
    safe_title = str(title).replace("'", "''").replace("\n", " ")[:64]
    safe_body = str(body).replace("'", "''").replace("\n", " ")[:256]
    balloon_script = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "Add-Type -AssemblyName System.Drawing;"
        "$n = New-Object System.Windows.Forms.NotifyIcon;"
        "$n.Icon = [System.Drawing.SystemIcons]::Information;"
        "$n.Visible = $true;"
        f"$n.ShowBalloonTip(5000, '{safe_title}', '{safe_body}', 'Info');"
        "Start-Sleep -Milliseconds 1200;"
        "$n.Dispose();"
    )
    balloon_ok, balloon_detail = _run_powershell(balloon_script)
    if balloon_ok:
        return True, "已发送 Windows 托盘通知（Toast 不可用时自动降级）"
    return False, f"桌面通知失败：Toast（{toast_detail}）；托盘通知（{balloon_detail}）"


def _send_webhook(url: str, payload: dict[str, Any]) -> tuple[bool, str]:
    """通用 Webhook POST JSON（企业微信/飞书/钉钉可自行适配）；失败重试 1 次。"""
    if not url:
        return False, "Webhook 未配置 url（notify.webhook.url 为空）"
    try:
        import requests
    except ImportError:
        return False, "Webhook 发送失败：requests 未安装，请 pip install requests"
    last_error = ""
    for attempt in range(2):
        try:
            response = requests.post(url, json=payload, timeout=5)
            if 200 <= int(response.status_code) < 300:
                return True, f"Webhook 发送成功（HTTP {response.status_code}）"
            last_error = f"HTTP {response.status_code}"
        except Exception as exc:  # noqa: BLE001
            last_error = f"{type(exc).__name__}: {exc}"
        if attempt == 0:
            logger.warning("Webhook 首次发送失败，重试 1 次：%s", last_error)
    return False, f"Webhook 发送失败：{last_error}"


class Notifier:
    """通知服务（多渠道路由 + 失败隔离 + 落 ``notifications`` 表）。

    :param config: 全局配置（``AppConfig``）；为空时读 ``core.config.get_config()``。
    :param session_factory: 写 ``notifications`` 表的会话工厂；为空时用 ``core.db.get_session_factory()``。
    :param desktop_sender: 桌面通知实现（测试可注入假实现）。
    :param webhook_sender: Webhook 实现（测试可注入，避免真实网络）。
    :param clock: 时钟，默认 ``core.models.now_local``。
    """

    def __init__(
        self,
        config: Any = None,
        *,
        session_factory: Any = None,
        desktop_sender: Callable[[str, str], tuple[bool, str]] | None = None,
        webhook_sender: Callable[[str, dict[str, Any]], tuple[bool, str]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if config is None:
            from core.config import get_config

            config = get_config()
        self.config = config
        self._session_factory = session_factory
        self._desktop_sender = desktop_sender or _send_desktop_windows
        self._webhook_sender = webhook_sender or _send_webhook
        self._clock = clock or now_local

    # -- 公共入口 ---------------------------------------------------------

    def send(
        self,
        event_type: str,
        title: str,
        body: str,
        channels: list[str] | None = None,
    ) -> NotifyReport:
        """发送通知（单渠道失败不影响其它渠道）。"""
        notify_cfg = getattr(self.config, "notify", None)
        enabled = bool(getattr(notify_cfg, "enabled", False))
        report = NotifyReport(event_type=event_type, title=title, enabled=enabled)
        selected = [str(item).strip().upper() for item in (channels or self._configured_channels())]
        if not enabled:
            report.message = "通知总开关已关闭（notify.enabled=false），仅写日志、不发送"
            logger.info("通知未发送（总开关关闭）：event_type=%s title=%s", event_type, title)
            report.results["LOG"] = {"status": "DISABLED", "error": None, "detail": report.message}
            return report

        payload = {
            "event_type": event_type,
            "title": title,
            "body": body,
            "ts": self._clock().isoformat(timespec="seconds"),
        }
        for channel in selected:
            ok, detail = self._dispatch(channel, title, body, payload)
            status = NotificationStatus.SENT.value if ok else NotificationStatus.FAILED.value
            if not ok:
                logger.warning("通知渠道 %s 发送失败，已降级为日志：%s", channel, detail)
                report.results[channel] = {"status": "LOG", "error": detail, "detail": "失败后降级为日志"}
                self._persist(channel, event_type, title, body, "FAILED", detail)
                continue
            report.results[channel] = {"status": status, "error": None, "detail": detail}
            report.any_success = True
            self._persist(channel, event_type, title, body, status, None)

        if selected:
            logger.info(
                "通知发送完成：event_type=%s channels=%s success=%s",
                event_type, ",".join(selected), report.any_success,
            )
        else:
            logger.warning("通知未发送：没有启用的渠道（notify.channels 全为 false）")
            report.message = "没有启用的通知渠道（通知已降级为日志）"
            report.results["LOG"] = {"status": "LOG", "error": None, "detail": report.message}
        try:
            publish_event(
                Topic.NOTIFY_SENT,
                {"channel": ",".join(selected) or "LOG", "event_type": event_type, "status": report.status},
            )
        except Exception:  # noqa: BLE001 - 事件失败不影响通知
            logger.debug("发布 notify.sent 事件失败", exc_info=True)
        return report

    def test(self, message: str = "QuantDesk 通知测试") -> NotifyReport:
        """发送一条测试通知（设置页“发送测试”按钮使用）。"""
        return self.send("notify.test", "QuantDesk 测试通知", message)

    # -- 内部 -------------------------------------------------------------

    def _configured_channels(self) -> list[str]:
        channels_cfg = getattr(getattr(self.config, "notify", None), "channels", None)
        if channels_cfg is None:
            return [CHANNEL_DESKTOP]
        selected: list[str] = []
        if bool(getattr(channels_cfg, "desktop", False)):
            selected.append(CHANNEL_DESKTOP)
        if bool(getattr(channels_cfg, "webhook", False)):
            selected.append(CHANNEL_WEBHOOK)
        if bool(getattr(channels_cfg, "email", False)):
            selected.append(CHANNEL_EMAIL)
        return selected

    def _dispatch(
        self, channel: str, title: str, body: str, payload: dict[str, Any]
    ) -> tuple[bool, str]:
        if channel == CHANNEL_DESKTOP:
            return self._desktop_sender(title, body)
        if channel == CHANNEL_WEBHOOK:
            from core.config import get_config  # noqa: F401 - 保持配置读取一致

            url = ""
            webhook_cfg = getattr(getattr(self.config, "notify", None), "webhook", None)
            if isinstance(webhook_cfg, dict):
                url = str(webhook_cfg.get("url") or "")
            elif webhook_cfg is not None:
                url = str(getattr(webhook_cfg, "url", "") or "")
            if not url:
                url = os.environ.get("QD_WEBHOOK_URL", "")
            return self._webhook_sender(url, payload)
        if channel == CHANNEL_EMAIL:
            return False, "邮件渠道 MVP 未实现（至少可用桌面通知与 Webhook），已降级为日志"
        return False, f"未知通知渠道：{channel!r}"

    def _persist(
        self,
        channel: str,
        event_type: str,
        title: str,
        body: str,
        status: str,
        error: str | None,
    ) -> None:
        """写 ``notifications`` 表（失败只记日志，不影响主流程）。"""
        try:
            factory = self._session_factory
            if factory is None:
                from core.db import get_session_factory

                factory = get_session_factory()
            from core.models import NotificationORM

            with factory() as session:
                session.add(
                    NotificationORM(
                        channel=channel,
                        event_type=event_type,
                        title=title[:256],
                        body=body,
                        status=status,
                        error=error,
                        sent_at=self._clock(),
                    )
                )
                session.commit()
        except Exception:  # noqa: BLE001
            logger.debug("写 notifications 表失败（不阻塞通知）：channel=%s", channel, exc_info=True)


__all__ = ["Notifier", "NotifyReport", "CHANNEL_DESKTOP", "CHANNEL_EMAIL", "CHANNEL_WEBHOOK"]
