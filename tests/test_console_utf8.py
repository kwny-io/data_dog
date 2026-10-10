"""控制台编码兜底（`core/console.py`）的回归测试。

为什么要有这份（这条缺陷单测抓不到）
-----------------------------------
原始缺陷是：Windows 简体中文控制台（代码页 936）下 `print("▸ …")` 抛
`UnicodeEncodeError`，**命令直接崩、整条演示链路走不完**。CI 只跑 Linux，
默认套件也只在 pytest 里（stdout 被替换成捕获对象，不经过真控制台编码），
所以"全绿"和"能跑"在这里是两回事。

本测试用 `PYTHONIOENCODING=gbk` **在子进程里重建那个编码环境**——它等价于
Windows cp936 控制台的效果（GBK 装不下 `▸`/`✅`），且跨平台可复现：
修复前 returncode=1 + stderr 里有 UnicodeEncodeError，修复后 returncode=0。
"""
from __future__ import annotations

import ast
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

from data_dog.core.console import ensure_utf8_console

REPO = Path(__file__).resolve().parent.parent

# 入口清单：跑得快的用子进程真验；慢的（含真仿真/大评估）只在下面的 AST 检查里守。
FAST_ENTRIES = [
    "data_dog.e2e.replay_demo",
    "data_dog.data_plane.p2_demo",
    "data_dog.alerting.p3_demo",
    "data_dog.operator_factory.p1_demo",
    "data_dog.agent.degradation_demo",
]
# 所有**用户可见的入口**（含慢的与 CLI）：必须都调了兜底，否则就是"漏了一个入口"
ALL_ENTRY_FILES = [
    "src/data_dog/cli.py",
    "src/data_dog/e2e/replay_demo.py",
    "src/data_dog/data_plane/p2_demo.py",
    "src/data_dog/alerting/p3_demo.py",
    "src/data_dog/operator_factory/p1_demo.py",
    "src/data_dog/agent/degradation_demo.py",
    "src/data_dog/synth/synth_eval.py",
    "src/data_dog/synth/dataset_eval.py",
    "src/data_dog/mujoco_sim/mujoco_eval.py",
]


class _FakeStream:
    """带 reconfigure 的最简替身（只记下被设成什么）。"""

    def __init__(self):
        self.encoding = "cp936"
        self.errors = "strict"

    def reconfigure(self, *, encoding=None, errors=None):
        self.encoding = encoding or self.encoding
        self.errors = errors or self.errors


class _FailingReconfigure(_FakeStream):
    """reconfigure 抛任意异常的流（已关闭 / 已 detach / 行为不可预测的替身）。

    刻意覆盖 `RuntimeError`：既有的
    `tests/test_synth.py::test_synth_eval_main_tolerates_odd_stdout` 就是用
    RuntimeError 锁这条契约的（"抛任何异常都不能崩"），这里保持一致。
    """

    def reconfigure(self, *, encoding=None, errors=None):
        raise RuntimeError("不支持重配置")


class _NoReconfigure:
    """没有 reconfigure 的流（StringIO / 某些 IDE 包装）：必须静默跳过。"""

    encoding = "ascii"


# ---------------------------------------------------------------- helper 单元

def test_helper_reconfigures_both_streams(monkeypatch):
    out, err = _FakeStream(), _FakeStream()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    assert ensure_utf8_console() is True
    assert (out.encoding, out.errors) == ("utf-8", "replace")
    assert (err.encoding, err.errors) == ("utf-8", "replace")


def test_helper_is_idempotent(monkeypatch):
    out = _FakeStream()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", _FakeStream())
    assert ensure_utf8_console() is True
    assert ensure_utf8_console() is True
    assert out.encoding == "utf-8"


def test_helper_skips_stream_without_reconfigure(monkeypatch):
    """没有 reconfigure 的流不能让它抛错——否则兜底本身成了新的崩溃点。"""
    monkeypatch.setattr(sys, "stdout", _NoReconfigure())
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    assert ensure_utf8_console() is False


def test_helper_survives_reconfigure_failure(monkeypatch):
    """reconfigure 抛错（流已关闭等）也必须吞掉，绝不能往外抛。"""
    monkeypatch.setattr(sys, "stdout", _FailingReconfigure())
    monkeypatch.setattr(sys, "stderr", _FailingReconfigure())
    assert ensure_utf8_console() is False


# ---------------------------------------------------------------- 真进程回归

@pytest.mark.parametrize("module", FAST_ENTRIES)
def test_entrypoint_survives_gbk_console(module):
    """GBK 控制台下入口必须 rc=0（修复前：UnicodeEncodeError + rc=1）。"""
    env = dict(os.environ, PYTHONIOENCODING="gbk", PYTHONUTF8="0")
    env.pop("PYTHONUTF8", None)      # 别让 UTF-8 模式顺手把问题掩盖掉
    p = subprocess.run([sys.executable, "-m", module], cwd=REPO, env=env,
                       capture_output=True)
    err = p.stderr.decode("utf-8", "replace")
    assert "UnicodeEncodeError" not in err, err[-600:]
    assert p.returncode == 0, err[-600:]


# ---------------------------------------------------------------- 入口齐全性

@pytest.mark.parametrize("rel", ALL_ENTRY_FILES)
def test_user_facing_entrypoints_call_console_helper(rel):
    """每个用户可见入口都要接上兜底——漏掉的那个恰好就是"在 Windows 上仍然崩"的那个。"""
    src = (REPO / rel).read_text(encoding="utf-8")
    calls = [
        n for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.Call)
        and (getattr(n.func, "id", None) or getattr(n.func, "attr", None))
        == "ensure_utf8_console"
    ]
    assert calls, f"{rel} 没有调用 ensure_utf8_console（Windows GBK 控制台会崩）"
