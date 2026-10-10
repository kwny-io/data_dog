"""P2 受控引用解析（受架构 §9.1 大载荷外置 + 安全约束）。

安全约束（必须遵守）：
  - 服务端请求 URL 时**仅允许 http/https**；
  - 发请求前**校验 host**，并**拒绝 localhost、环回、私有和保留地址**（防 SSRF）；
  - 解析域名后要检查**所有解析结果**都不是内网/保留地址（防 DNS rebinding）；
  - 超时、大小上限、失败关闭。

本模块提供两样东西：
  1. `validate_url`（纯校验，不联网）——"这个 URL 能不能请求"；
  2. `safe_request`（安全取数原语）——真正发请求时**必须**用它，而不是直接用
     `urllib.request.urlopen`。它补上了 `validate_url` 单独做不到的两件事：
       - **逐跳复验重定向**：`urlopen` 默认跟随 3xx 且不看目标，于是一个攻击者可控的
         公网域名回 `302 http://169.254.169.254/…` 就能读到云凭据；
       - **把连接钉在校验过的 IP 上**：`validate_url` 与真正建连之间的第二次 DNS 解析
         是经典 TOCTOU 窗口（DNS 重绑定）。钉住后解析器改口也不影响本次连接。

分层说明：`safe_request` 刻意留在这里（而不是 `core/`），因为 `alerting.notifiers` 与
`evidence.change_source` **本来就**从本模块取 `validate_url`；放这里不新增任何跨层依赖
（`tests/test_layering.py` 守着这一点）。"""
from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from ..errors import ToolErrorCode, err

_ALLOWED_SCHEMES = {"http", "https"}
# 仅允许的端口（避免指向奇怪的内部服务）；80/443 为常规
_ALLOWED_PORTS = {80, 443, None}
_BLOCKED_HOSTNAMES = {"localhost", "localhost.localdomain", "metadata.google.internal"}
# URL 长度上限（docstring 一直宣称"大小上限"，但此前没有实现）
_MAX_URL_LEN = 2048
# 控制字符：`urlsplit` 会把 \t\n\r 从 authority 里**剥掉**，于是"被校验的字符串"
# 与"被请求的字符串"不再一致（解析差分）。拿不准就拒，而不是替调用方规范化。
_BLOCKED_CHARS = ("\t", "\n", "\r", "\x00")


@dataclass
class FetchDecision:
    allowed: bool
    url: str = ""
    reason: str = ""
    resolved_ips: tuple[str, ...] = ()


def _is_public_ip(ip: str) -> bool:
    """是否**可全球路由**的地址（黑名单 + 白名单取交集）。

    为什么两者都要：单用六项黑名单会漏掉 CGNAT（100.64.0.0/10：`is_private` 为 False、
    但 `is_global` 也为 False，实测被放行）；单用 `is_global` 又会放过**多播**
    （`ff02::1` 在 CPython 里 `is_global` 竟然是 True）与 **NAT64**（`64:ff9b::/96`
    内嵌 IPv4，能借道打到内网 v4）。故：先按黑名单排除明显非公网段，再要求 `is_global`。
    方向始终是失败关闭——拿不准就拒。
    IPv4-mapped IPv6（`::ffff:127.0.0.1`）在 Python 3.12.4+ 已按映射后的 IPv4 判定。
    """
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if (addr.is_private or addr.is_loopback or addr.is_link_local
            or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
        return False
    return addr.is_global


def _basic_url_checks(url: str, *, enforce_port: bool = True) -> tuple[str, int | None, str]:
    """URL 的**结构性**检查（长度/控制字符/协议/host/端口）。

    返回 `(host, port, reason)`；`reason` 非空即拒。与 IP 是否公网无关——那是
    `validate_url` / `safe_request` 的下一层判断。抽出来是为了让"显式放行内网"
    档（`allow_private=True`）复用同一套结构检查，而不是各写一份。
    """
    if not url:
        return "", None, "URL 为空"
    if any(ch in url for ch in _BLOCKED_CHARS):
        return "", None, "URL 含控制字符（\\t/\\n/\\r/NUL）"
    try:
        p = urlparse(url)
        port = p.port          # 畸形端口（`:abc` / `:99999`）在这里抛 ValueError
    except Exception:
        return "", None, "URL 解析失败（含畸形端口）"
    if p.scheme not in _ALLOWED_SCHEMES:
        return "", None, f"仅允许 http/https，得到 {p.scheme!r}"
    host = p.hostname
    if not host:
        return "", None, "缺少 host"
    if host.lower() in _BLOCKED_HOSTNAMES:
        return "", None, "host 被拒绝（localhost 类）"
    if enforce_port and port not in _ALLOWED_PORTS:
        return "", None, f"端口不被允许: {port}"
    return host, port, ""


def validate_url(url: str, *, resolver=None) -> FetchDecision:
    """校验 URL 是否可安全请求。resolver: host -> List[ip]（默认用 socket，可注入以便测试）。"""
    if len(url) > _MAX_URL_LEN:
        return FetchDecision(False, url=url[:80],
                             reason=f"URL 过长（上限 {_MAX_URL_LEN} 字符）")
    host, _port, reason = _basic_url_checks(url)
    if reason:
        return FetchDecision(False, url=url, reason=reason)

    # 解析所有 IP，全部必须为公网地址（防 SSRF / DNS rebinding）
    ips = _resolve(host, resolver)
    if not ips:
        # 空答案 = 解析不出任何地址：**失败关闭**（此前空列表会被当成"没有坏 IP"而放行，
        # 这正是最省事的 DNS 重绑定起手式）
        return FetchDecision(False, url=url, reason="域名解析失败（无解析结果）")
    bad = [ip for ip in ips if not _is_public_ip(ip)]
    if bad:
        return FetchDecision(False, url=url, reason=f"解析到非公网地址: {bad}",
                             resolved_ips=tuple(ips))
    return FetchDecision(True, url=url, resolved_ips=tuple(ips))


def _resolve(host: str, resolver) -> list[str] | None:
    # 若 host 本身就是 IP 字面量
    try:
        ipaddress.ip_address(host)
        return [host]
    except ValueError:
        pass
    if resolver is not None:
        try:
            got = list(resolver(host))
        except Exception:
            return None
        return got or None                 # 空答案 → 失败（不返回 [] 让调用方误判为"全公网"）
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return None
    ips = sorted({str(info[4][0]) for info in infos})
    return ips or None


def safe_get_allowed(url: str, *, resolver=None) -> tuple[bool, object]:
    """对外统一入口：返回 (ok, url) 或 (False, err)。调用方在**真正发请求前**必须先过它。"""
    d = validate_url(url, resolver=resolver)
    if not d.allowed:
        return False, err(ToolErrorCode.DENIED, f"URL 被安全策略拒绝: {d.reason}")
    return True, d.url


# ---------------------------------------------------------------- 安全取数原语
#
# 为什么需要它（本模块此前自认的两处缺口，见 tests/test_ssrf_adversarial.py）
#   ① **重定向逐跳不复验**：`urllib.request.urlopen` 默认跟随 3xx，于是
#      "公网域名 → 302 → 169.254.169.254" 可以绕过只校验初始 URL 的门；
#   ② **校验与建连之间没有钉住**：`resolve(host)` 在校验时做过一次，urlopen 建连时
#      会**再解析一次** —— 这就是 DNS 重绑定的 TOCTOU 窗口。
# 两者都在这一层解决：自己解析、自己连、每跳重新校验。

_REDIRECT_STATUS = {301, 302, 303, 307, 308}
_MAX_REDIRECTS = 3
_MAX_BYTES = 64 * 1024


@dataclass
class PinnedRequest:
    """交给 transport 的一次请求：**连接目标已钉死**（`ip`），主机名只用于 Host/SNI/证书。"""

    url: str
    host: str
    ip: str
    scheme: str
    port: int
    method: str
    path: str
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | None = None
    timeout_s: float = 5.0

    @property
    def host_header(self) -> str:
        default_port = 443 if self.scheme == "https" else 80
        return self.host if self.port == default_port else f"{self.host}:{self.port}"


@dataclass
class RawResponse:
    status: int
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class SafeResponse:
    ok: bool
    status: int = 0
    body: bytes = b""
    final_url: str = ""
    error: str = ""
    hops: tuple[str, ...] = ()
    resolved_ip: str = ""


class _PinnedHTTPConnection(http.client.HTTPConnection):
    """连到**已校验的那个 IP**（而不是再解析一次主机名）。"""

    def __init__(self, host: str, ip: str, **kw) -> None:
        super().__init__(host, **kw)
        self._pin_ip = ip

    def connect(self) -> None:                     # pragma: no cover - 真建连
        self.sock = socket.create_connection((self._pin_ip, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """连到钉住的 IP，但 **SNI 与证书校验仍按主机名**做。

    这是"钉住"能同时保住安全性的关键：连的是已校验的地址，验证的是原本要访问的名字。
    若反过来（连 IP 且按 IP 验证证书），要么验不过、要么只能关掉校验。
    """

    def __init__(self, host: str, ip: str, *, context=None, **kw) -> None:
        super().__init__(host, context=context, **kw)
        self._pin_ip = ip
        self._ctx = context if context is not None else ssl.create_default_context()

    def connect(self) -> None:                     # pragma: no cover - 真建连
        self.sock = socket.create_connection((self._pin_ip, self.port), self.timeout)
        self.sock = self._ctx.wrap_socket(self.sock, server_hostname=self.host)


def _default_transport(req: PinnedRequest) -> RawResponse:
    """默认传输：`http.client` + 钉住的 IP。

    刻意不用 `urllib.request.urlopen`：它会自己解析 DNS 并自动跟随重定向，
    正好是上面 ①② 两个缺口的来源。这里把"连哪个 IP"变成显式参数。
    """
    cls = _PinnedHTTPSConnection if req.scheme == "https" else _PinnedHTTPConnection
    conn = cls(req.host, req.ip, port=req.port, timeout=req.timeout_s)
    try:
        conn.putrequest(req.method, req.path, skip_host=True, skip_accept_encoding=True)
        conn.putheader("Host", req.host_header)
        for key, value in req.headers.items():
            conn.putheader(key, value)
        if req.body is not None:
            # 手工走 putrequest/endheaders 时**必须自己写 Content-Length**：
            # `http.client` 只在 `request()` 那条高层路径上自动补。漏了它，服务端会按
            # 0 长度读 body（实测：本地 server 收到空 {}，测试随即 KeyError）。
            conn.putheader("Content-Length", str(len(req.body)))
        conn.endheaders(req.body)
        resp = conn.getresponse()
        body = resp.read(_MAX_BYTES)
        return RawResponse(resp.status, body,
                           {k.lower(): v for k, v in resp.getheaders()})
    finally:
        conn.close()


def safe_request(url: str, *, method: str = "POST", data: bytes | None = None,
                 headers: dict[str, str] | None = None, timeout_s: float = 5.0,
                 max_redirects: int = _MAX_REDIRECTS, max_bytes: int = _MAX_BYTES,
                 allow_private: bool = False, resolver=None,
                 transport=None) -> SafeResponse:
    """安全地发一次请求：**每跳重新校验 + 把连接钉在校验过的 IP 上**。

    - 失败一律**不抛异常**，返回 `ok=False` 与 `error`（调用方按各自契约处理）；
    - `allow_private=True` 只给**显式放行**的本地/受信场景（`DD_NOTIFY_ALLOW_PRIVATE=1`）：
      仍做结构检查与钉住，只是不再要求解析结果是公网地址（本机端口也不受白名单限制）；
    - `transport` 可注入（测试用），签名 `(PinnedRequest) -> RawResponse`，
      默认实现见 `_default_transport`；
    - 3xx 只跟随 `max_redirects` 跳，每跳都重跑结构检查 + IP 检查；
      `301/302/303` 按浏览器语义降级为 GET（不带 body），`307/308` 保留方法与 body。
    """
    send = transport if transport is not None else _default_transport
    hops: list[str] = []
    current = url
    body = data
    cur_method = method
    limit = max(0, int(max_redirects))

    for hop in range(limit + 1):
        host, port, reason = _basic_url_checks(current, enforce_port=not allow_private)
        if reason:
            return SafeResponse(False, error=f"url_denied: {reason}", hops=tuple(hops))
        ips = _resolve(host, resolver)
        if not ips:
            return SafeResponse(False, error="url_denied: 域名解析失败（无解析结果）",
                                hops=tuple(hops))
        if allow_private:
            ip = ips[0]
        else:
            bad = [x for x in ips if not _is_public_ip(x)]
            if bad:
                return SafeResponse(False, error=f"url_denied: 解析到非公网地址 {bad}",
                                    hops=tuple(hops))
            ip = ips[0]                    # 全部已确认公网 → 取第一条并钉住

        parsed = urlparse(current)
        path = parsed.path or "/"
        if parsed.query:
            path = f"{path}?{parsed.query}"
        req = PinnedRequest(
            url=current, host=host, ip=ip, scheme=parsed.scheme,
            port=port if port is not None else (443 if parsed.scheme == "https" else 80),
            method=cur_method, path=path,
            headers=dict(headers or {}), body=body, timeout_s=timeout_s)
        hops.append(current)
        try:
            raw = send(req)
        except Exception as e:                          # noqa: BLE001 - 传输失败归一化
            return SafeResponse(False, error=f"{type(e).__name__}: {e}",
                                final_url=current, hops=tuple(hops), resolved_ip=ip)

        location = raw.headers.get("location", "")
        if raw.status in _REDIRECT_STATUS and location:
            if hop >= limit:
                return SafeResponse(False, status=raw.status, final_url=current,
                                    error="redirect_too_many", hops=tuple(hops),
                                    resolved_ip=ip)
            if raw.status in (301, 302, 303) and cur_method.upper() != "GET":
                cur_method, body = "GET", None
            current = urljoin(current, location)
            continue

        ok = 200 <= raw.status < 300
        return SafeResponse(ok, status=raw.status, body=raw.body[:max_bytes],
                            final_url=current, hops=tuple(hops), resolved_ip=ip,
                            error="" if ok else f"HTTP {raw.status}")

    return SafeResponse(False, error="redirect_too_many", hops=tuple(hops))
