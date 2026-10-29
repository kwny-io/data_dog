"""受限 DSL 的安全求值器（实现 docs/dsl_spec.md v0.1 的可落地子集）。

设计要点（对齐架构 §11 / §8.3）：
  - **完全自建的 词法→递归下降语法→AST 解释器**：不调用任何 Python 的动态代码执行
    原语，因此外部输入不可能变成可执行代码。
  - 函数是**闭集白名单**；白名单外的名字在**编译期**拒绝（不是运行时报错）。
  - 纯函数、确定性：同 (输入, 窗口聚合) → 同输出；禁止随机/挂钟时间。
  - 类型错误在编译期拒绝（如布尔参与算术）。
  - 字段缺失走 missing() 语义，不抛异常中断数据流。

支持的语法子集（v0.1 落地范围）：
  字面量：整数/浮点/布尔/字符串/时长(30s,20ms,5m)
  运算符：or and not == != < <= > >= + - * / % & |（按 dsl_spec §3.1 优先级）
  函数  ：abs min max floor ceil round clamp bit popcount present missing
          crc16 crc32 checksum8
  窗口  ：window.mean/max/min/last/first/sum/std/slope/delta/max_step/
          baseline/count/p50/p95/coverage/mean_abs/baseline_abs/count_abs
  元信息：window.samples() / window.span() /
          window.packet_coverage() / window.packets_lost()（窗口自身，无字段参数）
  括号分组。

未落地（显式声明，避免"假装支持"）：to_celsius 等**单位换算函数**（需算子声明单位表，留待
P2 与 Enricher 一起做）；状态机（dsl_spec §5，本模块只做无状态表达式层）。
"""
from __future__ import annotations

import re
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any


class DSLError(ValueError):
    """DSL 编译或求值错误（统一归类为 invalid_input）。"""


class SandboxViolation(RuntimeError):
    """违反沙箱策略（超时 / 超步 / 越权）。

    定义在 dsl.py（最底层）以免与 sandbox.py 循环导入；sandbox.py 再对外导出。
    继承 RuntimeError 而非 DSLError：`evaluate()` 只吞 DSLError/TypeError/ValueError，
    故本异常会**向上传播**——沙箱越权是安全事件，不能被"视为不命中"悄悄掩盖。

    为何不继承 DSLError：DSLError 语义是"表达式本身有问题 → 不命中即可"，
    而 SandboxViolation 语义是"执行越界，须由调用方记录/熔断"。
    """


# ---------------------------------------------------------------- 词法

_TOKEN_RE = re.compile(
    r"""
    (?P<ws>\s+)
  | (?P<duration>\d+(?:\.\d+)?(?:ms|s|m)\b)
  | (?P<float>\d+\.\d+)
  | (?P<int>0x[0-9a-fA-F]+|\d+)
  | (?P<string>"(?:[^"\\]|\\.)*")
  | (?P<op>==|!=|<=|>=|&&|\|\||[<>+\-*/%&|()!,])
  | (?P<ident>[A-Za-z_][A-Za-z0-9_.]*)
    """,
    re.VERBOSE,
)


@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    pos: int


def tokenize(src: str) -> list[Token]:
    toks: list[Token] = []
    i = 0
    while i < len(src):
        m = _TOKEN_RE.match(src, i)
        if not m:
            raise DSLError(f"无法识别的字符 @{i}: {src[i:i+10]!r}")
        i = m.end()
        kind = m.lastgroup
        if kind == "ws":
            continue
        toks.append(Token(kind or "", m.group(), m.start()))
    toks.append(Token("eof", "", len(src)))
    return toks


# ---------------------------------------------------------------- AST

@dataclass
class Num:
    value: float


@dataclass
class Str:
    value: str


@dataclass
class Bool:
    value: bool


@dataclass
class FieldRef:
    name: str


@dataclass
class Unary:
    op: str
    operand: Any


@dataclass
class Binary:
    op: str
    left: Any
    right: Any


@dataclass
class Call:
    name: str
    args: list[Any]


# 运算符优先级（数字越小越低；对齐 dsl_spec §3.1）
_BIN_PREC = {
    "|": 1,
    "or": 1,
    "&": 2,
    "and": 2,
    "==": 3, "!=": 3,
    "<": 4, "<=": 4, ">": 4, ">=": 4,
    "+": 5, "-": 5,
    "*": 6, "/": 6, "%": 6,
}
_ARITH_OPS = {"+", "-", "*", "/", "%"}

# 函数闭集白名单（dsl_spec §3.3）
BUILTIN_FUNCS = {
    "abs", "min", "max", "floor", "ceil", "round", "clamp", "bit",
    "popcount", "present", "missing", "crc16", "crc32", "checksum8",
}
WINDOW_FUNCS = {
    "mean", "max", "min", "last", "first", "sum", "std", "slope",
    "delta", "baseline", "count", "p50", "p95", "coverage", "max_step",
    "mean_abs", "baseline_abs", "count_abs",
}
# 零参数的窗口函数：取的是**窗口本身**的元信息，不是某个字段的聚合。
# 单独成集，因为解析器的"第一个参数须是字段名"规则对它们不适用。
WINDOW_META_FUNCS = {"samples", "span", "packet_coverage", "packets_lost"}


class Parser:
    """递归下降 + 优先级爬升。"""

    def __init__(self, toks: list[Token]):
        self.toks = toks
        self.i = 0
        self.declared_fields: set[str] = set()

    def peek(self) -> Token:
        return self.toks[self.i]

    def next(self) -> Token:
        t = self.toks[self.i]
        self.i += 1
        return t

    def expect(self, value: str) -> None:
        t = self.next()
        if t.value != value:
            raise DSLError(f"期望 {value!r} @{t.pos}，得到 {t.value!r}")

    # -- 表达式层次 --
    def parse(self, declared_fields: Sequence[str] = ()) -> Any:
        self.declared_fields = set(declared_fields)
        node = self.parse_expr(0)
        if self.peek().kind != "eof":
            raise DSLError(f"表达式尾部有多余内容: {self.peek().value!r}")
        return node

    def parse_expr(self, min_prec: int) -> Any:
        left = self.parse_unary()
        while True:
            t = self.peek()
            op = t.value if t.kind == "op" else (
                t.value if t.kind == "ident" and t.value in ("and", "or") else None
            )
            if op is None or op not in _BIN_PREC:
                break
            prec = _BIN_PREC[op]
            if prec < min_prec:
                break
            self.next()
            right = self.parse_expr(prec + 1)
            left = Binary(op, left, right)
        return left

    def parse_unary(self) -> Any:
        t = self.peek()
        if t.value == "not":
            self.next()
            return Unary("not", self.parse_unary())
        if t.value == "-":
            self.next()
            return Unary("-", self.parse_unary())
        return self.parse_atom()

    def parse_atom(self) -> Any:
        t = self.next()
        if t.kind == "float":
            return Num(float(t.value))
        if t.kind == "int":
            return Num(int(t.value, 0))
        if t.kind == "duration":
            return Num(_duration_to_seconds(t.value))
        if t.kind == "string":
            return Str(_unquote(t.value))
        if t.kind == "ident":
            if t.value == "true":
                return Bool(True)
            if t.value == "false":
                return Bool(False)
            # 函数调用 or 字段
            if self.peek().value == "(":
                return self.parse_call(t.value)
            return self._make_field_ref(t)
        if t.value == "(":
            node = self.parse_expr(0)
            self.expect(")")
            return node
        raise DSLError(f"无法解析的原子 @{t.pos}: {t.value!r}")

    def _make_field_ref(self, t: Token) -> FieldRef:
        if t.value.startswith("window."):
            raise DSLError(f"window 聚合请写作 window.mean(...) 形式，不能用 {t.value!r}")
        if self.declared_fields and t.value not in self.declared_fields:
            raise DSLError(f"未声明的字段 {t.value!r}（须先在算子 fields 里声明）")
        return FieldRef(t.value)

    def parse_call(self, name: str) -> Any:
        """解析函数调用。window 聚合写作 window.mean(field) —— 词法把 window.mean
        识别为一个 ident，这里据前缀判定。"""
        is_window = name == "window" or name.startswith("window.")
        self.expect("(")
        args: list[Any] = []
        if self.peek().value != ")":
            while True:
                args.append(self.parse_expr(0))
                if self.peek().value == ",":
                    self.next()
                    continue
                break
        self.expect(")")
        if is_window:
            func = name.split(".", 1)[1] if "." in name else ""
            if func not in WINDOW_FUNCS and func not in WINDOW_META_FUNCS:
                raise DSLError(f"未知窗口函数 {func!r}（闭集白名单见 dsl_spec §3.5）")
            if func in WINDOW_META_FUNCS:
                # window.samples() / window.span()：窗口元信息，无字段参数。
                if args:
                    raise DSLError(f"window.{func}() 不接受参数")
                return Call("window." + func, args)
            if not args or not isinstance(args[0], FieldRef):
                raise DSLError(f"window.{func}(...) 第一个参数须是字段名")
            return Call("window." + func, args)
        if name in WINDOW_FUNCS:
            raise DSLError(f"{name} 是窗口函数，须写作 window.{name}(...)")
        if name not in BUILTIN_FUNCS:
            raise DSLError(f"未知函数 {name!r}（函数为闭集白名单，见 dsl_spec §3.3）")
        return Call(name, args)


def _unquote(s: str) -> str:
    body = s[1:-1]
    return body.replace('\\"', '"').replace("\\\\", "\\")


def _duration_to_seconds(s: str) -> float:
    m = re.fullmatch(r"(\d+(?:\.\d+)?)(ms|s|m)", s)
    if not m:
        raise DSLError(f"非法时长: {s!r}")
    v = float(m.group(1))
    unit = m.group(2)
    return v / 1000.0 if unit == "ms" else v * 60.0 if unit == "m" else v


# ---------------------------------------------------------------- 包级到达率

def packet_coverage_of(seqs: Sequence[int | None]) -> dict[str, Any]:
    """按**设备序列号**的连续性算包级到达率。

    放在这里（而不是 `data_plane.chain`）是分层决定的：在线窗口与离线 fixture
    都要用同一份算法，而 `data_plane` 已经单向依赖本模块（`Detector` 调 `evaluate`）。
    反向放会造出 `data_plane ↔ operator_factory` 的循环导入，`test_layering` 会红。

    **为什么不能靠字段级覆盖率**：`len(series[f]) / samples`，整包丢失时包没进网关，
    `push()` 没被调用、`samples` 也不增加，分子分母一起缩水 → 恒为 1.0。
    唯一能揭示丢包的是设备序列号：收到 3、5 就说明 4 丢了。

    `expected` 的算法：每相邻一对算"这一段应该发生几包"，正常 +1，有缺口时
    `b - a` 就是应发数（如 3→5 表示中间该有 4，共 2 包）。
    **不能写成 `max(0, b-a-1)` 再求和**：`Σ(b-a)` 会望远镜式抵消成 `last-first`，
    再减 1 就少算每段的第一包——实测该写法把 60/80 算成 20/20（覆盖率 1.0，报不出来）。

    返回 `known=False` 的情形（调用方必须跳过判据）：
      - 不足两条（一条算不出"相邻差"）；
      - 任一条缺序列号（不知道就别说，别把"没给"当成"没丢"）。
    """
    if len(seqs) < 2 or any(s is None for s in seqs):
        return {"known": False, "expected": 0, "arrived": 0, "lost": 0,
                "coverage": None, "samples": len(seqs)}
    # 收窄到 `list[int]`：上面那条已经把 None 全部挡掉，但 `any(...)` 不是
    # 类型谓词，检查器看不到这层保证。不写 `cast` 而重新构造一次列表——
    # 代价是 n 个元素，换来的是**真的**过滤而不是骗过检查器。
    vals = [int(s) for s in seqs if s is not None]
    arrived = len(vals)
    # 每一步"应该发生几包"：正常 +1；有缺口时 b-a 就是这一段应发的包数
    # （如 3→5 说明中间该有 4，共 2 包）。b-a<=0（回跳/重复/重启归零）按 1 算，
    # 即"这是紧接着的一条"，不把它当成丢包——设备重启是常态，误报代价高。
    expected = 1 + sum(max(1, b - a) for a, b in zip(vals, vals[1:], strict=False))
    lost = expected - arrived
    return {"known": True, "expected": expected, "arrived": arrived,
            "lost": lost, "coverage": arrived / expected, "samples": arrived}


# ---------------------------------------------------------------- 窗口上下文

@dataclass
class WindowContext:
    """窗口聚合只依赖事件时间序列（确定性）。缺失字段 → None，按 missing() 语义处理。

    `samples`：该窗口收到的**样本条数**（与字段无关）。为 0 表示"不知道"，此时
    `window.coverage` 返回 None（→ 涉及它的规则求值期失败 → 不命中），而不是把
    "一个样本都没收到"当成"覆盖率 0 / 全丢"去误报丢包。

    `times`：field → 与 `series[field]` 等长的**事件时间**（秒）。给了就用真实时间算
    `slope`（单位 = 字段单位/秒，与采样率无关）；没给则退回按样本序号（等价于 1Hz）。
    这是 dsl_spec §3.4「窗口聚合只依赖事件时间」的落地——不这么做，同一个斜率阈值
    在 10Hz 与 100Hz 下含义完全不同。

    `packets`：**包级**到达情况（`WindowBuffer.packet_coverage` 的返回值），
    形如 `{"known": True, "expected": 62, "arrived": 53, "lost": 9, "coverage": .85}`。
    与 `samples` 是两个不同的量，不能互相替代：
      - `samples` = 网关**收到**几条 → 整包丢失时它也跟着变小，看不出缺口；
      - `packets` = 设备**发出**几条、实际到了几条（按序列号连续性算）。
    `window.coverage(field)` 用的是前者，因此对整包丢失结构上不可见；
    判包级丢包必须用 `window.packet_coverage()`。`known=False` 时它返回 None
    （"不知道"），与 `coverage` 同一降级语义。
    """
    series: dict[str, list[float]] = field(default_factory=dict)
    baseline: dict[str, float] = field(default_factory=dict)
    samples: int = 0
    times: dict[str, list[float]] = field(default_factory=dict)
    packets: dict[str, Any] = field(default_factory=dict)

    def _vals(self, name: str) -> list[float] | None:
        return self.series.get(name)

    def meta(self, func: str) -> Any:
        """窗口自身元信息（无字段参数）。

        - `samples`：窗口收到的样本条数（`<=0` 表示不知道 → None）。
        - `span`：窗口的事件时间跨度（秒）。**需要 `times` 或各序列等长可推断**；
          拿不到事件时间时返回 None，绝不用"样本条数"冒充秒数——否则同一个
          阈值在 10Hz 与 100Hz 下含义差 10 倍。
        - `packet_coverage`：包级到达率 = 实到 / 应有（按设备序列号空洞算）。
          `known=False`（序列号不足/缺失）→ None，绝不返回 1.0 冒充"没丢包"。
        - `packets_lost`：窗口内**整包丢失**的条数。同上，不知道 → None。
        """
        if func == "samples":
            return self.samples if self.samples > 0 else None
        if func == "packet_coverage":
            if not self.packets.get("known"):
                return None
            return self.packets.get("coverage")
        if func == "packets_lost":
            if not self.packets.get("known"):
                return None
            return self.packets.get("lost")
        if func == "span":
            xs: list[float] = []
            for fld, vals in self.series.items():
                t = self.times.get(fld)
                if t and len(t) == len(vals):
                    xs = t
                    break
            if len(xs) < 2:
                return None
            return max(xs) - min(xs)
        raise DSLError(f"未知窗口元信息: {func}")

    def agg(self, func: str, field_name: str, extra: tuple = ()) -> Any:
        if func == "coverage":
            # 该字段在窗口里的**有效率** = 有效点数 / 窗口样本数。
            # 丢包/解析不出的样本不会在 series 里留下占位（见 chain.WindowBuffer），
            # 所以"有效率下降"正是丢包的可观测信号。
            # 无样本（samples<=0）→ None（不知道），绝不当成 0 去误报。
            if self.samples <= 0:
                return None
            return len(self.series.get(field_name) or ()) / self.samples
        vals = self._vals(field_name)
        if vals is None or not vals:
            return None
        if func == "mean":
            return sum(vals) / len(vals)
        if func == "mean_abs":
            # |x| 的窗口均值。**不是 abs(mean(x))**——后者在正负交替的序列上
            # 会因相互抵消而趋近 0。力矩 `tau_est` 在步态中 44% 的样本为负，
            # 只有先取绝对值再平均，才能和恒非负的 `current` 比较。
            return sum(abs(v) for v in vals) / len(vals)
        if func == "max":
            return max(vals)
        if func == "min":
            return min(vals)
        if func == "sum":
            return sum(vals)
        if func == "first":
            return vals[0]
        if func == "last":
            return vals[-1]
        if func == "delta":
            return vals[-1] - vals[0]
        if func == "max_step":
            # 相邻样本的最大**绝对**跳变：编码器跳变/信号突跳的可观测信号。
            # 与 slope 不同，它对单点毛刺敏感而对面上的缓升不敏感——正是要区分
            # "编码器坏了（一跳多半圈）"和"温度在慢慢爬"。
            if len(vals) < 2:
                return 0.0
            # 相邻对，长度天然差 1，strict 必须为 False。
            return max(abs(b - a) for a, b in zip(vals, vals[1:], strict=False))
        if func == "std":
            mu = sum(vals) / len(vals)
            return (sum((v - mu) ** 2 for v in vals) / len(vals)) ** 0.5
        if func == "slope":
            n = len(vals)
            if n < 2:
                return 0.0
            xs = self.times.get(field_name)
            if xs is None or len(xs) != n:
                xs = list(range(n))     # 无事件时间 → 按序号（等价 1Hz，旧行为）
            mean_x = sum(xs) / n
            mu_y = sum(vals) / n
            # 上面已保证 len(xs) == n == len(vals)，不等就是 bug，不该静默截断。
            num = sum((x - mean_x) * (v - mu_y) for x, v in zip(xs, vals, strict=True))
            den = sum((x - mean_x) ** 2 for x in xs)
            return num / den if den else 0.0
        if func == "baseline":
            return self.baseline.get(field_name)
        if func == "baseline_abs":
            # 与 `mean_abs(field)` 配对的基线：`|x|` 的滚动均值，取不到就返回 None
            # （→ 规则不命中），绝不回退到有符号基线——那正是要被修掉的错配。
            return self.baseline.get(f"{field_name}|abs")
        if func in ("p50", "p95"):
            q = 0.50 if func == "p50" else 0.95
            s = sorted(vals)
            idx = min(len(s) - 1, int(round(q * (len(s) - 1))))
            return s[idx]
        if func in ("count", "count_abs"):
            if len(extra) != 2:
                raise DSLError(f"window.{func}(field, op, value) 需要 2 个额外参数")
            op_e, thr_e = extra
            cmp = {
                ">": lambda a, b: a > b, "<": lambda a, b: a < b,
                ">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b,
                "==": lambda a, b: a == b, "!=": lambda a, b: a != b,
            }.get(op_e)
            if cmp is None:
                raise DSLError(f"window.{func} 的比较符非法: {op_e!r}")
            thr = _as_number(thr_e)
            if func == "count_abs":
                # 对 |x| 计数，与 `mean_abs` / `baseline_abs` 同一族。
                # 有符号量（`dq` / `tau_est`）判"大小"必须走这个：
                # `count(dq, "<", 0.05)` 在**反向转动**时同样成立——实测奔跑有符号
                # 占比 46.2%，距 50% 门槛只差 3.8 个点；取 |dq| 后是 0.8%。
                return sum(1 for v in vals if cmp(abs(v), thr))
            return sum(1 for v in vals if cmp(v, thr))
        raise DSLError(f"未知窗口函数: {func}")


# ---------------------------------------------------------------- 求值

def _as_number(x: Any) -> float:
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        raise DSLError(f"期望数值，得到 {x!r}")
    return x


class Evaluator:
    def __init__(self, fields: dict[str, Any], window: WindowContext | None = None,
                 max_steps: int = 0, wall_s: float = 0.0):
        self.fields = fields
        self.window = window or WindowContext()
        # Tier 0 预算：max_steps<=0 表示不限步；wall_s<=0 表示不限时（默认不限，
        # 保持既有语义不变——只有走沙箱的执行器才传非零预算）。
        self.max_steps = max_steps
        self._steps = 0
        self._deadline = (time.monotonic() + wall_s) if wall_s and wall_s > 0 else 0.0

    # -- 预算检查（Tier 0）--
    def _tick(self) -> None:
        """每访问一个 AST 节点记一步；超步 / 超时即抛 SandboxViolation。

        检查点是**节点级**（不是每条语句级）：DSL 无循环/无递归，表达式深度即
        节点数，所以节点计数天然有界——但深链如 a+b+c+... （解析为左深树）或
        巨大 window 输入仍需封顶，故保留。
        """
        if self.max_steps:
            self._steps += 1
            if self._steps > self.max_steps:
                raise SandboxViolation(
                    f"步数超预算 {self.max_steps}（疑似超长表达式）")
        if self._deadline and time.monotonic() > self._deadline:
            raise SandboxViolation("求值超墙钟预算（疑似超长窗口输入）")

    # -- 编译期类型检查 --
    def check(self, node: Any) -> str:
        """返回表达式静态类型：'num' | 'bool' | 'str'。类型错误即编译失败。"""
        if isinstance(node, Num):
            return "num"
        if isinstance(node, Str):
            return "str"
        if isinstance(node, Bool):
            return "bool"
        if isinstance(node, FieldRef):
            return "any"
        if isinstance(node, Unary):
            inner = self.check(node.operand)
            if node.op == "not":
                if inner not in ("bool", "any"):
                    raise DSLError(f"not 需要布尔操作数，得到 {inner}")
                return "bool"
            if inner not in ("num", "any"):
                raise DSLError(f"一元 - 需要数值，得到 {inner}")
            return "num"
        if isinstance(node, Binary):
            lt, rt = self.check(node.left), self.check(node.right)
            if node.op in ("and", "or"):
                if lt not in ("bool", "any") or rt not in ("bool", "any"):
                    raise DSLError(f"{node.op} 需要布尔操作数，得到 {lt}/{rt}")
                return "bool"
            if node.op in _ARITH_OPS or node.op == "&":
                if lt not in ("num", "any") or rt not in ("num", "any"):
                    raise DSLError(f"{node.op} 需要数值操作数，得到 {lt}/{rt}")
                return "num"
            if node.op in ("==", "!="):
                return "bool"
            if node.op in ("<", "<=", ">", ">="):
                if (lt == "bool" or rt == "bool") and lt != "any" and rt != "any":
                    raise DSLError("比较运算符不接受布尔操作数")
                return "bool"
            raise DSLError(f"未知运算符 {node.op!r}")
        if isinstance(node, Call):
            return self._check_call(node)
        raise DSLError(f"未知 AST 节点: {type(node).__name__}")

    def _check_call(self, node: Call) -> str:
        name = node.name
        if name in ("present", "missing"):
            return "bool"
        if name in ("window.count", "window.count_abs"):
            return self._check_count(node)
        for a in node.args:
            t = self.check(a)
            if t not in ("num", "any"):
                raise DSLError(f"{name} 的参数需为数值，得到 {t}")
        return "num"

    def _check_count(self, node: Call) -> str:
        """window.count / count_abs(field, op, value)：第 2 参是**字符串比较符**，不是数值。

        特判原因：通用循环要求所有参数为数值，会让 window.count 在**编译期**就被拒，
        于是"持续饱和/持续高温"这类占比判据根本写不出来（实测踩到）。
        """
        fn = node.name
        self.check(node.args[0]) if node.args else None
        if len(node.args) != 3:
            raise DSLError(f"{fn}(field, op, value) 需要 3 个参数")
        op_node = node.args[1]
        if not isinstance(op_node, Str):
            raise DSLError(f"{fn} 第 2 个参数须是字符串比较符，如 \">=\"")
        if op_node.value not in (">", "<", ">=", "<=", "==", "!="):
            raise DSLError(f"{fn} 的比较符非法: {op_node.value!r}")
        t = self.check(node.args[2])
        if t not in ("num", "any"):
            raise DSLError(f"{fn} 的比较值需为数值，得到 {t}")
        return "num"

    # -- 求值（自建 AST 解释器：不触碰任何动态代码执行原语）--
    def eval_node(self, node: Any) -> Any:
        self._tick()
        if isinstance(node, Num):
            return node.value
        if isinstance(node, Str):
            return node.value
        if isinstance(node, Bool):
            return node.value
        if isinstance(node, FieldRef):
            return self.fields.get(node.name)
        if isinstance(node, Unary):
            v = self.eval_node(node.operand)
            if node.op == "not":
                return not bool(v)
            return -_as_number(v)
        if isinstance(node, Binary):
            return self._eval_binary(node)
        if isinstance(node, Call):
            return self._eval_call(node)
        raise DSLError(f"未知 AST 节点: {type(node).__name__}")

    def _eval_binary(self, node: Binary) -> Any:
        op = node.op
        # 短路
        if op == "and":
            return bool(self.eval_node(node.left)) and bool(self.eval_node(node.right))
        if op == "or":
            return bool(self.eval_node(node.left)) or bool(self.eval_node(node.right))
        if op == "|":
            return int(_as_number(self.eval_node(node.left))) | int(_as_number(self.eval_node(node.right)))
        lv = self.eval_node(node.left)
        rv = self.eval_node(node.right)
        if op in ("==", "!="):
            eq = (lv == rv)
            return eq if op == "==" else (not eq)
        ln, rn = _as_number(lv), _as_number(rv)
        if op == "+":
            return ln + rn
        if op == "-":
            return ln - rn
        if op == "*":
            return ln * rn
        if op == "/":
            if rn == 0:
                raise DSLError("除零")
            return ln / rn
        if op == "%":
            if rn == 0:
                raise DSLError("取模除零")
            return ln % rn
        if op == "&":
            return int(ln) & int(rn)
        if op == "<":
            return ln < rn
        if op == "<=":
            return ln <= rn
        if op == ">":
            return ln > rn
        if op == ">=":
            return ln >= rn
        raise DSLError(f"未知运算符 {op!r}")

    def _eval_call(self, node: Call) -> Any:
        name = node.name
        if name in ("present", "missing"):
            ref = node.args[0]
            if not isinstance(ref, FieldRef):
                raise DSLError(f"{name} 需要字段名")
            has = self.fields.get(ref.name) is not None
            return has if name == "present" else (not has)
        if name.startswith("window."):
            func = name.split(".", 1)[1]
            if func not in WINDOW_FUNCS and func not in WINDOW_META_FUNCS:
                raise DSLError(f"未知窗口函数 {func!r}")
            self._tick()  # 单个大聚合调用前再查一次，防超长窗口拖死
            if func in WINDOW_META_FUNCS:
                return self.window.meta(func)
            fld = node.args[0].name
            extra = tuple(self.eval_node(a) for a in node.args[1:])
            return self.window.agg(func, fld, extra)

        args = [self.eval_node(a) for a in node.args]
        if name == "abs":
            return abs(_as_number(args[0]))
        if name == "min":
            return min(_as_number(a) for a in args)
        if name == "max":
            return max(_as_number(a) for a in args)
        if name == "floor":
            import math
            return math.floor(_as_number(args[0]))
        if name == "ceil":
            import math
            return math.ceil(_as_number(args[0]))
        if name == "round":
            return round(_as_number(args[0]))
        if name == "clamp":
            x, lo, hi = (_as_number(a) for a in args[:3])
            return max(lo, min(hi, x))
        if name == "bit":
            return (int(_as_number(args[0])) >> int(_as_number(args[1]))) & 1
        if name == "popcount":
            return bin(int(_as_number(args[0]))).count("1")
        if name in ("crc16", "crc32", "checksum8"):
            return _checksum(name, self._to_bytes(args[0]))
        raise DSLError(f"未知函数 {name!r}")

    @staticmethod
    def _to_bytes(v: Any) -> bytes:
        if isinstance(v, (bytes, bytearray)):
            return bytes(v)
        if isinstance(v, str):
            return v.encode("utf-8")
        if isinstance(v, (list, tuple)):
            return bytes(int(x) & 0xFF for x in v)
        raise DSLError(f"校验函数需要字节序列/字符串/整数数组，得到 {type(v).__name__}")


def _checksum(kind: str, data: bytes) -> int:
    if kind == "checksum8":
        return sum(data) & 0xFF
    if kind == "crc16":  # CRC-16/CCITT-FALSE
        crc = 0xFFFF
        for b in data:
            crc ^= b << 8
            for _ in range(8):
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
        return crc
    # crc32 (IEEE)
    crc = 0xFFFFFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xEDB88320 if crc & 1 else crc >> 1
    return (~crc) & 0xFFFFFFFF


# ---------------------------------------------------------------- 对外 API

@dataclass
class CompiledRule:
    rule_id: str
    source: str
    ast: Any
    severity_hint: str = "warning"
    message: str = ""
    for_seconds: float | None = None


def compile_rule(rule: dict, declared_fields: Sequence[str] = ()) -> CompiledRule:
    """把一条 dsl_rule 编译成可复用的 CompiledRule；语法/类型/白名单错误在此暴露。"""
    if "when" not in rule:
        raise DSLError("dsl_rule 缺少 when")
    parser = Parser(tokenize(str(rule["when"])))
    ast = parser.parse(declared_fields)
    ev = Evaluator({})
    ty = ev.check(ast)
    if ty not in ("bool", "any"):
        raise DSLError(f"when 必须求值为布尔，实际为 {ty}")
    for_s = None
    if "for" in rule:
        for_s = _duration_to_seconds(str(rule["for"]))
    sev = rule.get("severity_hint", "warning")
    if sev not in ("info", "warning", "critical"):
        raise DSLError(f"severity_hint 非法: {sev!r}")
    return CompiledRule(
        rule_id=str(rule.get("rule_id", "")),
        source=str(rule["when"]),
        ast=ast,
        severity_hint=sev,
        message=str(rule.get("message", "")),
        for_seconds=for_s,
    )


def evaluate(rule: CompiledRule, fields: dict[str, Any],
             window: WindowContext | None = None,
             max_steps: int = 0, wall_s: float = 0.0) -> bool:
    """对单个事件（+窗口上下文）求值，返回是否命中。

    健壮性：任何求值期异常（字段缺失导致的 None 比较、类型不符等）一律视为"不命中"，
    **不得抛出**中断数据流（架构 §10.3：字段缺失走 missing 语义，不抛异常）。

    例外：`SandboxViolation`（继承 RuntimeError）**不在此吞掉**——它是安全事件，
    须由调用方（沙箱执行器）记录并熔断，不能被伪装成"不命中"。
    """
    try:
        return bool(Evaluator(fields, window, max_steps=max_steps, wall_s=wall_s).eval_node(rule.ast))
    except (DSLError, TypeError, ValueError):
        return False
