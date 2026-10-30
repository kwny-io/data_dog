"""`go2_sim/semantic.py` 的测试：**钉住"界面上不会再漏出标识符"这件事**。

为什么要有这个文件
------------------
用户报的问题是"故障那边显示的不是中文名，是文件名"。修的时候踩到两个坑，
每一个都能让修复看起来完事、实际上只闭了一半的口：

1. **只统计了一个来源**。一开始照着仿真器的 23 条规则抄表，但真算子
   `operators/generated/*.yaml` 里还有 `BATT_SOC_LOW` 等 3 条——
   一换到真实数据，界面上就重新冒出下划线串。
2. **接错了字段**。候选卡片的 `signal` 不是 `event_type`，是
   `_signal_of()` 派生的信号族，归不进语义桶时**直接返回裸字段名**
   （`tau_est`/`q`/`lost`）。拿事件类型表去翻它，翻出来的名字全是错的。

这两个坑的共同点是：**表看起来是全的，因为它只覆盖了"我想到的那部分"**。
所以下面不是"抽查几条翻译对不对"，而是**从源码里把规则和信号全捞出来，
逐条验证每一个都能翻成人话**。加了新规则没补表，这里就红。

三个测试：
  - `test_every_rule_has_a_chinese_name`  —— 源码里的每条规则都能翻译
  - `test_every_signal_has_a_chinese_name` —— 每个派生信号都能翻译
  - `test_web_and_python_agree`            —— Python 与 TS 两份表不漂移

约定同 `tests/test_go2_sim_pure.py`：每个 test 结尾 `print("PASS <name>")`。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from data_dog.data_plane.candidate import _signal_of
from data_dog.go2_sim.faults import FAULT_LIBRARY
from data_dog.go2_sim.semantic import (
    COMPONENT_KIND_NAME,
    COMPONENT_PART_NAME,
    EVENT_TYPE_NAME,
    FAULT_NAME,
    RULE_NAME,
    SIGNAL_NAME,
    component_name,
    describe_fault,
    event_type_name,
    fault_name,
    rule_name,
    signal_name,
)

# 结论半句住在展示层，不在语义层——`providers` 不能依赖 `go2_sim`
# （会成环，tests/test_layering.py 守着），而组句本来就是展示层的活。
from data_dog.presentation import SIGNAL_CLAUSE, signal_clause

# 仓库根：tests/ 的上一级。
_ROOT = Path(__file__).resolve().parents[1]

# 声明规则的**全部**来源。漏一个，那一路的规则就会在界面上漏原串。
#
# ⚠ 这里最初只放了前两个 Python 文件，结果 `BATT_SOC_LOW` 那 3 条真算子规则
#   根本不在检查范围内——测试通过，界面上照样漏原串。**把第 3 个来源补上之后，
#   故意删掉一条表项才验得出红。** 加规则来源时，这个常量必须同步。
_RULE_SOURCES_PY = (
    _ROOT / "src/data_dog/operator_factory/p1_demo.py",
    _ROOT / "src/data_dog/go2_sim/subsystem_operator.py",
)
_RULE_SOURCE_YAML = _ROOT / "operators/generated"

# 规则 ID 的形状：全大写、数字、下划线。用来从字面量里区分"这是规则 ID"。
_RULE_ID_RE = re.compile(r"[A-Z][A-Z0-9_]+")


def _extract_rules() -> set[tuple[str, str]]:
    """从**全部**声明处捞 (rule_id, field) 对——注意是**对**，不是 rule_id 去重。

    ⚠ 同一个 rule_id 可以在多处声明**不同的 field**：`POWER_SOC_LOW` 在
      `p1_demo.py` 里是 `bms_soc`（真链路，字段来自 IDL 映射），在
      `subsystem_operator.py` 里是 `soc`（仿真器载荷）。按 rule_id 去重会让
      后写的那份**覆盖**前一份，于是真链路的 `bms_soc` 永远不进检查范围。
      实测就是这样：字典版全绿，线上候选卡片显示 "bms soc"。

    **四种**声明风格并存，都要认：
      - `subsystem_operator.py`：`rule("POWER_SOC_LOW", "soc", "le", ...)`
      - `p1_demo.py` 字典式：`{"rule_id": "MOTOR_LOAD_SPIKE", ... "field": "tau_est"}`
      - `p1_demo.py` 关键字式：`DeclarativeRule(rule_id="POWER_SOC_LOW", field="bms_soc", ...)`
      - `operators/generated/*.yaml`：`rule_id: BATT_SOC_LOW` / `field: soc`

    ⚠ 关键字式是**第三次**才发现漏掉的，前两次漏的是"来源文件"和"YAML"。
      漏它的后果不是少测一条，而是**凭空造出一个不存在的信号族**：
      `POWER_SOC_LOW` 在关键字式里 field 是 `bms_soc`，漏掉后被 `rule(...)`
      那一版配对成 `soc` → 推出信号族 `soc`（已登记，测试全绿），
      而线上真实值是 `bms_soc`（**没登记**，界面显示 "bms soc"）。
      测试通过、界面漏，和前面两个坑是同一个形状：**表只覆盖了我想到的那部分。**

    YAML 那一路用正则而不用 yaml 解析器：只取两个标量字段，正则够用，
    且不引入"YAML 版本不同解析出的结构不同"这种额外变量。
    """
    found: set[tuple[str, str]] = set()
    for p in _RULE_SOURCES_PY:
        tree = ast.parse(p.read_text(encoding="utf-8"), filename=str(p))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                # `rule("ID", "field", ...)` —— 位置参数
                if (getattr(node.func, "id", "") == "rule"
                        and len(node.args) >= 2
                        and all(isinstance(a, ast.Constant) and isinstance(a.value, str)
                                for a in node.args[:2])):
                    found.add((node.args[0].value, node.args[1].value))
                    continue
                # `DeclarativeRule(rule_id="ID", field="f", ...)` —— 关键字参数
                kw = {k.arg: k.value for k in node.keywords if k.arg}
                rid = kw.get("rule_id")
                if (isinstance(rid, ast.Constant) and isinstance(rid.value, str)
                        and _RULE_ID_RE.fullmatch(rid.value)):
                    fld = kw.get("field")
                    found.add((rid.value,
                               fld.value if isinstance(fld, ast.Constant)
                               and isinstance(fld.value, str) else ""))
            elif isinstance(node, ast.Dict):
                # `{"rule_id": "ID", "field": "f", ...}` —— 字典字面量
                kv = {}
                for k, v in zip(node.keys, node.values, strict=True):
                    if (isinstance(k, ast.Constant) and isinstance(k.value, str)
                            and isinstance(v, ast.Constant) and isinstance(v.value, str)):
                        kv[k.value] = v.value
                rid = kv.get("rule_id")
                if rid and _RULE_ID_RE.fullmatch(rid):
                    found.add((rid, kv.get("field", "")))

    for y in sorted(_RULE_SOURCE_YAML.glob("*.yaml")):
        text = y.read_text(encoding="utf-8")
        # 条目以顶层 `- field:` 开头，`rule_id` 在条目中间。
        # 按 "^field:" 切块（条目内 field 总在首行），再从块里找 rule_id。
        for block in re.split(r"^- ", text, flags=re.M):
            # 条目首行裸写（`- ` 已被切走），其余行带两格缩进 → 两处都要容忍前导空格
            rid = re.search(r"^\s*rule_id:\s*([A-Z][A-Z0-9_]+)", block, re.M)
            if not rid:
                continue
            fld = re.search(r"^\s*field:\s*([a-z_0-9]+)", block, re.M)
            found.add((rid.group(1), fld.group(1) if fld else ""))
    return found


def _has_cjk(s: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in s)


# --------------------------------------------------------------------- 规则

def test_every_rule_has_a_chinese_name() -> None:
    """源码里出现的每条规则 ID，都必须能翻成中文。

    这条是钉坑 1 的：只照着仿真器抄表时，`BATT_SOC_LOW` 这类真算子的规则
    不在表里，`rule_name()` 会退化成 "Batt soc low" 混到界面上。
    """
    pairs = _extract_rules()
    rule_ids = {rid for rid, _ in pairs}
    assert len(rule_ids) >= 20, f"只捞到 {len(rule_ids)} 条规则，提取式样可能失效了"

    missing = sorted(r for r in rule_ids if r not in RULE_NAME)
    assert not missing, (
        f"这些规则没有登记中文名，界面上会显示成下划线串：{missing}\n"
        f"补进 src/data_dog/go2_sim/semantic.py 的 RULE_NAME，"
        f"以及 web/lib/glossary.ts 的 RULE_NAME。"
    )

    for rid in rule_ids:
        name = rule_name(rid)
        assert _has_cjk(name), f"{rid} 翻出来是 {name!r}，没有中文"
        assert "_" not in name, f"{rid} 翻出来是 {name!r}，还带着下划线"

    print(f"PASS test_every_rule_has_a_chinese_name ({len(rule_ids)} 条规则)")


def test_every_signal_has_a_conclusion_clause() -> None:
    """每个信号族都要有"结论里的半句"，否则电池会被说成温度越线。

    这条钉的是台账 A86：`offline_demo._conclusion` 原来三条分支一律写死
    "温度越过厂商保护阈值"，于是 `battery.main` 的结论成了
    "主机电池温度越过厂商保护阈值"——电池报的是 SOC/电压/压差，没有温度。

    要求**两张表键集相同**：只给 SIGNAL_NAME 补名字、不补 SIGNAL_CLAUSE 时，
    结论会静默退化成 "X出现异常"（不说谎，但也不具体）。退化本身是安全的，
    但它会让"补了表"看起来完成了，所以这里卡死。
    """
    pairs = _extract_rules()
    signals = {_signal_of(rid, fld) for rid, fld in pairs}

    missing = sorted(s for s in signals if s not in SIGNAL_CLAUSE)
    assert not missing, (
        f"这些信号族没有结论半句，调查结论会退化成 'X出现异常'：{missing}\n"
        f"补进 presentation.py 的 SIGNAL_CLAUSE（注意方向：温度越上限、电量破下限），"
        f"连 _CONFIRMED_CAUSE / _CONFIRMED_ACTIONS 一起补，三张表要同键集。"
    )
    # 反向检查：SIGNAL_CLAUSE 里不该有 SIGNAL_NAME 不认识的族。
    # 多写一条半句而没有名字，说明那个族从没在真链路出现过——表在自我膨胀。
    orphan = sorted(set(SIGNAL_CLAUSE) - set(SIGNAL_NAME))
    assert not orphan, (
        f"这些半句对应的信号族没有中文名（从没在候选里出现过？）：{orphan}"
    )

    for sig in signals:
        clause = signal_clause(sig)
        assert _has_cjk(clause), f"signal {sig!r} 的半句是 {clause!r}，没有中文"
        assert not clause.startswith(sig), (
            f"signal {sig!r} 的半句直接以原串开头，说明没翻译：{clause!r}"
        )

    print(f"PASS test_every_signal_has_a_conclusion_clause ({len(signals)} 个信号族)")


def test_every_signal_has_a_chinese_name() -> None:
    """每个派生信号族都必须能翻成中文。

    这条是钉坑 2 的：`signal` 来自 `_signal_of(rule_id, field)`，归不进
    四个语义桶时**原样返回字段名**。实测控制台第一张候选卡就是 `tau_est`。
    """
    pairs = _extract_rules()
    signals = {_signal_of(rid, fld) for rid, fld in pairs}
    assert len(signals) >= 10, f"只推出 {len(signals)} 个信号族，提取式样可能失效了"

    missing = sorted(s for s in signals if s not in SIGNAL_NAME)
    assert not missing, (
        f"这些信号族没有登记中文名，候选卡片上会显示成裸字段名：{missing}\n"
        f"补进 semantic.py 的 SIGNAL_NAME 与 glossary.ts 的 SIGNAL_NAME。"
    )

    for sig in signals:
        name = signal_name(sig)
        assert _has_cjk(name), f"signal {sig!r} 翻出来是 {name!r}，没有中文"

    print(f"PASS test_every_signal_has_a_chinese_name ({len(signals)} 个信号族)")


# ---------------------------------------------------------------- 故障 / 部件

def test_fault_table_covers_library() -> None:
    """故障表必须与 `FAULT_LIBRARY` 一一对应。

    `FAULT_NAME` 是**从 FAULT_LIBRARY 派生**的（不是抄的），所以这条
    实际上在钉"派生关系没被谁改成手抄"。手抄就会有两个事实源。
    """
    keys = {f.key for f in FAULT_LIBRARY}
    assert set(FAULT_NAME) == keys, (
        f"FAULT_NAME 与 FAULT_LIBRARY 不一致："
        f"多 {sorted(set(FAULT_NAME) - keys)}，少 {sorted(keys - set(FAULT_NAME))}"
    )
    for f in FAULT_LIBRARY:
        assert fault_name(f.key) == f.label
        assert _has_cjk(f.label), f"{f.key} 的 label 没有中文"

    print(f"PASS test_fault_table_covers_library ({len(keys)} 条故障)")


def test_component_name_reads_naturally() -> None:
    """部件名是"实例在前、类型在后"，且未知部分不与中文粘连。"""
    assert component_name("joint_motor.front_left_hip") == "左前髋关节电机"
    assert component_name("joint_motor.rear_right_knee") == "右后膝关节电机"
    assert component_name("battery.main") == "主机电池"
    # 空串不该炸，也不该返回 None
    assert component_name("") == ""
    # 未知类型 + 已知实例：两边都要露出来，中间用空格隔开
    odd = component_name("unknown_kind.rear_left_foot")
    assert "左后足" in odd and "unknown kind" in odd
    assert "足unknown" not in odd, f"未知类型粘到了中文上：{odd!r}"

    print("PASS test_component_name_reads_naturally")


# ------------------------------------------------------------- 未知值兜底

def test_unknown_values_degrade_without_crashing() -> None:
    """没登记的值不能抛错，也不能原地吐出全大写蛇形串。

    设计取舍：`fault_name` 返回原串（能直接 grep），`rule_name` 去下划线
    （全大写蛇形是日志长相，不是给人念的）。两者不同是有意的。
    """
    assert fault_name("F99_not_a_real_key") == "F99_not_a_real_key"  # 原样，可 grep
    assert rule_name("NEW_RULE_X") == "New rule x"                    # 去下划线
    assert rule_name("") == ""
    assert fault_name("") == ""
    assert event_type_name("some_new_thing") == "some new thing"
    assert signal_name("") == ""
    # 空输入对每个公开函数都不该炸
    for fn in (fault_name, rule_name, signal_name, event_type_name, component_name):
        fn("")

    print("PASS test_unknown_values_degrade_without_crashing")


def test_describe_fault_carries_full_operator_context() -> None:
    """tooltip 需要的三样：名字、故事、症状。缺一个就只剩四个字。"""
    spec = FAULT_LIBRARY[1]
    d = describe_fault(spec.key)
    assert d["key"] == spec.key
    assert d["name"] == spec.label
    assert d["story"] and d["symptom"], f"{spec.key} 缺 story 或 symptom"
    # 未知故障也要给出结构完整的 dict，不能 KeyError
    unknown = describe_fault("F99_x")
    assert unknown["name"] == "F99_x"
    assert unknown["story"] == ""

    print("PASS test_describe_fault_carries_full_operator_context")


# ------------------------------------------------------------ 两侧表一致性

def _parse_ts_table(source: str, name: str) -> dict[str, str]:
    """从 `glossary.ts` 里抠出一个 `export const NAME = { ... }` 表。

    只认 `key: "value"` 这种字面量——这份文件就是纯数据，不需要真解析 TS。
    """
    m = re.search(
        rf"export const {name}: Record<string, string> = \{{(.*?)\n\}};",
        source,
        re.S,
    )
    assert m, f"glossary.ts 里找不到 {name}"
    body = m.group(1)
    # 去掉整行注释，避免把注释里的示例当成数据
    body = "\n".join(ln for ln in body.splitlines() if not ln.strip().startswith("//"))
    return dict(re.findall(r'([A-Za-z_][A-Za-z0-9_]*):\s*"([^"]*)"', body))


def test_web_and_python_agree() -> None:
    """Python 与 TS 两份表必须一致——单边改会让一侧静默漏原串。

    `semantic.py` 是事实源，`glossary.ts` 是 web 打包用的镜像（web 不能
    import Python）。没有这条测试，"补了一边忘了另一边"会一直没人发现，
    直到有人在浏览器里看见下划线。
    """
    ts_path = _ROOT / "web/lib/glossary.ts"
    assert ts_path.exists(), f"找不到 {ts_path}"
    source = ts_path.read_text(encoding="utf-8")

    pairs = (
        ("FAULT_NAME", FAULT_NAME),
        ("RULE_NAME", RULE_NAME),
        ("SIGNAL_NAME", SIGNAL_NAME),
        ("EVENT_TYPE_NAME", EVENT_TYPE_NAME),
    )
    for ts_name, py_table in pairs:
        ts_table = _parse_ts_table(source, ts_name)
        only_py = sorted(set(py_table) - set(ts_table))
        only_ts = sorted(set(ts_table) - set(py_table))
        assert not only_py, f"{ts_name}：Python 有、TS 没有 → {only_py}（web 会漏原串）"
        assert not only_ts, f"{ts_name}：TS 有、Python 没有 → {only_ts}（会互相漂移）"
        for k, v in py_table.items():
            assert ts_table[k] == v, (
                f"{ts_name}[{k!r}] 两侧不一致：Python={v!r} TS={ts_table[k]!r}"
            )
        print(f"  {ts_name}: {len(ts_table)} 条两侧一致")

    # 部件名两张表也要对齐，否则同一部件在控制台和 web 上叫法不同
    for ts_name, py_table in (
        ("COMPONENT_KIND_NAME", COMPONENT_KIND_NAME),
        ("COMPONENT_PART_NAME", COMPONENT_PART_NAME),
    ):
        ts_table = _parse_ts_table(source, ts_name)
        assert ts_table == py_table, f"{ts_name} 两侧不一致"

    print("PASS test_web_and_python_agree")
