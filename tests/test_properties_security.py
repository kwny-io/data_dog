"""性质测试：安全边界要的是"对所有输入都成立"，不是"我举了 5 个例子"。

为什么补这一层
--------------
引用语法、rawlog 容器化、配置体检、分区 DDL 标识符这四处都是**安全边界**，此前只有例子
测试（"这几个路径被拒了"）。例子测试能证明"有防护"，证明不了"防护没有缺口"——攻击面恰好
是没人想到的输入。性质测试把断言换成全称命题：

1. **任意字符串**喂给引用解析器：永不抛异常，失败必须收敛成结构化错误码；
2. **任意存储键**喂给 rawlog 目录读取器：要么读到根目录内的文件、要么被拒——根目录外的
   哨兵内容**永不出现**在返回体里（这是"容器化"的强断言，不是"我试过 ../../"）；
3. **任意环境变量字典**喂给配置体检（含渲染）：永不抛异常，且结果结构始终成立；
4. **任意浮点时间**喂给日桶：单调、且 [day*86400, (day+1)*86400) 恰好包住输入；
5. **任意标识符**喂给分区 DDL：要么被白名单接受，要么抛 ValueError——**绝不**拼进 SQL。

确定性：所有用例 `derandomize=True`——每次跑同一批输入，避免"CI 今天红明天绿"，
与本仓"全离线确定性"的承诺一致（hypothesis 本身不联网）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("hypothesis", reason="未装 dev extras（hypothesis），跳过性质测试")

from hypothesis import given, settings  # noqa: E402
from hypothesis import strategies as st  # noqa: E402

from data_dog.config_check import check, render  # noqa: E402
from data_dog.evidence import rawlog  # noqa: E402
from data_dog.evidence.refs import default_resolver, is_valid_ref, parse_ref  # noqa: E402
from data_dog.storage import partitioning  # noqa: E402
from data_dog.storage.models import day_of  # noqa: E402

# 便宜的纯函数多跑一些；要构造解析器/读文件系统的少跑一些（性质不变，只是采样密度）。
FAST = settings(derandomize=True, max_examples=60, deadline=None)
MATH = settings(derandomize=True, max_examples=400, deadline=None)
_SENTINEL = "TOP-SECRET-SENTINEL-9f3a"
_RESOLVER = None


def _resolver():
    """解析器只建一次：性质测试关心的是输入空间，不是构造开销。"""
    global _RESOLVER
    if _RESOLVER is None:
        _RESOLVER = default_resolver()
    return _RESOLVER


# ------------------------------------------------------------ 1. 引用解析

@FAST
@given(st.text(max_size=120))
def test_resolver_never_raises_on_arbitrary_text(raw: str):
    """任意文本：**永不抛**；失败必须是结构化错误码（这是对外契约，不是内部细节）。"""
    out = _resolver().resolve(raw)
    assert isinstance(out, dict) and "ok" in out
    if out["ok"] is False:
        assert isinstance(out.get("error"), str) and out["error"], out


@FAST
@given(st.text(max_size=80))
def test_is_valid_ref_agrees_with_parse_ref(raw: str):
    """`is_valid_ref` 必须与 `parse_ref` 完全同口径——两个判断不一致就是绕过点。"""
    try:
        parse_ref(raw)
    except ValueError:
        assert is_valid_ref(raw) is False, raw
    except Exception as e:                                  # noqa: BLE001
        pytest.fail(f"parse_ref 抛了非 ValueError：{type(e).__name__}: {e}（raw={raw!r}）")
    else:
        assert is_valid_ref(raw) is True, raw


@FAST
@given(st.text(max_size=40), st.lists(st.text(max_size=20), max_size=3))
def test_parse_ref_never_accepts_unknown_schemes(scheme: str, rest: list[str]):
    """白名单之外的 scheme 一律不合法（大小写、混空白都不行）。"""
    raw = scheme + "://x" + ("/" + "/".join(rest) if rest else "")
    if is_valid_ref(raw):
        parsed = parse_ref(raw)
        assert parsed.scheme in {"metricwin", "rawlog", "vendordoc", "replay"}, raw


# ------------------------------------------------------------ 2. rawlog 容器化

@pytest.fixture(scope="module")
def rawlog_tree(tmp_path_factory) -> tuple[Path, Path]:
    """受控根目录 + **根外**哨兵文件（内容绝不该出现在任何返回体里）。

    用 module 作用域：hypothesis 不允许 function 作用域的夹具在多个输入间被复用
    （它无法确认夹具是否该每例重置）。这个夹具只建一次目录，本来也不需要在例间重置。
    """
    base = tmp_path_factory.mktemp("rawlog_tree")
    root = base / "root"
    root.mkdir()
    (root / "ok.log").write_text("inside", encoding="utf-8")
    outside = base / "outside_secret.log"
    outside.write_text(_SENTINEL, encoding="utf-8")
    return root, outside


@FAST
@given(st.text(max_size=60))
def test_rawlog_never_reads_outside_the_root(rawlog_tree: tuple[Path, Path], key: str):
    """任意键：或"查到/没查到"，或被显式拒绝——**根外内容永不出现**。"""
    root, _outside = rawlog_tree
    src = rawlog.make_rawlog_source(
        config=rawlog.RawLogConfig(base_dir=root, base_url="", max_bytes=4096,
                                   timeout_s=1.0, allow_private=False))
    try:
        found, payload = src(key, [])
    except (rawlog.RawLogRefused, rawlog.RawLogNotConfigured):
        return                      # 显式拒绝：可以
    except Exception as e:                                  # noqa: BLE001
        pytest.fail(f"读取器抛了非 RawLogRefused 的异常：{type(e).__name__}: {e}（key={key!r}）")
    if found:
        assert _SENTINEL not in str(payload), f"读到了根目录之外的内容！key={key!r}"


@FAST
@given(st.text(max_size=40))
def test_rawlog_path_refusal_is_always_a_rawlog_error(rawlog_tree: tuple[Path, Path], key: str):
    """拒绝也要**拒绝得干净**：错误类型固定（调用方与解析器都靠它分流）。"""
    root, _outside = rawlog_tree
    cfg = rawlog.RawLogConfig(base_dir=root, base_url="", max_bytes=64,
                              timeout_s=1.0, allow_private=False)
    src = rawlog.make_rawlog_source(config=cfg)
    try:
        found, payload = src(key, [])
    except rawlog.RawLogRefused as e:
        assert str(e)                      # 必须带原因，不能只抛个空错误
    except rawlog.RawLogNotConfigured:
        pytest.fail("配了 base_dir 却报未配置？")
    else:
        assert isinstance(found, bool)


# ------------------------------------------------------------ 3. 配置体检

@FAST
@given(st.dictionaries(st.text(min_size=1, max_size=14), st.text(max_size=24), max_size=6))
def test_config_check_never_raises_and_render_is_total(env: dict[str, str]):
    """任意环境变量字典：体检与渲染都**不许抛**（它要在部署前跑，崩了就没人用）。"""
    report = check(env)
    assert isinstance(report.get("ok"), bool)
    assert isinstance(report.get("errors"), list)
    assert isinstance(report.get("warnings"), list)
    assert isinstance(report.get("checked_keys"), int)
    text = render(report)
    assert isinstance(text, str)


# ------------------------------------------------------------ 4. 日桶算术

@MATH
@given(st.floats(min_value=0, max_value=4e9, allow_nan=False, allow_infinity=False))
def test_day_of_exactly_covers_the_instant(ts: float):
    """分区键必须**恰好**包住输入：下限含、上限不含（差一就落错分区）。"""
    d = day_of(ts)
    assert d * 86400 <= ts < (d + 1) * 86400


@MATH
@given(st.floats(min_value=0, max_value=4e9, allow_nan=False, allow_infinity=False),
       st.floats(min_value=0, max_value=4e9, allow_nan=False, allow_infinity=False))
def test_day_of_is_monotonic(a: float, b: float):
    lo, hi = (a, b) if a <= b else (b, a)
    assert day_of(lo) <= day_of(hi)


# ------------------------------------------------------------ 5. 分区 DDL 标识符

@FAST
@given(st.text(max_size=40))
def test_partition_ddl_identifiers_are_whitelisted_or_refused(name: str):
    """DDL 标识符只能来自白名单；否则必须 ValueError——**绝不**拼进 SQL。"""
    from datetime import datetime, timezone

    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    end = datetime(2026, 11, 1, tzinfo=timezone.utc)
    try:
        sql = partitioning.create_partition_sql(name, start, end)
    except ValueError:
        return
    assert name in sql
    # 白名单字符集：拼出来的 DDL 里不该出现引号/分号/空白注入
    assert not any(ch in sql for ch in ("'", ";", "\n", "\r")), sql
