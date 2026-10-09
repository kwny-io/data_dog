"""P2 受控引用解析（受架构 §9.1 大载荷外置 + 安全约束）。

安全约束（必须遵守）：
  - 服务端请求 URL 时**仅允许 http/https**；
  - 发请求前**校验 host**，并**拒绝 localhost、环回、私有和保留地址**（防 SSRF）；
  - 解析域名后要检查**所有解析结果**都不是内网/保留地址（防 DNS rebinding）；
  - 超时、大小上限、失败关闭。

本模块只做**校验**（validate_url）与"允许的取数决策"；真正的网络请求由调用方执行，
且必须先过 validate_url。默认**不自动联网**（离线可跑）。
"""
from __future__ import annotations

import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urlparse

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


def validate_url(url: str, *, resolver=None) -> FetchDecision:
    """校验 URL 是否可安全请求。resolver: host -> List[ip]（默认用 socket，可注入以便测试）。"""
    if not url:
        return FetchDecision(False, reason="URL 为空")
    if len(url) > _MAX_URL_LEN:
        return FetchDecision(False, url=url[:80],
                             reason=f"URL 过长（上限 {_MAX_URL_LEN} 字符）")
    if any(ch in url for ch in _BLOCKED_CHARS):
        return FetchDecision(False, url=url[:80], reason="URL 含控制字符（\\t/\\n/\\r/NUL）")

    try:
        p = urlparse(url)
        port = p.port          # 畸形端口（`:abc` / `:99999`）在这里抛 ValueError
    except Exception:
        return FetchDecision(False, reason="URL 解析失败（含畸形端口）")

    if p.scheme not in _ALLOWED_SCHEMES:
        return FetchDecision(False, url=url, reason=f"仅允许 http/https，得到 {p.scheme!r}")
    host = p.hostname
    if not host:
        return FetchDecision(False, url=url, reason="缺少 host")
    if host.lower() in _BLOCKED_HOSTNAMES:
        return FetchDecision(False, url=url, reason="host 被拒绝（localhost 类）")
    if port not in _ALLOWED_PORTS:
        return FetchDecision(False, url=url, reason=f"端口不被允许: {port}")

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
