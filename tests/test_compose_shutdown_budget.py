"""停机预算必须"宽限期 > 排空期限"，否则末批事件会被 SIGKILL。

为什么这条值得一个守卫：Docker 的默认停机宽限期是 10s，应用排空 write-behind
队列的默认期限也正好是 10s。两者相等时，容器在"排空进行到一半"和"被硬杀"之间
赛跑——现象是**重启后少了最后一批数据，日志里什么都没有**。这不是理论：任何一次
`docker compose up -d --build`、滚动发布、`down` 都会走到这条路径。

守卫两件事：
1. compose 里 app 服务的 `stop_grace_period` 存在且严格大于排空期限；
2. 排空期限仍是代码里的同一个数（改了代码却没改 compose 会立刻红）。
"""

from __future__ import annotations

import inspect
import pathlib
import re

import pytest

yaml = pytest.importorskip("yaml")

_ROOT = pathlib.Path(__file__).resolve().parents[1]
_COMPOSES = ["compose.yaml", "compose.prod.yaml"]


def _drain_timeout_s() -> float:
    """从代码里读排空期限，而不是在测试里再写一个魔法数。"""
    from data_dog.service.write_behind import stop_writer

    default = inspect.signature(stop_writer).parameters["drain_timeout_s"].default
    return float(default)


def _parse_grace(text: str) -> float:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)(ms|s|m)\s*", text)
    assert m, f"无法解析 stop_grace_period: {text!r}"
    value, unit = float(m.group(1)), m.group(2)
    return value * {"ms": 0.001, "s": 1.0, "m": 60.0}[unit]


@pytest.mark.parametrize("name", _COMPOSES)
def test_compose_stop_grace_period_exceeds_drain_timeout(name: str) -> None:
    doc = yaml.safe_load((_ROOT / name).read_text(encoding="utf-8"))
    app = doc["services"]["app"]
    assert "stop_grace_period" in app, (
        f"{name} 的 app 服务缺 stop_grace_period：Docker 会按默认 10s 硬杀，"
        "与 write-behind 排空期限相等 → 末批事件可能静默丢失"
    )
    grace = _parse_grace(str(app["stop_grace_period"]))
    drain = _drain_timeout_s()
    assert grace > drain, (
        f"{name}: stop_grace_period({grace}s) 必须严格大于排空期限({drain}s)，"
        "否则 SIGKILL 会和排空赛跑"
    )
    # 留出余量：仅大 0.1s 等于没留
    assert grace >= drain * 1.5, (
        f"{name}: stop_grace_period({grace}s) 相对排空期限({drain}s) 余量太小，"
        "建议 ≥1.5 倍（排空慢时仍会被杀）"
    )
