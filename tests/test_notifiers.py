"""P4-3 通知适配器测试。

覆盖 docs/p4/03_notify_oncall.md §5 的相关验收：
  1. webhook notifier 对本地测试 server **真发 POST**；载荷已脱敏。
  2. SSRF 门：内网/回环 URL 默认**被拒**（失败关闭）；显式放行才可发。
  3. 未知通道 → 失败关闭。
  4. 邮件未配 SMTP → 失败关闭（不假装发出）。
  5. `build_notification` 不外发禁止字段（密钥/沙箱路径/原始载荷）。
"""
from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from data_dog.alerting.notifiers import (
    DeliveryResult,
    EmailNotifier,
    IMNotifier,
    Notification,
    NotifierRegistry,
    Route,
    WebhookNotifier,
    build_notification,
)

_HANDLER_BODIES: list[dict] = []


class _Handler(BaseHTTPRequestHandler):
    def do_POST(self):                                # noqa: N802
        n = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            _HANDLER_BODIES.append(json.loads(raw.decode("utf-8")))
        except Exception:                             # noqa: BLE001
            _HANDLER_BODIES.append({"_raw": raw.decode("utf-8", "replace")})
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *a):                        # 静音
        pass


@pytest.fixture()
def local_server():
    """起一个本地 HTTP server，返回 (url, bodies)；退出时关停。"""
    _HANDLER_BODIES.clear()
    srv = HTTPServer(("127.0.0.1", 0), _Handler)
    t = threading.Thread(target=srv.serve_forever, name="test-notify-srv",
                         daemon=True)
    t.start()
    host, port = srv.server_address
    yield f"http://{host}:{port}/hook", _HANDLER_BODIES
    srv.shutdown()


def _note() -> Notification:
    return Notification(event_id="alert_x", subject="s", body="b",
                        payload={"event_id": "alert_x", "severity": "critical"})


# ---------------------------------------------------------------- 1. 真发 POST

def test_webhook_really_posts(local_server, monkeypatch):
    """放行内网后，webhook notifier 对本地 server 真发 POST，正文是脱敏载荷。"""
    monkeypatch.setenv("DD_NOTIFY_ALLOW_PRIVATE", "1")
    url, bodies = local_server
    res = WebhookNotifier().send(_note(), route=Route("webhook", url, "alice"))
    assert res.ok and res.receipt.startswith("http_")
    assert bodies and bodies[0]["event_id"] == "alert_x"


def test_webhook_ssrf_blocks_private_by_default(local_server, monkeypatch):
    """**默认失败关闭**：内网/回环目标被 SSRF 门拒绝（不真的发出去）。"""
    monkeypatch.delenv("DD_NOTIFY_ALLOW_PRIVATE", raising=False)
    url, bodies = local_server
    res = WebhookNotifier().send(_note(), route=Route("webhook", url, "alice"))
    assert not res.ok
    assert "target_denied" in res.detail
    assert bodies == []                               # 根本没发出


def test_webhook_rejects_bad_scheme(monkeypatch):
    monkeypatch.setenv("DD_NOTIFY_ALLOW_PRIVATE", "1")
    res = WebhookNotifier().send(_note(),
                                 route=Route("webhook", "file:///etc/passwd"))
    assert not res.ok and "target_denied" in res.detail


# ---------------------------------------------------------------- 3/4. 失败关闭

def test_unknown_channel_fails_closed():
    reg = NotifierRegistry.default()
    res = reg.send(_note(), route=Route("carrier_pigeon", "x"))
    assert not res.ok and "unknown_channel" in res.detail


def test_email_without_smtp_fails_closed(monkeypatch):
    monkeypatch.delenv("DD_SMTP_HOST", raising=False)
    res = EmailNotifier().send(_note(), route=Route("email", "a@b.com"))
    assert not res.ok and "no_smtp" in res.detail


def test_im_without_target_fails_closed(monkeypatch):
    monkeypatch.delenv("DD_IM_WEBHOOK_URL", raising=False)
    res = IMNotifier().send(_note(), route=Route("im", ""))
    assert not res.ok and "no_target" in res.detail


# ---------------------------------------------------------------- 5. 脱敏

def test_build_notification_scrubs_forbidden_fields():
    """通知内容不含密钥/沙箱路径/原始载荷等禁止外发字段。"""
    note = build_notification({
        "event_id": "alert_x", "severity": "critical",
        "component_ref": "joint_motor.front_hip", "device_ref": "device-abc",
        "summary": "过热", "recommended_actions": ["停机"],
        "api_key": "SECRET", "sandbox_path": "C:/tmp/x",
        "raw_payload": {"secret": 1},
    })
    blob = json.dumps(note.payload, ensure_ascii=False)
    assert "SECRET" not in blob and "sandbox_path" not in blob
    assert "raw_payload" not in blob
    assert note.payload["event_id"] == "alert_x"


def test_registry_dispatches_by_channel(local_server, monkeypatch):
    """注册表按 channel 正确路由到对应适配器。"""
    monkeypatch.setenv("DD_NOTIFY_ALLOW_PRIVATE", "1")
    url, bodies = local_server
    reg = NotifierRegistry.default()
    res = reg.send(_note(), route=Route("webhook", url, "alice"))
    assert res.ok and bodies


def test_notifier_protocol_shape():
    """适配器契约：send 绝不抛异常，返回 DeliveryResult。"""
    from data_dog.alerting.notifiers import Notifier
    assert isinstance(WebhookNotifier(), Notifier)
    res = WebhookNotifier().send(_note(), route=Route("webhook", "not a url"))
    assert isinstance(res, DeliveryResult) and not res.ok
