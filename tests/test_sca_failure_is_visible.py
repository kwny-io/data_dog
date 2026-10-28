"""SCA 的「失败必须可见」：编码坏 / 空输出 / 非 JSON 都不许变成 pass。

背景（实测过的真实缺陷，提交前先有测试）：
`security_scan.py:_default_pip_audit_runner` 原先写 `json.loads(proc.stdout or "{}")`。
Windows 中文 locale 下 pip-audit 输出非 UTF-8 字节 → subprocess 读取线程抛 `UnicodeDecodeError`
→ `proc.stdout is None` → `or "{}"` 兜成 `{}` → 无 finding → **status="pass"**。
即扫描器崩了，报告写“通过”。

本文件盯三件事：
1. 空 stdout / 非 JSON / 退出码非零 → 必须 `skipped≠pass`，并带上可读原因；
2. 正常 JSON → 照旧解析（零回归）；
3. 非 UTF-8 字节**不再让 stdout 变成 None**（`encoding+errors` 那段的行为断言）。
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys

import pytest

from data_dog.operator_factory import security_scan as ss


def _run_with(fake_stdout, rc=0, stderr=""):
    """用 monkeypatch 替掉 subprocess.run，只测 runner 自己的判定逻辑。"""
    class _P:
        returncode = rc
        stdout = fake_stdout
        stderr = ""

    _P.stderr = stderr
    return _P


def test_empty_stdout_is_not_clean(monkeypatch):
    """一行没输出 → 不能当 {}。必须抛，让上层转成 skipped≠pass。"""
    monkeypatch.setattr(ss.subprocess, "run",
                        lambda *a, **k: _run_with("", rc=1, stderr="boom"))
    with pytest.raises(RuntimeError) as ei:
        ss._default_pip_audit_runner("")
    msg = str(ei.value)
    assert "未输出任何内容" in msg
    assert "rc=1" in msg
    assert "boom" in msg, "stderr 必须带出来，否则排障要重跑一遍"


def test_none_stdout_is_not_clean(monkeypatch):
    """`stdout=None`（解码失败时的真实表现）必须抛，绝不能 `or "{}"` 兜成 pass。"""
    monkeypatch.setattr(ss.subprocess, "run", lambda *a, **k: _run_with(None, rc=0))
    with pytest.raises(RuntimeError):
        ss._default_pip_audit_runner("")


def test_invalid_json_is_not_clean(monkeypatch):
    monkeypatch.setattr(ss.subprocess, "run",
                        lambda *a, **k: _run_with("<html>gateway</html>", rc=0))
    with pytest.raises(RuntimeError) as ei:
        ss._default_pip_audit_runner("")
    assert "不是合法 JSON" in str(ei.value)


def test_valid_json_still_parses(monkeypatch):
    payload = {"dependencies": [{"name": "x", "version": "1", "vulns": []}]}
    monkeypatch.setattr(ss.subprocess, "run",
                        lambda *a, **k: _run_with(json.dumps(payload), rc=1))
    assert ss._default_pip_audit_runner("") == payload, \
        "pip-audit 发现漏洞时退出码非 0 但 stdout 有 JSON，必须照常解析"


def test_runner_failure_becomes_skipped_not_pass(monkeypatch):
    """端到端：runner 抛 → scan_dependencies 必须 skipped（且 note 写清原因）。"""
    def _boom(_lf):
        raise RuntimeError("pip-audit 输出无法解码")

    r = ss.scan_dependencies(lockfile="", runner=_boom)
    assert r.status == "skipped"
    assert "skipped≠pass" in r.note


@pytest.mark.skipif(sys.platform != "win32", reason="非 UTF-8 字节解码行为在 Windows 上最典型")
def test_non_utf8_bytes_do_not_become_none(monkeypatch):
    """`encoding="utf-8", errors="replace"` 的行为锚：坏字节变成替换符而不是让 stdout 变 None。

    必须走**产品代码自己**的 kwargs，而不是测试里手写一份——
    变异验证时发现：手写版本让「把 encoding 删掉」这个变异没被杀（6 passed），
    即测试看着在守，实际守的是测试自己。改成捕获产品代码传给 subprocess 的 kwargs。
    """
    script = pathlib.Path("artifacts/_enc_probe.py")
    script.parent.mkdir(exist_ok=True)
    script.write_text(
        "import sys\nsys.stdout.buffer.write(bytes([180, 242, 211, 161]))\n", encoding="utf-8")
    seen: dict = {}
    real_run = subprocess.run

    def _spy(cmd, **kw):
        seen.update(kw)
        return real_run([sys.executable, str(script)], **kw)

    try:
        monkeypatch.setattr(subprocess, "run", _spy)
        with pytest.raises(RuntimeError):      # 坏字节 → 不是合法 JSON → 必须抛
            ss._default_pip_audit_runner("")
    finally:
        script.unlink(missing_ok=True)

    # 断言放在 inblock 之后：旧版本断言写在 runner 之前，seen 还是空字典，
    # 测试永远红（自己写的测试自己错，变异矩阵里一眼就看出它不是真红）。
    assert seen, "spy 没被调用：产品代码没走 ss.subprocess.run"
    assert "errors" in seen and "encoding" in seen, \
        f"产品代码必须显式给 encoding/errors，实际 kwargs={seen}"
    assert seen.get("errors") == "replace"
    assert seen.get("encoding") == "utf-8", f"实际 {seen.get('encoding')!r}"
