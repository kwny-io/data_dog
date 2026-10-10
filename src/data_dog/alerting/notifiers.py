"""P4-3 通知适配器：把告警真正投递给**人**（webhook / 邮件 / IM）。

定位
----
上报闭环此前止步于 `SaaSTestAdapter`（本地测试 SaaS，仅落库审计）。本模块补上
"最后一公里"：能发到值班人的 webhook、邮箱、或飞书/钉钉群机器人。

安全底线（别让"通知"变成 SSRF 入口）
------------------------------------
webhook / IM 的目标是**外部 URL**，直接 POST 等于给攻击者一个 SSRF 跳板（用它探测
内网/云元数据）。故目标 URL **必须先过** `data_plane/fetch.validate_url` 的 SSRF
校验（拒内网/回环/非公网 IP、拒非 http(s) 协议）；**真正的取数走 `safe_request`**
——裸 `urllib.request.urlopen` 会跟随 3xx 且不看目标、建连时还会二次解析 DNS，
正好能绕过只校验初始 URL 的门（这两处缺口已修，见 `fetch.py` 的安全取数原语）。
默认**失败关闭**——拿不准就拒。
仅显式 `DD_NOTIFY_ALLOW_PRIVATE=1`（本地测试 / 受信内网）才放行内网目标。

诚实边界
--------
- **通知可靠性 = 适配器可靠性**：webhook 返回非 2xx、超时、连接失败 → 本次投递
  `ok=False`；重投由 outbox/升级引擎的既有语义负责（3 次→dead）。**不保证必达**
  ——要必达需接专业告警平台，属更大工程。
- **SMTP / IM 凭据走环境变量**，不进仓库。
- 只用标准库（`urllib` / `smtplib`）：不引入 `requests`/`httpx` 依赖。
"""
from __future__ import annotations

import json
import logging
import smtplib
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any, Protocol, runtime_checkable
from urllib.parse import urlparse

from .. import settings
from ..data_plane.fetch import safe_request, validate_url

_log = logging.getLogger(__name__)

_ALLOWED_SCHEMES = {"http", "https"}


@dataclass(frozen=True)
class Route:
    """一条投递路由：往哪个通道、发给谁。"""
    channel: str                    # webhook / email / im
    target: str                     # webhook URL / 邮箱 / 群机器人 URL
    assignee: str = ""              # 值班人/组（记录用）
    level: int = 1                  # 值班级别（1=一线 …）


@dataclass(frozen=True)
class DeliveryResult:
    ok: bool
    detail: str = ""                # 失败原因 / 拒绝原因（审计用）
    receipt: str = ""               # 成功时的回执（如 HTTP 状态/LDAP msg id）


@dataclass(frozen=True)
class Notification:
    """一条待发通知的内容（已脱敏——不含明文设备号/密钥/原始载荷）。"""
    event_id: str
    subject: str
    body: str
    payload: dict[str, Any] = field(default_factory=dict)


def build_notification(alert: dict[str, Any]) -> Notification:
    """从落库告警行构造通知内容（对齐 §12.5：业务摘要 + 受控引用）。

    **不外发** device_ref 之外的敏感项；正文是人话，不含沙箱路径/堆栈/密钥。
    """
    event_id = str(alert.get("event_id", ""))
    severity = str(alert.get("severity", "warning"))
    summary = str(alert.get("summary", "")) or "(无摘要)"
    component = str(alert.get("component_ref", ""))
    device_ref = str(alert.get("device_ref", ""))
    actions = list(alert.get("recommended_actions", []) or [])

    subject = f"[{severity.upper()}] {component} 故障告警"
    lines = [
        f"事件: {event_id}",
        f"严重度: {severity}",
        f"部件: {component}",
        f"设备: {device_ref}",
        f"摘要: {summary}",
    ]
    if actions:
        lines.append("建议动作:")
        lines += [f"  - {a}" for a in actions]
    body = "\n".join(lines)
    payload = {
        "event_id": event_id,
        "severity": severity,
        "component_ref": component,
        "device_ref": device_ref,
        "summary": summary,
        "recommended_actions": actions,
        "source": "data_dog",
    }
    return Notification(event_id=event_id, subject=subject, body=body,
                        payload=payload)


@runtime_checkable
class Notifier(Protocol):
    """通知通道的统一接口。`send` 绝不抛异常——失败收敛成 `DeliveryResult`。"""
    name: str

    def send(self, note: Notification, *, route: Route) -> DeliveryResult: ...


# ---------------------------------------------------------------- SSRF 门

def _target_allowed(url: str) -> tuple[bool, str]:
    """通知目标 URL 的 SSRF 校验。默认失败关闭；`DD_NOTIFY_ALLOW_PRIVATE=1`
    时退化为「仅查协议 + host 存在」（本地测试/受信内网）。"""
    try:
        p = urlparse(url)
    except Exception:                                 # noqa: BLE001
        return False, "URL 解析失败"
    if p.scheme not in _ALLOWED_SCHEMES:
        return False, f"仅允许 http/https，得到 {p.scheme!r}"
    if not p.hostname:
        return False, "缺少 host"
    if settings.notify_allow_private_targets():
        return True, ""                               # 显式放行内网（测试/受信）
    d = validate_url(url)
    if not d.allowed:
        return False, d.reason
    return True, ""


def _post_json(url: str, payload: dict[str, Any], *,
               timeout_s: float) -> tuple[bool, str, str]:
    """POST 一个 JSON，返回 (ok, detail, receipt)。非 2xx / 网络错 → ok=False。

    走 `data_plane.fetch.safe_request`（**逐跳复验重定向 + 连接钉在校验过的 IP 上**）。
    此前用的是裸 `urllib.request.urlopen`：它默认跟随 3xx 且不看目标，于是
    "攻击者可控的公网 webhook → 302 → 169.254.169.254" 就能绕过只校验初始 URL 的门；
    而且它建连时会**再解析一次** DNS（经典 TOCTOU）。两处缺口都在 safe_request 里关掉。
    """
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    res = safe_request(url, method="POST", data=data,
                       headers={"Content-Type": "application/json"},
                       timeout_s=timeout_s,
                       allow_private=settings.notify_allow_private_targets())
    if res.ok:
        return True, "", f"http_{res.status}"
    if res.status:                       # 有 HTTP 状态码 → 回执带状态码（与旧行为一致）
        return False, f"HTTP {res.status}", f"http_{res.status}"
    return False, res.error, ""          # 传输层失败（含被安全门拒绝）


# ---------------------------------------------------------------- 具体通道

@dataclass
class WebhookNotifier:
    """POST JSON 到 `route.target`（外部 URL，过 SSRF 校验）。"""
    name: str = "webhook"

    def send(self, note: Notification, *, route: Route) -> DeliveryResult:
        ok, reason = _target_allowed(route.target)
        if not ok:
            return DeliveryResult(False, detail=f"target_denied: {reason}")
        ok, detail, receipt = _post_json(
            route.target, note.payload,
            timeout_s=settings.notify_http_timeout_s())
        return DeliveryResult(ok, detail=detail, receipt=receipt)


@dataclass
class IMNotifier:
    """POST 到飞书/钉钉/企微群机器人 webhook（目标 URL 过 SSRF 校验）。

    载荷用最常见的 `{"msg_type":"text","content":{"text": ...}}`（飞书/钉钉通用）；
    企微略有不同但同为 text 语义，正文是人话摘要，够值班看。目标 URL 通常来自
    `DD_IM_WEBHOOK_URL`（凭据在 URL 里）。
    """
    name: str = "im"

    def send(self, note: Notification, *, route: Route) -> DeliveryResult:
        target = route.target or settings.im_webhook_url()
        if not target:
            return DeliveryResult(False, detail="no_target: 未配 IM webhook URL")
        ok, reason = _target_allowed(target)
        if not ok:
            return DeliveryResult(False, detail=f"target_denied: {reason}")
        payload = {"msg_type": "text",
                   "content": {"text": f"{note.subject}\n{note.body}"}}
        ok, detail, receipt = _post_json(
            target, payload, timeout_s=settings.notify_http_timeout_s())
        return DeliveryResult(ok, detail=detail, receipt=receipt)


@dataclass
class EmailNotifier:
    """经 SMTP 发纯文本邮件给 `route.target`（邮箱地址）。凭据走环境变量。

    SMTP 目标**不是 URL**，故不受 SSRF 门约束（SMTP host 来自受信配置）。
    """
    name: str = "email"

    def send(self, note: Notification, *, route: Route) -> DeliveryResult:
        host = settings.smtp_host()
        if not host:
            return DeliveryResult(False, detail="no_smtp: 未配 DD_SMTP_HOST")
        if not route.target:
            return DeliveryResult(False, detail="no_target: 路由缺邮箱地址")
        msg = EmailMessage()
        msg["Subject"] = note.subject
        msg["From"] = settings.smtp_from()
        msg["To"] = route.target
        msg.set_content(note.body)
        try:
            with smtplib.SMTP(host, settings.smtp_port(),
                              timeout=settings.smtp_timeout_s()) as s:
                s.starttls()
                user = settings.smtp_user()
                if user:
                    s.login(user, settings.smtp_password())
                s.send_message(msg)
            return DeliveryResult(True, receipt="smtp_ok")
        except Exception as e:                        # noqa: BLE001
            return DeliveryResult(False, detail=f"{type(e).__name__}: {e}")


# ---------------------------------------------------------------- 注册表

@dataclass
class NotifierRegistry:
    """按 `route.channel` 选通道。未知 channel → 失败关闭（不猜、不静默丢弃）。"""
    notifiers: dict[str, Notifier] = field(default_factory=dict)

    @classmethod
    def default(cls) -> NotifierRegistry:
        impls: list[Any] = [WebhookNotifier(), EmailNotifier(), IMNotifier()]
        return cls(notifiers={n.name: n for n in impls})

    def for_channel(self, channel: str) -> Notifier | None:
        return self.notifiers.get((channel or "").strip().lower())

    def send(self, note: Notification, *, route: Route) -> DeliveryResult:
        n = self.for_channel(route.channel)
        if n is None:
            return DeliveryResult(False, detail=f"unknown_channel: {route.channel!r}")
        try:
            return n.send(note, route=route)
        except Exception as e:                        # noqa: BLE001
            # 适配器契约要求不抛；这里兜底，确保"通知"永不让调用方崩溃
            _log.warning("notifier 异常", extra={
                "channel": route.channel,
                "error": f"{type(e).__name__}: {e}"})
            return DeliveryResult(False, detail=f"{type(e).__name__}: {e}")
