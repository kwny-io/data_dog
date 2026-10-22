"""日志 `extra=` 不能用 LogRecord 保留键（静态守卫）。

为什么值一条测试：`logger.info("x", extra={"thread": t.name})` **不是**"多打一个字段"，
而是直接把调用抛掉——

    KeyError: "Attempt to overwrite 'thread' in LogRecord"

`Logger.makeRecord` 在把 extra 塞进 record 时，遇到 record 已有的属性就抛 KeyError。
本仓真发生过：`src/data_dog/service/app.py` 的"流式接入已启动"日志用了
`extra={"thread": ...}`，于是**每次流式接入启动都在最后一步抛异常**，
被外层 broad except 吞成一行 `流式接入启动失败（HTTP 入口不受影响）`——
功能静默不工作，HTTP 却一切正常，`/readyz` 是唯一的线索。

这类缺陷的可怕之处：不崩、不红、不告警，只是功能没开。所以用静态守卫把整类挡住。
"""

from __future__ import annotations

import ast
from pathlib import Path

from data_dog.core.logging import _RESERVED

_SRC = Path(__file__).resolve().parents[1] / "src"

# 接受 `extra` 关键字的日志方法名（logging 的 LogRecord 保留键规则对所有 level 生效）
_LOG_METHODS = {
    "debug", "info", "warning", "warn", "error", "exception", "critical", "log",
}


def _reserved_extra_keys(path: Path) -> list[tuple[int, str]]:
    """返回该文件里 `extra={...}` 中命中保留键的 (行号, 键名)。"""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else (
            func.id if isinstance(func, ast.Name) else "")
        if name not in _LOG_METHODS:
            continue
        for kw in node.keywords:
            if kw.arg != "extra" or not isinstance(kw.value, ast.Dict):
                continue
            for key in kw.value.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    if key.value in _RESERVED:
                        hits.append((key.lineno, key.value))
                elif key is None:
                    continue        # `**somedict` 展开：静态无法判定，交给运行时
    return hits


def test_no_logger_extra_uses_logrecord_reserved_keys() -> None:
    offenders: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        for lineno, key in _reserved_extra_keys(path):
            rel = path.relative_to(_SRC.parent).as_posix()
            offenders.append(f"{rel}:{lineno}: extra={{{key!r}: ...}}")
    assert not offenders, (
        "日志 extra 里用了 LogRecord 保留键——这些调用会抛 KeyError，功能静默失效。"
        "改个名字（例如 thread → thread_name）：\n  " + "\n  ".join(offenders))


def test_guard_would_catch_the_real_regression() -> None:
    """守卫自带正对照：把真出过的写法喂给它，必须抓到。

    没有这条，"守卫一直是绿的"可能只是因为它什么都没检查。
    """
    import tempfile

    bad = (
        "import logging\n"
        "_log = logging.getLogger('x')\n"
        "def f(thread):\n"
        "    _log.info('流式接入已启动', extra={'source': 'file', 'thread': thread.name})\n"
    )
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "bad.py"
        p.write_text(bad, encoding="utf-8")
        hits = _reserved_extra_keys(p)
    assert hits and hits[0][1] == "thread", hits
