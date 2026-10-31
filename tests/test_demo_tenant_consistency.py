"""演示租户必须与控台的租户默认值一致。

为什么需要这条守卫：`make ingest` 把演示数据投到某个租户，控台按 `DD_WEB_TENANT`
读另一个租户的数据——**默认值一旦不一致，"按文档跑完 ingest，控台永远是空"**，
而多租户隔离本身是正确行为，排查时最容易误判成"控制台坏了/接口返回空"。

本仓库真实发生过：`Makefile` 投 `tenant-real`、`compose.yaml` 控台读 `tenant-demo`，
于是照 README 跑完演示，控制台一个候选都看不到（数据确实落库了，只是落在别的租户）。

被判据（四侧，必须全等）：
1. Makefile 的 `TENANT ?=` 默认值；
2. compose.yaml 里控台服务的 `DD_WEB_TENANT: ${DD_TENANT:-<默认>}` 的默认值；
3. `go2_sim/server.py` 的 `DEMO_TENANT` —— 仿真器往哪个租户投遥测；
4. `tools/dev.sh` 的 `DD_WEB_TENANT:-<默认>` —— 本地起控台时读哪个租户。

第 3、4 条是后补的：**同一次事故在有第三条数据来源时又发生了一遍**——
仿真器投 `tenant-go2-demo`、控台读 `tenant-demo`，数据落了库却永远查不到。
原来这条测试只看 Makefile ↔ compose，谁也管不住仿真器，所以漂了也没人发现。
任一侧改了默认值而其余没跟，这条测试就红。
"""

from __future__ import annotations

import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[1]
MAKEFILE = REPO / "Makefile"
COMPOSE = REPO / "compose.yaml"
SIM_SERVER = REPO / "src" / "data_dog" / "go2_sim" / "server.py"
DEV_SH = REPO / "tools" / "dev.sh"

# Makefile: TENANT ?= tenant-demo
_MAKE_TENANT = re.compile(r"^TENANT\s*\?=\s*(\S+)\s*$", re.MULTILINE)
# compose: DD_WEB_TENANT: ${DD_TENANT:-tenant-demo}
_COMPOSE_TENANT = re.compile(
    r"DD_WEB_TENANT:\s*\$\{DD_TENANT:-([^}]+)\}"
)
# server.py: DEMO_TENANT = "tenant-demo"（允许行尾注释）
_SIM_TENANT = re.compile(r'^DEMO_TENANT\s*=\s*"([^"]+)"', re.MULTILINE)
# dev.sh: --tenant "${DD_WEB_TENANT:-tenant-demo}"
_DEV_TENANT = re.compile(r'\$\{DD_WEB_TENANT:-([^}]+)\}')


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


def _sim_tenant(server_text: str) -> str:
    match = _SIM_TENANT.search(server_text)
    assert match is not None, (
        "go2_sim/server.py 里找不到 `DEMO_TENANT = \"...\"`——"
        "仿真器往哪个租户投遥测必须是显式可读的常量"
    )
    return match.group(1)


def _dev_tenant(dev_text: str) -> str:
    match = _DEV_TENANT.search(dev_text)
    assert match is not None, (
        "tools/dev.sh 里找不到 `DD_WEB_TENANT:-...`——"
        "本地起控台时读哪个租户必须是显式可读的默认值"
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


def test_simulator_tenant_matches_console_tenant() -> None:
    """仿真器投的租户必须与控台读的父租户一致。

    仿真器接 `--api-url` 后是**第三条数据来源**，它一旦投错租户，
    形态与 `make ingest` 投错租户完全一样：后端有数据、控制台永远空。
    """
    sim_tenant = _sim_tenant(SIM_SERVER.read_text(encoding="utf-8"))
    console_tenant = _compose_tenant(COMPOSE.read_text(encoding="utf-8"))
    assert sim_tenant == console_tenant, (
        f"演示租户不一致：仿真器投到 {sim_tenant!r}，控台读 {console_tenant!r}。"
        "仿真数据会正常落库，但控制台一条都查不到（多租户隔离本身是对的），"
        "排查时极易误判成接口坏了。"
        "把 go2_sim/server.py 的 DEMO_TENANT 改成与控台默认值一致。"
    )


def test_dev_sh_tenant_matches_console_tenant() -> None:
    """`tools/dev.sh` 本地起的控台租户必须与仿真器投的一致。

    `dev.sh` 把同一个 `DD_WEB_TENANT` 既传给前端的 `--tenant`、又用作控台读取
    的默认值；写成两个字面量就会各自漂移。
    """
    dev_tenant = _dev_tenant(DEV_SH.read_text(encoding="utf-8"))
    sim_tenant = _sim_tenant(SIM_SERVER.read_text(encoding="utf-8"))
    assert dev_tenant == sim_tenant, (
        f"演示租户不一致：tools/dev.sh 起的控台读 {dev_tenant!r}，"
        f"仿真器投 {sim_tenant!r}。本地 `dev.sh` 起来后控制台会是空列表。"
        "把 dev.sh 的 DD_WEB_TENANT 默认值与 server.py 的 DEMO_TENANT 对齐。"
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


def test_guard_reads_simulator_value() -> None:
    """仿真器侧的解析器读的是文件里的真值，不是常量。"""
    assert _sim_tenant('DEMO_TENANT = "tenant-go2-demo"\n') == "tenant-go2-demo"
    with pytest.raises(AssertionError):
        _sim_tenant("DEMO_DEVICE = 'device-go2-sim-01'\n")


def test_guard_reads_dev_sh_value() -> None:
    """dev.sh 侧的解析器读的是 `${DD_WEB_TENANT:-...}` 的默认值。"""
    assert _dev_tenant('--tenant "${DD_WEB_TENANT:-tenant-x}"') == "tenant-x"
    with pytest.raises(AssertionError):
        _dev_tenant("--tenant tenant-demo")
