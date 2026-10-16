"""`benchmarks/baseline.md` 的不变量：**基线不能悄悄过期**。

为什么需要：这份文档此前把"写入路径批量化落库"列为未完成的下一步，而 `service/write_behind.py`
（写队列，T1）早已实施——文档与代码各说各话，而且**没有任何信号**。规则只有一条：
能自动核对的事实，就别指望人记得同步。
"""
from __future__ import annotations

import datetime as dt
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
BASELINE = ROOT / "benchmarks" / "baseline.md"


def _text() -> str:
    return BASELINE.read_text(encoding="utf-8")


def _has_write_behind() -> bool:
    """代码里是否已有写队列（批量化落库）：用它把文档的"下一步"钉在事实上。"""
    src = (ROOT / "src" / "data_dog" / "settings.py").read_text(encoding="utf-8")
    return "DD_WRITE_BEHIND" in src


def test_records_environment_date_and_reproducible_commands():
    t = _text()
    assert "**环境**" in t and "**日期**" in t, "基线必须写清环境与日期（否则无法判断它有多旧）"
    m = re.search(r"\*\*日期\*\*：(\d{4}-\d{2}-\d{2})", t)
    assert m, "日期必须是可解析的 YYYY-MM-DD"
    assert dt.date.fromisoformat(m.group(1)) <= dt.date.today(), "基线日期不能是未来"
    scripts = set(re.findall(r"tools/([A-Za-z0-9_.-]+)", t))
    assert scripts, "基线必须写清复现命令（含 tools/... 脚本）"
    for name in sorted(scripts):
        assert (ROOT / "tools" / name).exists(), "基线引用了不存在的脚本：tools/" + name


def test_loadgen_flags_in_the_doc_really_exist():
    """文档里写下的 loadgen 参数必须真的存在——参数改名/删掉时立刻红。"""
    src = (ROOT / 'tools' / 'loadgen.py').read_text(encoding='utf-8')
    declared = set(re.findall(r'add_argument\("(--[a-z0-9-]+)"', src))
    assert declared, "解析不到 loadgen 的参数定义（脚本结构变了？）"
    for line in _text().splitlines():
        if "loadgen.py" not in line:
            continue
        for flag in re.findall(r"(--[a-z0-9-]+)", line):
            assert flag in declared, "基线里的 " + flag + " 在 tools/loadgen.py 里不存在"


def test_every_table_has_consistent_columns():
    """数字表格要能被人和脚本同样读懂：同一张表里每行列数一致。"""
    blocks = re.split(r"\n(?=#{2,3} )", _text())
    seen = 0
    for block in blocks:
        rows = [ln for ln in block.splitlines() if ln.startswith("|")]
        if len(rows) < 3:
            continue
        seen += 1
        widths = {ln.count("|") for ln in rows}
        assert len(widths) == 1, "表格列数不一致：" + str(sorted(widths))
    assert seen >= 1, "基线里至少要有一张实测数字表"


def test_next_steps_do_not_contradict_the_code():
    """**声明 == 事实**：未完成清单不能把已经做完的事再列一遍（这份文档就栽在这里）。"""
    t = _text()
    open_items = re.findall(r"^- \[ \]\s*\*\*(.+?)\*\*", t, re.M)
    assert open_items, "基线应保留下一步清单（全做完也要留「在目标环境复测」这类）"
    if _has_write_behind():
        assert not any("批量" in item for item in open_items), (
            "写路径批量化（DD_WRITE_BEHIND / service/write_behind.py）已实施，基线却仍把它列为"
            "未完成——要么更新基线，要么写清为什么还不算完成")
        assert re.search(r"write_behind|write-behind|写队列", t), (
            "代码里有写队列，基线必须写清它对数字的影响（开/关、本轮实测口径）")
    else:
        assert "已实施" not in t or "批量化" not in t
