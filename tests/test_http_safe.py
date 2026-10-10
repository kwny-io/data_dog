"""`safe_request`：安全取数原语（**逐跳复验重定向 + 把连接钉在校验过的 IP 上**）。

为什么单独一份
--------------
`validate_url` 只回答"这个 URL 能不能请求"，它**看不到** 302 的目标、也管不住
"校验之后 DNS 改口"。`tests/test_ssrf_adversarial.py` 里那两条"记录现状"的用例
（`test_redirect_and_path_confusion_are_not_inspected` / `..._toctou_has_no_pinning`）
记的正是这两个缺口。本文件把**调用侧**的修复钉住：

1. 重定向目标必须**逐跳**重新过结构检查 + IP 检查，任一跳不过就整体失败（且不再往下发）；
2. 连接目标就是**校验时那个 IP**（`resolved_ips[0]`），解析器之后改口无效；
3. 失败一律返回 `ok=False` + `error`，不抛异常；body 有上限。

全部用例**离线**：要么注入 resolver/transport，要么连本机 127.0.0.1 的真实 server。
"""
from __future__ import annotations

import ssl
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from data_dog.data_plane.fetch import (
    RawResponse,
    _PinnedHTTPConnection,
    _PinnedHTTPSConnection,
    safe_request,
)

PUBLIC_A = "93.184.216.34"      # example.com
PUBLIC_B = "1.1.1.1"
METADATA = "169.254.169.254"    # 云元数据地址（链路本地）


class _Recorder:
    """按顺序返回预设响应，并记录每次 `PinnedRequest`（不发真请求）。"""

    def __init__(self, responses: list[RawResponse], *, exc: Exception | None = None):
        self.responses = list(responses)
        self.exc = exc
        self.requests: list = []

    def __call__(self, req):
        self.requests.append(req)
        if self.exc is not None and not self.requests[1:]:
            raise self.exc
        if not self.responses:
            return RawResponse(200, b"done", {})
        return self.responses.pop(0)

    @property
    def ips(self) -> list[str]:
        return [r.ip for r in self.requests]

    @property
    def methods(self) -> list[str]:
        return [r.method for r in self.requests]


def _resolver(mapping: dict[str, list[str]]):
    return lambda host: mapping[host]


# ---------------------------------------------------------------- 逐跳复验

def test_redirect_to_metadata_is_refused_and_second_hop_never_sent():
    """公网域名 302 到云元数据地址 → **拒**，且第二跳根本没发出去。"""
    rec = _Recorder([RawResponse(302, b"", {"location": f"http://{METADATA}/latest/meta-data/"})])
    res = safe_request(
        "https://evil.example/hook", method="POST", data=b"{}",
        resolver=_resolver({"evil.example": [PUBLIC_A], METADATA: [METADATA]}),
        transport=rec)
    assert res.ok is False
    assert "169.254.169.254" in res.error and res.error.startswith("url_denied")
    assert res.hops == ("https://evil.example/hook",), "第二跳不该被记入已发送的跳"
    assert len(rec.requests) == 1, "重定向目标被拒后不得再发请求"


def test_legit_redirect_is_followed_with_per_hop_pinning():
    """合法重定向仍能用：两跳各自解析、各自钉住（不是只钉第一跳）。"""
    rec = _Recorder([
        RawResponse(302, b"", {"location": "https://ok.example/v2/hook"}),
        RawResponse(200, b'{"ok":true}', {}),
    ])
    res = safe_request(
        "https://evil.example/hook", method="POST", data=b"{}",
        resolver=_resolver({"evil.example": [PUBLIC_A], "ok.example": [PUBLIC_B]}),
        transport=rec)
    assert res.ok is True and res.status == 200 and res.body == b'{"ok":true}'
    assert res.hops == ("https://evil.example/hook", "https://ok.example/v2/hook")
    assert rec.ips == [PUBLIC_A, PUBLIC_B], "每一跳都要用自己校验出来的 IP"


def test_redirect_chain_is_capped():
    """重定向自环 → 到上限即失败，不会无限跟随。"""
    rec = _Recorder([RawResponse(302, b"", {"location": "/loop"})] * 10)
    res = safe_request("https://evil.example/loop",
                       resolver=_resolver({"evil.example": [PUBLIC_A]}),
                       transport=rec, max_redirects=2)
    assert res.ok is False and res.error == "redirect_too_many"
    assert len(rec.requests) == 3, "max_redirects=2 → 最多发 3 次（初始 + 2 跳）"


def test_303_downgrades_to_get_and_307_keeps_method():
    """303 降级为 GET 且丢掉 body；307 保留方法（与浏览器语义一致）。"""
    rec303 = _Recorder([RawResponse(303, b"", {"location": "/done"}), RawResponse(200, b"x")])
    safe_request("https://a.example/hook", method="POST", data=b"payload",
                 resolver=_resolver({"a.example": [PUBLIC_A]}), transport=rec303)
    assert rec303.methods == ["POST", "GET"]
    assert rec303.requests[1].body is None

    rec307 = _Recorder([RawResponse(307, b"", {"location": "/done"}), RawResponse(200, b"x")])
    safe_request("https://a.example/hook", method="POST", data=b"payload",
                 resolver=_resolver({"a.example": [PUBLIC_A]}), transport=rec307)
    assert rec307.methods == ["POST", "POST"]
    assert rec307.requests[1].body == b"payload"


# ---------------------------------------------------------------- 钉住（TOCTOU）

def test_connection_is_pinned_to_validated_ip_when_resolver_changes_later():
    """**钉住**：解析器先给公网/回环，之后改口，也影响不了这次请求。

    行为版：真连本机 server。若实现像 `urlopen` 那样在建连时**再解析一次**，
    第二次会拿到 `10.255.255.1` 而连不上 → 本用例失败。
    """
    hits: list[bytes] = []
    seen: list[str] = []

    class _H(BaseHTTPRequestHandler):
        def do_POST(self):                                  # noqa: N802
            n = int(self.headers.get("Content-Length", 0) or 0)
            hits.append(self.rfile.read(n))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":true}')

        def log_message(self, *a):                          # 静音
            pass

    srv = HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    def flaky(host: str) -> list[str]:
        seen.append(host)
        # 第一次（校验时）给回环；之后（若有人再解析）改口到不可达地址
        return ["127.0.0.1"] if len(seen) == 1 else ["10.255.255.1"]

    try:
        res = safe_request(f"http://pinned.test:{port}/hook", method="POST",
                           data=b'{"a":1}', resolver=flaky, allow_private=True,
                           timeout_s=3.0)
    finally:
        srv.shutdown()

    assert res.ok is True, res.error
    assert hits == [b'{"a":1}'], "请求必须落在被钉住的 IP 上（且 body 完整）"
    assert res.resolved_ip == "127.0.0.1"
    assert seen == ["pinned.test"], "只解析一次——建连不再重新解析（TOCTOU 窗口关闭）"


def test_https_pins_ip_but_keeps_hostname_for_sni_and_cert():
    """TLS 不变式：**连 IP，但 SNI/证书校验用主机名**（否则要么验不过、要么只能关校验）。

    第二条断言守的是"别为了连 IP 就把校验关掉"：`_ctx` 必须是默认上下文
    （`CERT_REQUIRED` + `check_hostname=True`），而不是某个放宽过的上下文。
    """
    conn = _PinnedHTTPSConnection("api.example.com", PUBLIC_A, port=443, timeout=1.0)
    assert conn.host == "api.example.com" and conn._pin_ip == PUBLIC_A
    assert conn._ctx.verify_mode == ssl.CERT_REQUIRED
    assert conn._ctx.check_hostname is True
    plain = _PinnedHTTPConnection("api.example.com", PUBLIC_A, port=80, timeout=1.0)
    assert plain.host == "api.example.com" and plain._pin_ip == PUBLIC_A


# ---------------------------------------------------------------- 失败关闭与上限

def test_unresolvable_host_is_denied_before_any_io():
    """解析不出地址 → 在**发请求之前**就拒（拿不准就拒，不是"先试了再说"）。"""
    rec = _Recorder([RawResponse(200, b"x")])
    res = safe_request("https://nope.example/hook",
                       resolver=lambda host: [], transport=rec)
    assert res.ok is False and "域名解析失败" in res.error
    assert rec.requests == [], "被拒的 URL 绝不许发出任何请求"


def test_transport_exception_is_reported_not_raised():
    """传输层抛异常 → 收敛成 ok=False + error（调用方按各自契约处理，不炸出去）。"""
    rec = _Recorder([], exc=TimeoutError("timed out"))
    res = safe_request("https://a.example/hook",
                       resolver=_resolver({"a.example": [PUBLIC_A]}), transport=rec)
    assert res.ok is False and res.error.startswith("TimeoutError:")


def test_non_http_scheme_and_control_chars_are_denied():
    for bad in ("file:///etc/passwd", "gopher://a.example/", "ftp://a.example/x"):
        res = safe_request(bad, resolver=_resolver({"a.example": [PUBLIC_A]}))
        assert res.ok is False and "url_denied" in res.error
    res = safe_request("http://a.example/\r\nX-Evil: 1", resolver=_resolver({}))
    assert res.ok is False and "控制字符" in res.error


def test_response_body_is_capped():
    """响应体有上限（docstring 宣称的"大小上限"要在取数层同样成立）。"""
    rec = _Recorder([RawResponse(200, b"A" * 200_000, {})])
    res = safe_request("https://a.example/hook",
                       resolver=_resolver({"a.example": [PUBLIC_A]}),
                       transport=rec, max_bytes=1024)
    assert res.ok is True and len(res.body) == 1024


def test_allow_private_is_an_explicit_trust_relaxation():
    """**诚实边界**：`allow_private=True` 是显式放行（对应 `DD_NOTIFY_ALLOW_PRIVATE=1`），
    它只跳过"解析结果必须是公网"这一条，结构检查与钉住照旧。所以它**也能**指向链路
    本地的元数据地址——这是该开关的既有语义，只应给本机/受信内网用。
    """
    rec = _Recorder([RawResponse(200, b"ok")])
    res = safe_request(f"http://{METADATA}/latest/meta-data/", resolver=_resolver({METADATA: [METADATA]}),
                       transport=rec, allow_private=True)
    assert res.ok is True and res.resolved_ip == METADATA
    # 默认（不放行）则拒
    res2 = safe_request(f"http://{METADATA}/latest/meta-data/",
                        resolver=_resolver({METADATA: [METADATA]}), transport=rec)
    assert res2.ok is False and "非公网地址" in res2.error


def test_host_header_keeps_hostname_and_ports_are_whitelisted():
    """Host 头保留主机名（含非默认端口），而不是暴露被钉住的 IP。

    注意非默认端口只在 `allow_private=True`（本机/受信）下可达——默认档的端口白名单
    只放 80/443，这正是下面第二条断言。
    """
    rec = _Recorder([RawResponse(200, b"ok")])
    safe_request("http://a.example:8080/hook", resolver=_resolver({"a.example": [PUBLIC_A]}),
                 transport=rec, allow_private=True)
    assert rec.requests[0].host_header == "a.example:8080"
    assert rec.requests[0].path == "/hook"

    # 默认档：奇怪端口一律拒（不给"指向内部服务"留口子）
    rec2 = _Recorder([RawResponse(200, b"ok")])
    res = safe_request("http://a.example:8080/hook",
                       resolver=_resolver({"a.example": [PUBLIC_A]}), transport=rec2)
    assert res.ok is False and "端口不被允许" in res.error
    assert rec2.requests == []
