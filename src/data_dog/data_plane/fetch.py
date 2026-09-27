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


@dataclass
class FetchDecision:
    allowed: bool
    url: str = ""
    reason: str = ""
    resolved_ips: tuple[str, ...] = ()


def _is_public_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return not (
        addr.is_private or addr.is_loopback or addr.is_link_local
        or addr.is_reserved or addr.is_multicast or addr.is_unspecified
    )


def validate_url(url: str, *, resolver=None) -> FetchDecision:
    """校验 URL 是否可安全请求。resolver: host -> List[ip]（默认用 socket，可注入以便测试）。"""
    try:
        p = urlparse(url)
    except Exception:
        return FetchDecision(False, reason="URL 解析失败")

    if p.scheme not in _ALLOWED_SCHEMES:
        return FetchDecision(False, url=url, reason=f"仅允许 http/https，得到 {p.scheme!r}")
    host = p.hostname
    if not host:
        return FetchDecision(False, url=url, reason="缺少 host")
    if host.lower() in _BLOCKED_HOSTNAMES:
        return FetchDecision(False, url=url, reason="host 被拒绝（localhost 类）")
    if p.port not in _ALLOWED_PORTS:
        return FetchDecision(False, url=url, reason=f"端口不被允许: {p.port}")

    # 解析所有 IP，全部必须为公网地址（防 SSRF / DNS rebinding）
    ips = _resolve(host, resolver)
    if ips is None:
        return FetchDecision(False, url=url, reason="域名解析失败")
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
            return list(resolver(host))
        except Exception:
            return None
    try:
        infos = socket.getaddrinfo(host, None)
        return sorted({str(info[4][0]) for info in infos})
    except Exception:
        return None


def safe_get_allowed(url: str, *, resolver=None) -> tuple[bool, object]:
    """对外统一入口：返回 (ok, url) 或 (False, err)。调用方在**真正发请求前**必须先过它。"""
    d = validate_url(url, resolver=resolver)
    if not d.allowed:
        return False, err(ToolErrorCode.DENIED, f"URL 被安全策略拒绝: {d.reason}")
    return True, d.url
