"""`alerting/notifiers.py` 的边界路径测试（全离线，绝不发真请求）。

为什么要补这一份：已有 `tests/test_notifiers.py` 覆盖了"真发 POST + 默认失败
关闭"的主干，但**失败路径与 IM/邮件两条通道**没有被钉住——而这恰恰是通知模块
最危险的地方：投递失败若被当成成功，值班人永远不会收到告警（静默失败）。

本文件的三条纪律：
1. **不出网**：HTTP 层一律打桩（传输层 `data_plane.fetch._default_transport` /
   `smtplib.SMTP`），只断言"我们发出的报文长什么样"与"异常如何被收敛成 DeliveryResult"；
2. **失败即 ok=False**：HTTP 非 2xx、抛错、适配器异常都不许伪装成投递成功；
3. SSRF 门本身用桩替掉（真实公网 URL 会触发 DNS，用例必须离线）。
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from data_dog.alerting import notifiers as N
from data_dog.alerting.notifiers import (
    DeliveryResult,
    EmailNotifier,
    IMNotifier,
    Notification,
    NotifierRegistry,
    Route,
)
from data_dog.data_plane import fetch


class _TransportRecorder:
    """记录每次 `PinnedRequest`，并按需返回状态码或抛异常。

    打桩点从"`urllib.request.urlopen`"移到了 `data_plane.fetch._default_transport`：
    取数现在走 `safe_request`（**逐跳复验重定向 + 连接钉在校验过的 IP 上**），
    不再是裸 urlopen——所以"不出网"要打在传输层这一处。
    """

    def __init__(self, *, status: int = 200, body: bytes = b'{"ok":true}',
                 exc: Exception | None = None) -> None:
        self.status = status
        self.body = body
        self.exc = exc
        self.requests: list = []

    def __call__(self, req):
        self.requests.append(req)
        if self.exc is not None:
            raise self.exc
        return fetch.RawResponse(self.status, self.body, {})

    def payload(self) -> dict:
        return json.loads(self.requests[0].body.decode("utf-8"))


@pytest.fixture()
def transport(monkeypatch):
    """把默认传输换成替身（自动回收）。用法：`rec = transport(status=503)`。"""
    def _install(*, status=200, body=b'{"ok":true}', exc=None):
        rec = _TransportRecorder(status=status, body=body, exc=exc)
        monkeypatch.setattr(fetch, "_default_transport", rec)
        return rec
    return _install


class _FakeSMTP:
    """`smtplib.SMTP` 替身：记录 starttls/login/send_message，可注入异常。"""

    instances: list = []
    login_error: Exception | None = None

    def __init__(self, host, port, timeout=None) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.started_tls = False
        self.login_args: tuple | None = None
        self.messages: list = []
        _FakeSMTP.instances.append(self)

    def __enter__(self) -> _FakeSMTP:
        return self

    def __exit__(self, *exc) -> bool:
        return False

    def starttls(self) -> None:
        if _FakeSMTP.login_error is not None:
            raise _FakeSMTP.login_error
        self.started_tls = True

    def login(self, user, password) -> None:
        self.login_args = (user, password)

    def send_message(self, msg) -> None:
        self.messages.append(msg)


@pytest.fixture()
def smtp_stub(monkeypatch):
    """把 smtplib.SMTP 换成替身（自动回收），并清掉记录。"""
    _FakeSMTP.instances = []
    _FakeSMTP.login_error = None
    monkeypatch.setattr(N.smtplib, "SMTP", _FakeSMTP)
    yield _FakeSMTP
    _FakeSMTP.login_error = None


def _note() -> Notification:
    return Notification(event_id="alert_x", subject="过热告警", body="温度 78.2°C",
                        payload={"event_id": "alert_x"})


# ---------------------------------------------------------------- SSRF 门


def test_target_allowed_url_parse_failure_is_denied(monkeypatch):
    """URL 解析本身炸了也不能放行——拿不准就拒（失败关闭）。"""
    def _boom(_url):
        raise ValueError("bad url")

    monkeypatch.setattr(N, "urlparse", _boom)
    assert N._target_allowed("http://93.184.216.34/hook") == (False, "URL 解析失败")
    print("PASS test_target_allowed_url_parse_failure_is_denied")


def test_target_allowed_missing_host_is_denied(monkeypatch):
    """只有协议没有 host 的 URL → 拒绝（不能把通知发到"空目标"）。"""
    monkeypatch.setenv("DD_NOTIFY_ALLOW_PRIVATE", "1")
    assert N._target_allowed("http://") == (False, "缺少 host")
    print("PASS test_target_allowed_missing_host_is_denied")


def test_target_allowed_consults_ssrf_gate_for_public_url(monkeypatch):
    """公网目标必须**真的过** SSRF 门（此处替掉门以免用例联网做 DNS）。

    断言两件事：一是门被调用（不是被绕过），二是门放行即返回 allowed。
    """
    monkeypatch.delenv("DD_NOTIFY_ALLOW_PRIVATE", raising=False)
    seen: list[str] = []

    def _gate(url: str):
        seen.append(url)
        return SimpleNamespace(allowed=True, reason="")

    monkeypatch.setattr(N, "validate_url", _gate)
    assert N._target_allowed("https://alerts.example.com/hook") == (True, "")
    assert seen == ["https://alerts.example.com/hook"]
    print("PASS test_target_allowed_consults_ssrf_gate_for_public_url")


# ---------------------------------------------------------------- _post_json


# 用**公网 IP 字面量**（example.com 的地址）而不是 `http://x/hook`：后者解析不了，
# 会被安全门在"发请求之前"拒掉（`url_denied: 域名解析失败`），根本走不到传输层桩
# ——那测的就不是投递路径了。（"解析不了就拒"本身另有专门用例。）
def test_post_json_http_error_is_delivery_failure(transport):
    """服务端返回 5xx → ok=False + 回执带状态码（HTTP 错误现在是**普通响应**，
    不再像裸 urlopen 那样抛 HTTPError）。"""
    transport(status=500, body=b"boom")

    ok, detail, receipt = N._post_json("http://93.184.216.34/hook", {"a": 1}, timeout_s=2.5)
    assert ok is False
    assert detail == "HTTP 500"
    assert receipt == "http_500"
    print("PASS test_post_json_http_error_is_delivery_failure")


def test_post_json_non_2xx_status_is_delivery_failure(transport):
    """非 2xx 的**正常响应**同样算失败（不假装投递成功），并带上超时预算。"""
    rec = transport(status=503)

    ok, detail, receipt = N._post_json("http://93.184.216.34/hook", {"a": 1}, timeout_s=2.5)
    assert (ok, detail, receipt) == (False, "HTTP 503", "http_503")
    assert [r.timeout_s for r in rec.requests] == [2.5]
    assert rec.requests[0].method == "POST"
    assert rec.payload() == {"a": 1}
    print("PASS test_post_json_non_2xx_status_is_delivery_failure")


def test_post_json_connection_error_is_delivery_failure(transport):
    """连不上 → ok=False 且 detail 带异常类型，绝不冒充投递成功。"""
    transport(exc=ConnectionRefusedError("connection refused"))

    ok, detail, receipt = N._post_json("http://93.184.216.34/hook", {"a": 1}, timeout_s=1.0)
    assert ok is False
    assert detail.startswith("ConnectionRefusedError:")
    assert receipt == ""
    print("PASS test_post_json_connection_error_is_delivery_failure")


# ---------------------------------------------------------------- IM 通道


def test_im_private_target_denied_by_default(monkeypatch):
    """IM 群机器人目标默认过 SSRF 门：内网地址被拒（target_denied）。"""
    monkeypatch.delenv("DD_NOTIFY_ALLOW_PRIVATE", raising=False)
    monkeypatch.delenv("DD_IM_WEBHOOK_URL", raising=False)
    res = IMNotifier().send(_note(), route=Route("im", "http://127.0.0.1:9/hook"))
    assert isinstance(res, DeliveryResult) and not res.ok
    assert res.detail.startswith("target_denied")
    print("PASS test_im_private_target_denied_by_default")


def test_im_posts_text_payload_from_env_url(monkeypatch, transport):
    """路由没给 target 时回落到 `DD_IM_WEBHOOK_URL`，并 POST 文本载荷（subject+body）。"""
    monkeypatch.setenv("DD_NOTIFY_ALLOW_PRIVATE", "1")
    monkeypatch.setenv("DD_IM_WEBHOOK_URL", "http://127.0.0.1:9/hook")
    rec = transport(status=200)

    res = IMNotifier().send(_note(), route=Route("im", ""))
    assert res.ok is True and res.receipt == "http_200"
    payload = rec.payload()
    assert payload["msg_type"] == "text"
    assert "过热告警" in payload["content"]["text"]
    assert "温度 78.2°C" in payload["content"]["text"]
    print("PASS test_im_posts_text_payload_from_env_url")


# ---------------------------------------------------------------- 邮件通道


def test_email_with_route_but_no_target_fails_closed(monkeypatch):
    """配了 SMTP 但路由没给收件人 → 失败关闭（不猜收件人、不空发）。"""
    monkeypatch.setenv("DD_SMTP_HOST", "smtp.example.com")
    res = EmailNotifier().send(_note(), route=Route("email", ""))
    assert not res.ok and "no_target" in res.detail
    print("PASS test_email_with_route_but_no_target_fails_closed")


def test_email_sends_with_starttls_and_login(monkeypatch, smtp_stub):
    """SMTP 全链路（打桩）：starttls + 登录 + 发信；报文头/正文正确。"""
    monkeypatch.setenv("DD_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("DD_SMTP_PORT", "2525")
    monkeypatch.setenv("DD_SMTP_USER", "ops@example.com")
    monkeypatch.setenv("DD_SMTP_PASSWORD", "secret")
    monkeypatch.setenv("DD_SMTP_FROM", "alerts@example.com")

    res = EmailNotifier().send(_note(), route=Route("email", "oncall@example.com"))
    assert res.ok is True and res.receipt == "smtp_ok"
    assert len(smtp_stub.instances) == 1
    s = smtp_stub.instances[0]
    assert (s.host, s.port) == ("smtp.example.com", 2525)
    assert s.started_tls is True
    assert s.login_args == ("ops@example.com", "secret")
    msg = s.messages[0]
    assert msg["To"] == "oncall@example.com"
    assert msg["From"] == "alerts@example.com"
    assert msg["Subject"] == "过热告警"
    assert "温度 78.2°C" in msg.get_content()
    print("PASS test_email_sends_with_starttls_and_login")


def test_smtp_timeout_is_decoupled_from_http_timeout(monkeypatch, smtp_stub):
    """SMTP 超时走**自己的**配置项：调邮件超时不再连带放长 webhook 超时（反之亦然）。

    此前 `EmailNotifier` 直接复用 `DD_NOTIFY_HTTP_TIMEOUT_S`，两类下游只能共用一个值——
    而 SMTP 要 TLS 握手 + 认证 + 中继排队，特性与"一次 HTTP POST"完全不同。
    默认值仍是 5s（与旧行为一致），这条测的是**可分别配置**。
    """
    monkeypatch.setenv("DD_SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("DD_SMTP_TIMEOUT_S", "17")
    monkeypatch.setenv("DD_NOTIFY_HTTP_TIMEOUT_S", "3")

    from data_dog import settings

    assert settings.smtp_timeout_s() == 17.0
    assert settings.notify_http_timeout_s() == 3.0          # 两项互不影响

    EmailNotifier().send(_note(), route=Route("email", "oncall@example.com"))
    assert smtp_stub.instances[0].timeout == 17.0
    print("PASS test_smtp_timeout_is_decoupled_from_http_timeout")


def test_email_smtp_failure_is_delivery_failure(monkeypatch, smtp_stub):
    """SMTP 握手/认证失败 → ok=False 且如实带上异常类型（不吞异常）。"""
    monkeypatch.setenv("DD_SMTP_HOST", "smtp.example.com")
    monkeypatch.delenv("DD_SMTP_USER", raising=False)     # 未配用户 → 跳过 login
    smtp_stub.login_error = N.smtplib.SMTPException("tls rejected")

    res = EmailNotifier().send(_note(), route=Route("email", "oncall@example.com"))
    assert not res.ok
    assert "SMTPException" in res.detail
    print("PASS test_email_smtp_failure_is_delivery_failure")


# ---------------------------------------------------------------- 注册表兜底


class _ExplodingNotifier:
    """违反"绝不抛异常"契约的适配器——注册表必须兜住它。"""

    name = "boom"

    def send(self, note, *, route):
        raise RuntimeError("adapter exploded")


def test_registry_swallows_adapter_exception():
    """适配器抛异常时注册表兜底成 DeliveryResult(False)：通知永不拖垮调用方。"""
    reg = NotifierRegistry(notifiers={"boom": _ExplodingNotifier()})
    res = reg.send(_note(), route=Route("boom", "x"))
    assert res.ok is False
    assert "RuntimeError" in res.detail
    print("PASS test_registry_swallows_adapter_exception")
