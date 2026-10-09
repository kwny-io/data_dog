"""SSRF 对抗面测试：`data_plane/fetch.validate_url` 的**真实**接受/拒绝行为。

为什么单独开一个文件
--------------------
`tests/test_data_plane.py` 里的 3 条 SSRF 用例只覆盖"明显内网/环回被拒 + 公网字面量被放行
+ 域名解析到内网被拒"。真实攻击面在**编码/解析差分**上：十进制/八进制/十六进制 IP、
IPv6 映射的 IPv4、userinfo `@` 混淆、控制字符、空 DNS 答案、重定向跳变、畸形端口。本文件
把这些形式逐条钉住。

两条纪律（与本仓库"绝不为了变绿而放宽断言"一致）
------------------------------------------------
1. **离线**：任何测试都不得真的走 DNS。autouse 夹具把 `socket.getaddrinfo` 换成"记录 +
   `gaierror`"，若有用例穿透到真实解析，teardown 直接断言失败——既避免联网，也避免
   "本机 DNS 恰好泛解析"造成的假绿。
2. **如实记录**：发现校验函数行为与"防 SSRF/失败关闭"的宣称不符时，**不改 src**，而是写
   "记录当前真实行为"的用例，并在 docstring 里标明这是缺陷/缺口（见文件末尾的
   `test_*_current_behavior` 系列）。这些断言描述的是事实，不是期望值。
"""
from __future__ import annotations

import ipaddress
import socket

import pytest

from data_dog.data_plane import fetch
from data_dog.data_plane.fetch import safe_get_allowed, validate_url


class _Resolver:
    """可注入的解析器替身：记录被查询的 host，并按预设返回/抛出。

    `answers` 为 Exception 实例时抛出它（模拟解析失败）。记录 host 列表是为了证明
    "校验的到底是哪一份名字"——这正是编码/解析差分类 SSRF 的关键证据。
    """

    def __init__(self, answers):
        self.answers = answers
        self.hosts: list[str] = []

    def __call__(self, host: str) -> list[str]:
        self.hosts.append(host)
        if isinstance(self.answers, Exception):
            raise self.answers
        return list(self.answers)


@pytest.fixture(autouse=True)
def _no_real_dns(monkeypatch):
    """封网：默认解析路径一律记录并失败关闭，测试结束若有穿透则报错。"""
    escaped: list[str] = []

    def _blocked(host, *a, **kw):
        escaped.append(host)
        raise socket.gaierror("测试禁止真实 DNS 解析")

    monkeypatch.setattr(fetch.socket, "getaddrinfo", _blocked)
    yield escaped
    assert escaped == [], f"有用例穿透到真实 DNS 解析：{escaped}"


# ------------------------------------------------------- IPv6 映射的 IPv4

def test_ipv6_mapped_ipv4_rejected():
    """`::ffff:a.b.c.d` 这类映射写法不能把内网 IPv4 偷渡进来。

    Python 的 `ipaddress` 对映射地址会**同时**报告 is_private/is_loopback（例如
    `::ffff:7f00:1` 是 loopback），所以字面量路径和解析器答案路径都必须被拒。
    """
    literals = [
        "http://[::ffff:127.0.0.1]/",
        "http://[::ffff:10.0.0.1]/",
        "http://[::ffff:192.168.1.1]/",
        "http://[::ffff:169.254.169.254]/",     # 云元数据服务
        "http://[::ffff:0.0.0.0]/",
        "http://[0:0:0:0:0:ffff:7f00:1]/",      # 同一地址的十六进制写法
        "http://[::ffff:0:127.0.0.1]/",
    ]
    for u in literals:
        d = validate_url(u)
        assert not d.allowed, f"{u} 必须被拒，实际放行"
        assert "非公网" in d.reason

    # 映射地址也可能来自解析器答案（域名指向 AAAA 映射地址）
    d = validate_url("https://evil.example/x", resolver=_Resolver(["::ffff:127.0.0.1"]))
    assert not d.allowed and "非公网" in d.reason

    # 记录当前行为（fail-closed 方向的过度拦截，不是漏洞）：::ffff:0:0/96 在 Python 里
    # 命中 is_reserved，于是**公网** IPv4 的映射形式（如 ::ffff:8.8.8.8）也被拒。
    assert not validate_url("http://[::ffff:8.8.8.8]/").allowed
    print("PASS test_ipv6_mapped_ipv4_rejected")


# ------------------------------------------------------- 进制/IP 简写混淆

def test_decimal_octal_hex_and_short_form_ip_rejected():
    """十进制整数、八进制、十六进制、`127.1`、末尾点等非标准 IP 写法都必须失败关闭。

    校验函数用 `ipaddress.ip_address` 判字面量，上述写法**都不是**合法 IP 字面量 → 落到
    解析器。因此有两条真实路径，两条都必须拒：
      (a) 解析器不认这些形式（本机 Windows 实测）→ 域名解析失败；
      (b) 解析器按 inet_aton 语义把它们规整成 127.0.0.1（glibc 行为）→ 解析到非公网。
    两条都覆盖，避免用例依赖本机解析器脾气。
    """
    forms = ["2130706433", "017700000001", "0x7f000001", "0x7F000001",
             "0x7f.0.0.1", "127.1", "127.0.0.1.", "0"]
    dead = _Resolver(socket.gaierror("no such host"))
    for h in forms:
        d = validate_url(f"http://{h}/", resolver=dead)
        assert not d.allowed and "解析失败" in d.reason, f"{h} 应因解析失败被拒"

    loop = _Resolver(["127.0.0.1"])
    for h in forms:
        d = validate_url(f"http://{h}/", resolver=loop)
        assert not d.allowed and "非公网" in d.reason, f"{h} 解析成环回后仍必须拒"
    # 这些混淆形式确实被送进了解析器 → 校验不是字符串黑名单，而是看解析结果。
    # host 已被 urlsplit 小写化（0x7F000001 → 0x7f000001），故按小写比对。
    assert loop.hosts == [h.lower() for h in forms]
    print("PASS test_decimal_octal_hex_and_short_form_ip_rejected")


# ------------------------------------------------------- localhost 变体

def test_localhost_variants_and_unspecified_rejected():
    """localhost 家族、未指定地址、IPv6 环回/未指定、非 127.0.0.1 的环回都要拒。"""
    assert not validate_url("http://localhost/").allowed
    assert "localhost 类" in validate_url("http://LOCALHOST/").reason   # 大小写不绕过
    assert not validate_url("http://localhost.localdomain/").allowed
    assert not validate_url("http://metadata.google.internal/").allowed
    assert not validate_url("http://0.0.0.0/").allowed                  # 未指定
    assert not validate_url("http://[::1]/").allowed                    # IPv6 环回
    assert not validate_url("http://[::]/").allowed                     # IPv6 未指定
    assert not validate_url("http://127.0.0.2/").allowed                # 整个 127/8

    # 尾点（DNS 绝对名）绕过了名字黑名单，但解析结果仍被查 → 拒
    d = validate_url("http://localhost./", resolver=_Resolver(["127.0.0.1", "::1"]))
    assert not d.allowed and "非公网" in d.reason
    # 黑名单里没有的元数据域名同理：靠"解析到 169.254/16"拦住
    d2 = validate_url("http://metadata.google.internal./",
                      resolver=_Resolver(["169.254.169.254"]))
    assert not d2.allowed and "169.254.169.254" in d2.reason
    print("PASS test_localhost_variants_and_unspecified_rejected")


# ------------------------------------------------------- userinfo `@` 混淆

def test_userinfo_at_obfuscation_validates_authority_not_userinfo():
    """`@` 之前的 userinfo 不参与校验，被校验的是 authority 里的真实 host。

    这是正确的方向：`http://<内网IP>@<公网域名>/` 里真正会连的是公网域名，所以放行；
    而 `http://<任意>@<内网IP>/` 必须按内网拒（连解析都不问，因为字面量直接可判）。
    """
    rec = _Resolver(["93.184.216.34"])
    d = validate_url("http://127.0.0.1@evil.example/", resolver=rec)
    assert d.allowed and rec.hosts == ["evil.example"]

    rec2 = _Resolver(["93.184.216.34"])
    d2 = validate_url("http://evil.example@127.0.0.1/", resolver=rec2)
    assert not d2.allowed and rec2.hosts == []          # 字面量路径，解析器都不该被问

    # 反斜杠不是 userinfo 分隔符（urllib 语义）→ host 仍是 127.0.0.1 → 拒。
    # 记录风险：若有下游客户端按 WHATWG 把 `\` 当 `/`，它会连到 evil.example，
    # 而校验看到的是 127.0.0.1——两个方向都不安全，属解析差分（详见报告）。
    d3 = validate_url("http://evil.example\\@127.0.0.1/", resolver=rec2)
    assert not d3.allowed

    # userinfo 里塞端口也过同一套端口白名单
    d4 = validate_url("http://user:pass@93.184.216.34:8080/")
    assert not d4.allowed and "端口" in d4.reason
    print("PASS test_userinfo_at_obfuscation_validates_authority_not_userinfo")


# ------------------------------------------------------- scheme 白名单

def test_non_http_scheme_matrix_rejected():
    """只允许 http/https：危险 scheme、无 scheme、畸形 URL 全部失败关闭。"""
    urls = [
        "file:///etc/passwd",                  # 本地文件
        "ftp://example.com/x",
        "gopher://127.0.0.1:80/_x",            # 老牌 SSRF 通道
        "dict://127.0.0.1:11211/",             # 打内存数据库
        "ldap://127.0.0.1/",
        "data:text/plain,hello",               # 内联数据
        "jar:http://127.0.0.1!/x",             # Java 归档包装
        "javascript:alert(1)",
        "//evil.example/x",                    # 协议相对：urlparse 的 scheme 为空
        "not a url",
        "",
        "http:/127.0.0.1/",                    # 少一条斜杠 → 没有 host
        "http:///127.0.0.1",
    ]
    for u in urls:
        d = validate_url(u)
        assert not d.allowed, f"{u!r} 必须被拒"
    # scheme 大小写不构成绕过：urlsplit 会小写化，于是仍走 http(s) 校验 → 内网照样拒
    assert not validate_url("HTTP://127.0.0.1/").allowed
    assert not validate_url("HtTpS://127.0.0.1/").allowed
    # 反向对照：大写 scheme + 公网字面量仍应放行（不是靠"拒一切大写"过关）
    assert validate_url("HtTpS://93.184.216.34/").allowed
    print("PASS test_non_http_scheme_matrix_rejected")


# ------------------------------------------------------- 重定向 / 路径混淆

def test_redirect_and_path_confusion_are_not_inspected():
    """记录当前真实行为（**安全缺口，未修**）：只校验初始 URL 的 authority。

    内网/元数据地址藏在 query、path、甚至 302 重定向目标里时，`validate_url` 完全看不见：
    它拿到的 host 始终是 `evil.example`，于是"放行"。这不是测试放宽——这是被测函数的能力
    边界。缺口在于**调用方**：`alerting.notifiers._post_json` 与
    `evidence.change_source.WebhookChangeSource._post` 都用 `urllib.request.urlopen`，
    默认跟随 3xx 重定向，且没有任何一跳重新过 `validate_url`。于是一个攻击者可控的公网
    域名（或一个被接管的可信 webhook）回 302 到 169.254.169.254 即可读到云凭据。
    修法方向（不在本次范围，见报告）：禁用自动重定向，或在 opener 的
    `redirect_request` 里对每一跳重新校验。
    """
    rec = _Resolver(["93.184.216.34"])
    for u in [
        "https://evil.example/r?to=http://169.254.169.254/latest/meta-data/",
        "https://evil.example//169.254.169.254/latest/meta-data/",
        "https://evil.example/http://169.254.169.254/",
        "https://evil.example/..%2f..%2f169.254.169.254/",
    ]:
        d = validate_url(u, resolver=rec)
        assert d.allowed, "记录事实：路径/query 里的内网地址不参与 authority 校验"
        assert rec.hosts[-1] == "evil.example"
    print("PASS test_redirect_and_path_confusion_are_not_inspected")


# ------------------------------------------------------- 超长 URL

def test_overlong_url_has_no_length_cap():
    """记录当前真实行为（**加固缺口**）：实现里没有任何 URL 长度上限。

    模块 docstring 宣称"超时、大小上限、失败关闭"，但 `validate_url` 里既无长度检查也无
    大小/超时逻辑——超长 URL 唯一的"拦法"是解析失败（而 IP 字面量 host 根本不解析）。
    后果：10 万字符的 URL 一路放行，会把长度成本转嫁给下游（日志/正则/上报）。
    """
    assert validate_url("https://93.184.216.34/" + "a" * 100_000).allowed
    assert validate_url("https://93.184.216.34/?q=" + "b" * 100_000).allowed

    long_host = "https://" + "a" * 20_000 + ".example/"
    assert validate_url(long_host, resolver=_Resolver(["93.184.216.34"])).allowed
    d = validate_url(long_host, resolver=_Resolver(socket.gaierror("name too long")))
    assert not d.allowed and "解析失败" in d.reason
    print("PASS test_overlong_url_has_no_length_cap")


# ------------------------------------------------------- DNS 重绑定

def test_dns_rebinding_any_private_answer_rejects():
    """多答案域名：只要**任意一条**答案是内网/保留地址就拒（不取第一条、不投票）。"""
    d = validate_url("https://evil.example/x",
                     resolver=_Resolver(["93.184.216.34", "10.0.0.9"]))
    assert not d.allowed and "10.0.0.9" in d.reason
    # 答案顺序无关
    d2 = validate_url("https://evil.example/x",
                      resolver=_Resolver(["10.0.0.9", "93.184.216.34"]))
    assert not d2.allowed
    # 混合 IPv4/IPv6 也逐条查
    d3 = validate_url("https://evil.example/x",
                      resolver=_Resolver(["169.254.169.254", "2606:2800:220:1::1"]))
    assert not d3.allowed
    # 全公网 → 放行，且 resolved_ips 保留全部答案（唯一可用于"钉住连接目标"的字段）
    ok = _Resolver(["93.184.216.34", "1.1.1.1"])
    d4 = validate_url("https://evil.example/x", resolver=ok)
    assert d4.allowed and d4.resolved_ips == ("93.184.216.34", "1.1.1.1")
    # 畸形答案（解析器被投毒）→ 失败关闭
    assert not validate_url("https://evil.example/x", resolver=_Resolver(["not-an-ip"])).allowed
    print("PASS test_dns_rebinding_any_private_answer_rejects")


def test_dns_rebinding_toctou_has_no_pinning():
    """记录当前真实行为（**安全缺口，未修**）：校验与取数之间没有解析结果钉住。

    `validate_url` 无缓存、无 repeated-call 记忆：同名第一次解析到公网 → 放行；第二次
    （真正发请求时）解析已切到内网 → 拒。也就是说"先校验、后取数"之间存在经典 TOCTOU
    窗口。防御手段本可以是 `FetchDecision.resolved_ips`（钉住 IP 或直接连 IP），但没有
    任何调用方用它：`pipeline._payload` 只看 `allowed`、`notifiers._target_allowed` 只取
    `reason`、`WebhookChangeSource._check_url` 只缓存布尔值。
    """
    seq = _Resolver(["93.184.216.34"])
    assert validate_url("https://evil.example/x", resolver=seq).allowed
    seq.answers = ["10.0.0.9"]                     # 同名第二次解析已切内网
    assert not validate_url("https://evil.example/x", resolver=seq).allowed
    assert seq.hosts == ["evil.example", "evil.example"]     # 外层没有任何记忆
    print("PASS test_dns_rebinding_toctou_has_no_pinning")


def test_empty_dns_answer_is_treated_as_public_current_behavior():
    """记录当前真实行为（**安全缺陷，未修**）：解析器返回空答案 → 直接放行。

    `bad = [ip for ip in ips if not _is_public_ip(ip)]` 对空列表恒为 `[]`，于是"一个地址
    都没解析到"被当成"所有答案都是公网"。`_resolve` 只在**抛异常**时返回 None（→ 域名解析
    失败，失败关闭）；返回空列表则一路放行到 `FetchDecision(allowed=True)`。
    真实世界出现 NODATA/空答案（或 DoH 桩、被劫持/投毒的 DNS）时，校验放行而实际连接目标
    未知——与模块"失败关闭"的宣称相悖，也是 DNS 重绑定最省事的一种起手式。
    修法方向：`if not ips: return FetchDecision(False, reason="解析不到任何地址")`。
    """
    d = validate_url("https://evil.example/x", resolver=_Resolver([]))
    assert d.allowed is True
    assert d.resolved_ips == ()
    # 唯一对外入口同样放行 → 影响会直接落到取数路径
    ok, _ = safe_get_allowed("https://evil.example/x", resolver=_Resolver([]))
    assert ok is True
    print("PASS test_empty_dns_answer_is_treated_as_public_current_behavior")


# ------------------------------------------------------- 畸形端口

def test_malformed_port_raises_instead_of_denying_current_behavior(monkeypatch):
    """记录当前真实行为（**缺陷，未修**）：畸形端口抛出 ValueError，而不是返回拒绝。

    `validate_url` 只把 `urlparse(url)` 包在 try 里，随后 `p.port not in _ALLOWED_PORTS`
    才触发端口解析——`:abc` / `:-1` / `:99999` 会让 `urllib.parse` 抛 ValueError，异常直接
    逃逸。调用方拿到的是异常而不是 `FetchDecision(allowed=False)`：
    `notifiers._target_allowed` 同样未捕获（本用例一并钉住），于是**攻击者可控的畸形
    payload_ref / 配置错的通知 URL 会在投递路径上炸出 500**，而不是被"拒绝"。
    正确方向：失败关闭（返回带原因的拒绝），异常绝不该逃出校验函数。
    """
    for bad in ["http://example.com:abc/", "http://example.com:-1/",
                "http://example.com:99999/"]:
        with pytest.raises(ValueError):
            validate_url(bad)

    from data_dog.alerting import notifiers
    # 去掉"显式放行内网"的后门影响，让用例不受本机 .env 影响（只测端口解析本身）；
    # 用 monkeypatch 而非直接赋值，避免把全局设置泄漏给其它测试。
    monkeypatch.setattr(notifiers.settings, "notify_allow_private_targets", lambda: False)
    with pytest.raises(ValueError):
        notifiers._target_allowed("http://example.com:abc/")
    print("PASS test_malformed_port_raises_instead_of_denying_current_behavior")


# ------------------------------------------------------- 特殊地址段

def test_cgnat_range_is_allowed_current_behavior():
    """记录当前真实行为（**安全缺口，未修**）：100.64.0.0/10（RFC 6598 CGNAT）被放行。

    `_is_public_ip` 用 is_private/is_loopback/is_link_local/is_reserved/is_multicast/
    is_unspecified 六个 property 做**黑名单**，而 CPython 的 `is_private` 并不覆盖
    100.64.0.0/10（`is_global` 才为 False）。CGNAT 段在运营商/云 VPC 里被当作内网使用，
    可路由到内部服务，因此应拦。改用 `addr.is_global`（白名单语义）可一次性覆盖此类遗漏。
    """
    cgnat = ipaddress.ip_address("100.64.0.1")
    assert cgnat.is_private is False            # 黑名单没覆盖它
    assert cgnat.is_global is False             # 但语义上并非可全球路由
    assert validate_url("http://100.64.0.1/").allowed is True    # 缺口：放行
    assert validate_url("http://100.127.255.254/").allowed is True

    # 对照：其余特殊段实测都被拦（黑名单覆盖到的部分是对的）
    rejected = ["198.18.0.1", "192.0.2.1", "240.0.0.1", "255.255.255.255",
                "224.0.0.1", "0.1.2.3", "169.254.0.1", "172.16.0.1",
                "192.0.0.1", "198.51.100.1", "203.0.113.1", "127.0.0.2"]
    for host in rejected:
        assert not validate_url(f"http://{host}/").allowed, f"{host} 应被拒"
    # IPv6 侧：ULA / 链路本地 / 组播 / 文档段 / NAT64 均被拒
    for host in ["fc00::1", "fe80::1", "ff02::1", "2001:db8::1", "64:ff9b::808:808"]:
        assert not validate_url(f"http://[{host}]/").allowed, f"{host} 应被拒"
    # IPv6 带 scope（链路本地）也不放过
    assert not validate_url("http://[fe80::1%25eth0]/").allowed
    print("PASS test_cgnat_range_is_allowed_current_behavior")


# ------------------------------------------------------- 控制字符 / 解析差分

def test_control_chars_in_authority_are_stripped_but_raw_url_returned():
    """记录当前真实行为（**缺陷，未修**）：authority 里的 \\t/\\n/\\r 被 urlsplit 静默剥掉。

    `validate_url("http://exa\\tmple.com/")` 校验（并交给解析器）的 host 是 `example.com`，
    但 `FetchDecision.url` 返回的是**原始字符串**（仍带 \\t），调用方就是拿这个原文去发请求。
    于是"被校验的名字"与"被请求的名字"不是同一份规范化结果——只要下游解析器对控制字符的
    处理与 urlsplit 不一致（不剥、或按 WHATWG 视 \\t 为分隔符），就会连到与校验不同的 host。
    `\\x00` 更是**不被剥离**，会原样进入解析器。正确做法：直接拒绝 authority 中的控制字符，
    并返回规范化后的 URL（而不是原文）。
    """
    rec = _Resolver(["93.184.216.34"])
    d = validate_url("http://exa\tmple.com/", resolver=rec)
    assert d.allowed and rec.hosts == ["example.com"]
    assert "\t" in d.url                     # 返回原文 ← 与"被校验的 host"不一致的差分点

    rec2 = _Resolver(["93.184.216.34"])
    d2 = validate_url("http://exa\x00mple.com/", resolver=rec2)
    assert rec2.hosts == ["exa\x00mple.com"]  # NUL 原样进解析器（未被拒）
    assert d2.allowed

    # 安全的那一项：urlsplit 会小写化 host（大小写混淆不构成绕过）
    rec3 = _Resolver(["93.184.216.34"])
    validate_url("HTTP://ExAmPlE.COM/", resolver=rec3)
    assert rec3.hosts == ["example.com"]
    print("PASS test_control_chars_in_authority_are_stripped_but_raw_url_returned")


# ------------------------------------------------------- 对外唯一入口

def test_url_parse_failure_is_fail_closed(monkeypatch):
    """`urlparse` 自身抛异常时同样失败关闭：返回拒绝，而不是把异常抛给调用方。"""
    from data_dog.data_plane import fetch as fetch_mod

    def boom(_url):
        raise ValueError("畸形 IPv6 字面量")

    monkeypatch.setattr(fetch_mod, "urlparse", boom)
    d = fetch_mod.validate_url("http://[::1]/")
    assert not d.allowed and d.reason == "URL 解析失败"
    print("PASS test_url_parse_failure_is_fail_closed")


def test_default_resolver_path_checks_every_getaddrinfo_answer(monkeypatch):
    """默认解析路径（未注入 resolver）走 `socket.getaddrinfo`，答案要**去重排序**后同样逐条查。

    这里把 getaddrinfo 换成假的，既能覆盖默认路径又不联网：返回一条公网 + 一条 IPv6 环回，
    校验必须整体拒绝（不能只信第一条）。
    """
    from data_dog.data_plane import fetch as fetch_mod
    calls: list[str] = []

    def fake_getaddrinfo(host, *a, **kw):
        calls.append(host)
        return [(2, 1, 6, "", ("93.184.216.34", 0)),
                (10, 1, 6, "", ("::1", 0, 0, 0)),
                (2, 1, 6, "", ("93.184.216.34", 0))]      # 重复项应被去重

    monkeypatch.setattr(fetch_mod.socket, "getaddrinfo", fake_getaddrinfo)
    d = fetch_mod.validate_url("https://evil.example/x")
    assert calls == ["evil.example"]
    assert d.resolved_ips == ("93.184.216.34", "::1"), "去重 + 排序后的完整答案集"
    assert not d.allowed and "::1" in d.reason

    # 全公网答案 → 放行
    monkeypatch.setattr(fetch_mod.socket, "getaddrinfo",
                        lambda host, *a, **kw: [(2, 1, 6, "", ("93.184.216.34", 0))])
    assert fetch_mod.validate_url("https://evil.example/x").allowed
    print("PASS test_default_resolver_path_checks_every_getaddrinfo_answer")


def test_safe_get_allowed_wraps_decision():
    """`safe_get_allowed` 是调用方必须过的唯一入口：放行返回原 URL，拒绝返回 denied 错误。"""
    ok, val = safe_get_allowed("https://93.184.216.34/x")
    assert ok and val == "https://93.184.216.34/x"

    ok2, e = safe_get_allowed("http://169.254.169.254/latest/meta-data")
    assert not ok2
    assert isinstance(e, dict) and e["error"] == "denied"
    assert "被安全策略拒绝" in e["detail"]

    # 非 http(s) 同样落到 denied（不是 invalid_input）→ 安全语义一致
    ok3, e3 = safe_get_allowed("file:///etc/passwd")
    assert not ok3 and e3["error"] == "denied"

    # 空答案缺陷会穿过这个唯一入口（与 test_empty_dns_answer_* 同源）
    ok4, _ = safe_get_allowed("https://evil.example/x", resolver=_Resolver([]))
    assert ok4 is True
    print("PASS test_safe_get_allowed_wraps_decision")
