"""演示租户必须与控台的租户默认值一致。

为什么需要这条守卫：`make ingest` 把演示数据投到某个租户，控台按 `DD_WEB_TENANT`
读另一个租户的数据——**默认值一旦不一致，"按文档跑完 ingest，控台永远是空"**，
而多租户隔离本身是正确行为，排查时最容易误判成"控制台坏了/接口返回空"。

本仓库真实发生过：`Makefile` 投 `tenant-real`、`compose.yaml` 控台读 `tenant-demo`，
于是照 README 跑完演示，控制台一个候选都看不到（数据确实落库了，只是落在别的租户）。

被判据：
1. Makefile 的 `TENANT ?=` 默认值；
2. compose.yaml 里控台服务的 `DD_WEB_TENANT: ${DD_TENANT:-<默认>}` 的默认值；
两者必须相等。任一侧改了默认值而另一侧没跟，这条测试就红。
"""

from __future__ import annotations

import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
MAKEFILE = REPO / "Makefile"
COMPOSE = REPO / "compose.yaml"

# Makefile: TENANT ?= tenant-demo
_MAKE_TENANT = re.compile(r"^TENANT\s*\?=\s*(\S+)\s*$", re.MULTILINE)
# compose: DD_WEB_TENANT: ${DD_TENANT:-tenant-demo}
_COMPOSE_TENANT = re.compile(
    r"DD_WEB_TENANT:\s*\$\{DD_TENANT:-([^}]+)\}"
)


def _make_tenant(makefile_text: str) -> str:
    match = _MAKE_TENANT.search(makefile_text)
    assert match is not None, (
        "Makefile 里找不到 `TENANT ?= ...`——演示租户必须是显式可读的默认值，"
        "否则没人能判断 ingest 的数据落到哪个租户"
    )
    return match.group(1)


def _compose_tenant(compose_text: str) -> str:
    match = _COMPOSE_TENANT.search(compose_text)
    assert match is not None, (
        "compose.yaml 里找不到 `DD_WEB_TENANT: ${DD_TENANT:-...}`——"
        "控台必须把租户作为显式参数暴露出来，而不是硬编码在代码里"
    )
    return match.group(1)


def test_make_ingest_tenant_matches_console_tenant() -> None:
    make_tenant = _make_tenant(MAKEFILE.read_text(encoding="utf-8"))
    compose_tenant = _compose_tenant(COMPOSE.read_text(encoding="utf-8"))
    assert make_tenant == compose_tenant, (
        f"演示租户不一致：`make ingest` 投到 {make_tenant!r}，"
        f"控台读 {compose_tenant!r}。照 README 跑完演示会在控制台看到空列表，"
        "而数据其实已落库（多租户隔离正常工作），极易被误判成控制台坏了。"
        "把 Makefile 的 TENANT ?= 与 compose.yaml 的 DD_WEB_TENANT 默认值改成同一个。"
    )


@pytest.mark.parametrize(
    "makefile_text",
    [
        "TENANT ?= tenant-real\n",  # 历史值：与控台 tenant-demo 不一致
        "TENANT ?= other-tenant\n",
    ],
)
def test_guard_actually_reads_the_makefile_value(makefile_text: str) -> None:
    """守卫自检：解析器读的是文件里的真值，而不是常量或默认参数。"""
    assert _make_tenant(makefile_text) in {"tenant-real", "other-tenant"}


def test_guard_rejects_missing_declaration() -> None:
    """缺声明必须报错（避免"删掉就通过"的假绿）。"""
    with pytest.raises(AssertionError):
        _make_tenant("PORT ?= 8000\n")
