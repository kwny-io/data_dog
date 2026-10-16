"""rawlog:// 真实读取（evidence/rawlog.py）：两种模式 + 失败关闭 + 边界。

替换掉的旧行为值得留个记录：默认源曾是 mock（固定 authority 白名单 + digest "sha256:mock"），
ref 解析成功、权限校验通过，**内容却是假的**。这里把"真读"和"没配就别装"都钉住。
"""
from __future__ import annotations

import os
import subprocess

import pytest

from data_dog.evidence import rawlog
from data_dog.evidence.refs import EvidenceResolver


def _cfg(tmp_path, **kw) -> rawlog.RawLogConfig:
    return rawlog.RawLogConfig(
        base_dir=kw.pop("base_dir", tmp_path),
        base_url=kw.pop("base_url", ""),
        max_bytes=kw.pop("max_bytes", 1024),
        timeout_s=1.0,
        allow_private=False,
    )


def _resolve(source, ref):
    return EvidenceResolver({"rawlog": source}).resolve(ref)


# ---------------------------------------------------------------- 目录模式

def test_reads_local_file_with_digest_and_size(tmp_path):
    (tmp_path / "evt-1.log").write_bytes(b"motor overheat\n")   # 字节级写入：不受 Windows 换行翻译影响
    out = _resolve(rawlog.make_rawlog_source(config=_cfg(tmp_path)), "rawlog://evt-1.log")
    assert out["ok"] is True
    p = out["payload"]
    assert p["content"] == "motor overheat\n"
    assert p["bytes"] == 15
    assert p["truncated"] is False
    assert p["source"] == "file"
    assert len(p["content_sha256"]) == 64          # 内容摘要可独立核验


def test_lookup_maps_event_id_to_stored_ref(tmp_path):
    (tmp_path / "a.log").write_text("from ref", encoding="utf-8")
    src = rawlog.make_rawlog_source(lambda eid: {"evt-9": "a.log"}.get(eid),
                                   config=_cfg(tmp_path))
    out = _resolve(src, "rawlog://evt-9")
    assert out["ok"] is True
    assert out["payload"]["content"] == "from ref"
    assert out["payload"]["key"] == "a.log"


def test_lookup_miss_is_not_found(tmp_path):
    src = rawlog.make_rawlog_source(lambda eid: None, config=_cfg(tmp_path))
    out = _resolve(src, "rawlog://evt-9")
    assert out["ok"] is False and out["error"] == "not_found"


@pytest.mark.parametrize("key", ["../../etc/passwd", "/etc/passwd", "a/../b"])
def test_stored_key_escape_is_refused(tmp_path, key):
    """**直调数据源**（绕过 ref 语法层）验证容器化：绝对路径与穿越都必须拒绝。"""
    src = rawlog.make_rawlog_source(config=_cfg(tmp_path))
    with pytest.raises(rawlog.RawLogRefused):
        src(key, [])


def test_ref_grammar_blocks_traversal_before_the_reader(tmp_path):
    """第一道闸在语法层：含 .. 的引用根本走不到读取器。"""
    src = rawlog.make_rawlog_source(config=_cfg(tmp_path))
    out = _resolve(src, "rawlog://../etc/passwd")
    assert out["ok"] is False and out["error"] == "invalid_input"


def test_symlink_escape_is_refused(tmp_path):
    outside = tmp_path / "outside_secret.log"
    outside.write_text("secret", encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()
    try:
        os.symlink(outside, root / "link.log")
    except (OSError, NotImplementedError):        # Windows 无权限建符号链接
        pytest.skip("本机不支持创建符号链接")
    src = rawlog.make_rawlog_source(config=_cfg(root))
    with pytest.raises(rawlog.RawLogRefused):
        src("link.log", [])


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"),
                    reason="POSIX 专用：Windows 无 O_NOFOLLOW，走 resolve 回落（见模块 docstring）")
def test_posix_refuses_any_symlink_in_the_path(tmp_path):
    """POSIX 逐段 `openat`+`O_NOFOLLOW`：**任何**符号链接都拒（比"逃逸检查"更严）。"""
    (tmp_path / "real.log").write_bytes(b"body")
    os.symlink(tmp_path / "real.log", tmp_path / "link.log")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "deep.log").write_bytes(b"deep")
    os.symlink(tmp_path / "sub", tmp_path / "sub_link")
    src = rawlog.make_rawlog_source(config=_cfg(tmp_path))
    assert src("real.log", [])[1]["content"] == "body"          # 普通路径照常
    assert src("sub/deep.log", [])[1]["content"] == "deep"      # 多级普通路径照常
    for key in ("link.log", "sub_link/deep.log"):
        with pytest.raises(rawlog.RawLogRefused):
            src(key, [])


def test_oversize_local_is_truncated_and_labelled(tmp_path):
    (tmp_path / "big.log").write_text("x" * 5000, encoding="utf-8")
    out = _resolve(rawlog.make_rawlog_source(config=_cfg(tmp_path, max_bytes=100)),
                   "rawlog://big.log")
    p = out["payload"]
    assert p["truncated"] is True
    assert len(p["content"]) == 100 and p["bytes"] == 5000
    assert "已截断" in p["note"]                    # 截断必须看得见


def test_subpath_is_refused(tmp_path):
    src = rawlog.make_rawlog_source(config=_cfg(tmp_path))
    with pytest.raises(rawlog.RawLogRefused):
        src("evt-1", ["sub"])


# ---------------------------------------------------------------- 失败关闭

def test_unconfigured_fails_closed(monkeypatch):
    monkeypatch.delenv("DD_RAWLOG_BASE_DIR", raising=False)
    monkeypatch.delenv("DD_RAWLOG_BASE_URL", raising=False)
    out = _resolve(rawlog.make_rawlog_source(), "rawlog://e1")
    assert out["ok"] is False and out["error"] == "execution_failed"
    assert "未配置" in str(out.get("detail", ""))


def test_both_locations_configured_is_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("DD_RAWLOG_BASE_DIR", str(tmp_path))
    monkeypatch.setenv("DD_RAWLOG_BASE_URL", "https://cdn.example.com/logs")
    out = _resolve(rawlog.make_rawlog_source(), "rawlog://e1")
    assert out["ok"] is False and out["error"] == "execution_failed"
    assert "互斥" in str(out.get("detail", ""))


# ---------------------------------------------------------------- 对象存储模式

class _StubResp:
    def __init__(self, ok, status=200, body=b"", error="", final_url="", hops=()):
        self.ok = ok
        self.status = status
        self.body = body
        self.error = error
        self.final_url = final_url
        self.hops = hops
        self.resolved_ip = "203.0.113.9"


def _http_cfg() -> rawlog.RawLogConfig:
    return rawlog.RawLogConfig(base_dir=None, base_url="https://cdn.example.com/logs",
                               max_bytes=100, timeout_s=1.0, allow_private=False)


def test_http_mode_goes_through_safe_request(monkeypatch):
    seen: dict = {}

    def _fake(url, **kw):
        seen.update(kw, url=url)
        return _StubResp(True, body=b"from object storage",
                         final_url="https://cdn.example.com/logs/evt-1",
                         hops=("https://cdn.example.com/logs/evt-1",))

    monkeypatch.setattr("data_dog.data_plane.fetch.safe_request", _fake)
    out = _resolve(rawlog.make_rawlog_source(config=_http_cfg()), "rawlog://evt-1")
    assert out["ok"] is True
    assert out["payload"]["content"] == "from object storage"
    assert out["payload"]["source"] == "http"
    assert seen["url"] == "https://cdn.example.com/logs/evt-1"
    assert seen["allow_private"] is False            # 默认不许私网
    assert seen["max_bytes"] >= 100


def test_http_error_is_refused(monkeypatch):
    monkeypatch.setattr("data_dog.data_plane.fetch.safe_request",
                        lambda url, **kw: _StubResp(False, 403, error="url_denied: 非公网地址"))
    out = _resolve(rawlog.make_rawlog_source(config=_http_cfg()), "rawlog://evt-1")
    assert out["ok"] is False and out["error"] == "execution_failed"


def test_http_404_is_not_found(monkeypatch):
    monkeypatch.setattr("data_dog.data_plane.fetch.safe_request",
                        lambda url, **kw: _StubResp(False, 404, error="not_found"))
    out = _resolve(rawlog.make_rawlog_source(config=_http_cfg()), "rawlog://evt-1")
    assert out["ok"] is False and out["error"] == "not_found"


def test_http_exactly_at_cap_is_complete(monkeypatch):
    """恰好等于上限 = 完整（读取按「上限+1」取，拿不到第 cap+1 字节就说明到此为止）。"""
    monkeypatch.setattr("data_dog.data_plane.fetch.safe_request",
                        lambda url, **kw: _StubResp(True, body=b"y" * 100))
    out = _resolve(rawlog.make_rawlog_source(config=_http_cfg()), "rawlog://evt-1")
    assert out["payload"]["truncated"] is False
    assert out["payload"]["bytes"] == 100


def test_http_over_cap_is_truncated_with_unknown_full_length(monkeypatch):
    """拿到「上限+1」= 确定被截；对象存储没给完整长度，就如实说"完整长度未知"。"""
    monkeypatch.setattr("data_dog.data_plane.fetch.safe_request",
                        lambda url, **kw: _StubResp(True, body=b"y" * 101))
    out = _resolve(rawlog.make_rawlog_source(config=_http_cfg()), "rawlog://evt-1")
    assert out["payload"]["truncated"] is True
    assert len(out["payload"]["content"]) == 100
    assert "完整长度未知" in out["payload"]["note"]


def test_http_reader_asks_for_one_byte_more_than_the_cap(monkeypatch):
    """能不能判定截断，取决于读取时多要 1 字节；这条把该约定钉住。"""
    seen: dict = {}

    def _fake(url, **kw):
        seen.update(kw)
        return _StubResp(True, body=b"ok")

    monkeypatch.setattr("data_dog.data_plane.fetch.safe_request", _fake)
    _resolve(rawlog.make_rawlog_source(config=_http_cfg()), "rawlog://evt-1")
    assert seen["max_bytes"] == 101
# ------------------------------------------------- Windows：句柄级受控打开
_WIN = os.name == "nt"


def _junction(link, target) -> bool:
    """用 mklink /J 造目录 junction：**不需要管理员或开发者模式**（符号链接才需要）。"""
    proc = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                          capture_output=True, text=True)
    return proc.returncode == 0 and link.exists()


@pytest.mark.skipif(not _WIN, reason="Windows 句柄打开专用")
def test_windows_refuses_junction_directory_component(tmp_path):
    """中间目录是 junction（reparse point）→ 拒；失败关闭，不读到根外内容。"""
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (outside / "secret.log").write_bytes(b"TOP-SECRET")
    if not _junction(root / "link", outside):
        pytest.skip("本机不允许建 junction")
    src = rawlog.make_rawlog_source(config=_cfg(root))
    with pytest.raises(rawlog.RawLogRefused):
        src("link/secret.log", [])


@pytest.mark.skipif(not _WIN, reason="Windows 句柄打开专用")
def test_windows_refuses_junction_inside_root_too(tmp_path):
    """根**内部**的 junction 也拒——核对的是"句柄真实路径 == 受控路径"。

    这条是新旧行为的分水岭：旧实现 resolve() 后做包含性检查，别名解析回根内 → **放行**；
    新实现看句柄事实，路径不一致 → **拒绝**（保守，但确定）。
    """
    root = tmp_path / "root"
    root.mkdir()
    (root / "real").mkdir()
    (root / "real" / "a.log").write_bytes(b"body")
    if not _junction(root / "alias", root / "real"):
        pytest.skip("本机不允许建 junction")
    src = rawlog.make_rawlog_source(config=_cfg(root))
    assert src("real/a.log", [])[1]["content"] == "body"      # 真路径照常
    with pytest.raises(rawlog.RawLogRefused):                  # 经 junction 一律拒
        src("alias/a.log", [])


@pytest.mark.skipif(not _WIN, reason="Windows 句柄打开专用")
def test_windows_refuses_symlinked_file(tmp_path):
    """最后一段是符号链接：FILE_FLAG_OPEN_REPARSE_POINT 让命名对象就是**链接本身** → 拒。"""
    root = tmp_path / "root"
    root.mkdir()
    (root / "real.log").write_bytes(b"body")
    try:
        os.symlink(root / "real.log", root / "link.log")
    except (OSError, NotImplementedError):
        pytest.skip("本机无权限建符号链接（Windows 需开发者模式/管理员）")
    src = rawlog.make_rawlog_source(config=_cfg(root))
    assert src("real.log", [])[1]["content"] == "body"
    with pytest.raises(rawlog.RawLogRefused):
        src("link.log", [])


@pytest.mark.skipif(not _WIN, reason="Windows 句柄打开专用")
def test_windows_checks_the_opened_handle_path(tmp_path, monkeypatch):
    """证明核对的是**句柄事实**：把句柄真实路径改到别处，正常文件也必须拒。"""
    (tmp_path / "a.log").write_bytes(b"body")
    monkeypatch.setattr(rawlog, "_final_path",
                        lambda k32, handle: "c:\\windows\\system32\\kernel32.dll")
    src = rawlog.make_rawlog_source(config=_cfg(tmp_path))
    with pytest.raises(rawlog.RawLogRefused) as ei:
        src("a.log", [])
    assert "句柄实际指向" in str(ei.value)
