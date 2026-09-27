"""P1：文档 Revision 管理 + VendorKnowledgePackage 结构化抽取。

对齐架构：
  §8.1 文档接入   —— 每次接入创建**不可变** document_revision（内容哈希 + 来源 + 语言 +
                     与旧版本关系）。原始文档只读；知识与算子必须引用 revision + 哈希。
  §8.2 内容提取   —— 产出受 Schema 约束的 VendorKnowledgePackage，而非普通 RAG 切片：
                     字段规格（名/类型/单位）、数值规格（阈值/范围，**带定位与来源**）、
                     以及"不确定/仅推断"的内容要**显式标记**——模型推断不能被标为厂商事实。

诚实性设计（本模块的关键约束）：
  - 抽取器**只产出文档里真的能定位到的规格**，每条带 `locator`（行号/小节）与
    `confidence`（verified=文档明写 / inferred=由上下文推断 / unconfirmed=待确认）。
  - 若某个阈值（如电机 75/90°C）在文档里**找不到来源**，抽取器**不会**把它写成厂商事实；
    它只能以 `inferred/unconfirmed` 出现，或由调用方在 assumptions 里显式声明。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------- 文档 Revision

@dataclass(frozen=True)
class DocumentRevision:
    """一次不可变的文档接入（对齐 §8.1）。revision_id 由内容哈希派生。"""
    revision_id: str            # docrev_<sha256前12>，内容寻址
    source: str                 # 相对路径
    content_sha256: str
    language: str = "en"
    vendor: str = "unitree"
    product: str = "go2"
    supersedes: str | None = None   # 替代的旧 revision（可空）
    notes: str = ""


def make_revision(path: Path, *, root: Path, vendor: str = "unitree",
                  product: str = "go2", supersedes: str | None = None) -> DocumentRevision:
    data = path.read_bytes()
    digest = hashlib.sha256(data).hexdigest()
    rel = str(path.relative_to(root)).replace("\\", "/")
    return DocumentRevision(
        revision_id=f"docrev_{digest[:12]}",
        source=rel,
        content_sha256=digest,
        vendor=vendor,
        product=product,
        supersedes=supersedes,
    )


# ---------------------------------------------------------------- 知识包结构

@dataclass
class FieldSpec:
    name: str
    type: str                    # 来自 IDL
    unit: str | None = None
    source: str = ""             # 来源文档（含 revision）
    locator: str = ""            # 定位（行号/小节）
    confidence: str = "verified"  # verified | inferred | unconfirmed


@dataclass
class NumericSpec:
    """文档里明写的数值规格（阈值/范围/限值）。"""
    key: str                     # 语义键，如 operating_temp_min_c / max_speed_mps
    value: float
    unit: str
    source: str
    locator: str                 # 行号
    confidence: str = "verified"
    note: str = ""


@dataclass
class KnowledgeItem:
    """可能是"待确认"的知识条目（文档没写清 / 仅推断）。"""
    key: str
    value: Any
    confidence: str              # inferred | unconfirmed
    reason: str                  # 为什么不是 verified
    source: str = ""
    locator: str = ""


@dataclass
class ErrorCode:
    """厂商协议里的错误码/状态位枚举（如电机 MError）。"""
    code: int
    meaning: str
    source: str
    locator: str
    confidence: str = "verified"


@dataclass
class VendorKnowledgePackage:
    """受约束的结构化知识包（对齐 §8.2）。"""
    vendor: str
    product: str
    revisions: list[str] = field(default_factory=list)          # 引用的 revision_id
    fields: list[FieldSpec] = field(default_factory=list)
    numeric_specs: list[NumericSpec] = field(default_factory=list)
    error_codes: list[ErrorCode] = field(default_factory=list)
    unconfirmed: list[KnowledgeItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------- 抽取器

# 数值 + 单位的正则（只认文档真出现的）。要求数字是**独立 token**：
# 前面不能紧跟字母/数字/点（避免 "2023" 被当成 "2023A"，即使后面误接单位）。
_NUM_UNIT = re.compile(
    r"(?<![\w.])(?P<val>-?\d+(?:\.\d+)?)\s*(?P<unit>°C|℃|V|A|m/s|Hz|kg|rpm|N·m|Nm|W|%)"
)
_IDL_FIELD = re.compile(r"\s*-\s+(\w+):\s*type=(\w+)")

# 页眉/版权/噪声行：不含真实规格，直接跳过
_NOISE_LINE = re.compile(
    r"(All rights reserved|©|\bcopyright\b|^\s*\d+\s*$)",
    re.IGNORECASE,
)
# 年份（1900-2099）不当作规格值
_YEAR_RE = re.compile(r"^(19|20)\d{2}$")

# C 头文件协议注释里的温度保护阈值：如 "电机温度: -128~127°C 90°C时触发温度保护"
# 关键：取**紧邻**"触发/温度保护"的那个数字（不是范围上限 127）。
_PROTECT_TEMP = re.compile(
    r"(?P<val>\d+(?:\.\d+)?)\s*°?\s*C\s*时?\s*(?:触发|开启|进入)?\s*"
    r"(?:温度保护|过热保护|thermal\s+protect|over-?temp)",
    re.IGNORECASE,
)
# 英文语序兜底："triggers thermal protection at 90°C"
_PROTECT_TEMP_EN = re.compile(
    r"(?:thermal\s+protect|over-?temp)[^.\n]*?at\s*(?P<val>\d+(?:\.\d+)?)\s*°?\s*C",
    re.IGNORECASE,
)
# 错误码枚举：如 "0.正常 1.过热 2.过流 3.过压 4.编码器故障 5-7.保留"
# lookahead 兼容 "5-7.保留" 这种区间写法，避免漏掉最后一个单值项。
_ERROR_ENUM = re.compile(
    r"(\d+)\s*[.、)]\s*([^\s\d][^0-9]{0,20}?)(?=\s+\d+(?:\s*-\s*\d+)?\s*[.、)]|\s*$)",
    re.UNICODE,
)
# 温度字段声明（C 头文件）：如 "int8_t temp; // 电机温度"
_TEMP_FIELD = re.compile(r"(?:int8_t|uint8_t|int16_t)\s+(?P<name>temp|temperature)\b\s*;")

# 字段名 → 单位（IDL 本身不带单位，靠命名约定标注为 inferred，不冒充 verified）
_NAME_UNIT = {
    "temperature": "celsius",
    "current": "ampere",
    "soc": "percent",
    "voltage": "volt",
    "q": "radian",
    "dq": "rad/s",
    "ddq": "rad/s2",
}


def _line_of(text: str, needle: str) -> int:
    for i, line in enumerate(text.splitlines(), 1):
        if needle in line:
            return i
    return 0


def extract_fields(idl_text: str, revision: DocumentRevision) -> list[FieldSpec]:
    """从清洗后的 IDL 字段表抽字段规格。单位来自命名约定 → confidence=inferred。"""
    out: list[FieldSpec] = []
    for line in idl_text.splitlines():
        m = _IDL_FIELD.match(line)
        if not m:
            continue
        name, typ = m.group(1), m.group(2)
        unit = _NAME_UNIT.get(name)
        out.append(FieldSpec(
            name=name, type=typ, unit=unit,
            source=f"{revision.source}@{revision.revision_id}",
            locator=f"line:{_line_of(idl_text, f'- {name}:')}",
            confidence="inferred" if unit else "verified",
        ))
    return out


def extract_numeric_specs(doc_text: str, revision: DocumentRevision) -> list[NumericSpec]:
    """从手册/诊断文本抽明写的数值规格（带行号定位）。只抽**能定位**的，不推断阈值。"""
    out: list[NumericSpec] = []
    seen: set[tuple] = set()
    for i, line in enumerate(doc_text.splitlines(), 1):
        if _NOISE_LINE.search(line):
            continue
        low = line.lower()
        for m in _NUM_UNIT.finditer(line):
            raw = m.group("val")
            if _YEAR_RE.match(raw):        # 2023 之类的年份不是规格
                continue
            val = float(raw)
            unit = m.group("unit").replace("℃", "°C")
            # 给已知语境一个语义键；未知语境的按通用键
            key = _semantic_key(low, unit)
            sig = (key, val, unit, i)
            if sig in seen:
                continue
            seen.add(sig)
            out.append(NumericSpec(
                key=key, value=val, unit=unit,
                source=f"{revision.source}@{revision.revision_id}",
                locator=f"line:{i}", confidence="verified",
                note=line.strip()[:120],
            ))
    return out


def _semantic_key(low: str, unit: str) -> str:
    if unit in ("°C",) and ("run the robot" in low or "temperature" in low or "operating" in low):
        return "operating_temp_c"
    if unit == "m/s":
        return "max_speed_mps"
    if unit == "%":
        return "percent_spec"
    if unit == "V":
        return "voltage_spec"
    if unit == "A":
        return "current_spec"
    return f"numeric_spec_{unit}"


def extract_protocol_header(text: str, revision: DocumentRevision
                            ) -> tuple[list[NumericSpec], list[ErrorCode]]:
    """从厂商 C/C++ 协议头（.h）的注释里抽**权威规格**：

      - 温度保护阈值（如 GO-M8010-6：'90°C时触发温度保护'）→ NumericSpec(verified)
      - 错误码枚举（MError：正常/过热/过流/过压/编码器故障）→ ErrorCode(verified)

    这是把"项目假设"升级为"厂商事实"的权威来源（官方开源仓库、BSD-3 许可）。
    """
    specs: list[NumericSpec] = []
    codes: list[ErrorCode] = []
    src = f"{revision.source}@{revision.revision_id}"
    for i, line in enumerate(text.splitlines(), 1):
        # 温度保护阈值（中/英文两种语序）
        for pat in (_PROTECT_TEMP, _PROTECT_TEMP_EN):
            for m in pat.finditer(line):
                specs.append(NumericSpec(
                    key="motor_temp_protect_c", value=float(m.group("val")), unit="°C",
                    source=src, locator=f"line:{i}", confidence="verified",
                    note=line.strip()[:120],
                ))
        # 错误码枚举（仅在含 MError/错误标识/error 的行）
        if re.search(r"MError|错误标识|error", line, re.IGNORECASE) and "//" in line:
            comment = line.split("//", 1)[1]
            for m in _ERROR_ENUM.finditer(comment):
                meaning = m.group(2).strip(" 。.,、")
                if not meaning:
                    continue
                codes.append(ErrorCode(
                    code=int(m.group(1)), meaning=meaning,
                    source=src, locator=f"line:{i}", confidence="verified",
                ))
    return specs, codes


# ---------------------------------------------------------------- 组装

_KNOWN_UNITS = set(_NAME_UNIT.values())

# 非厂商语料（我们自己的说明/许可）——绝不可当语料抽取，否则会循环自证。
_META_NAMES = {"SOURCES.MD", "LICENSE", "README", "README.MD"}


def _is_meta_file(name: str) -> bool:
    return name.upper() in _META_NAMES or name.upper().startswith("LICENSE")


def build_knowledge_package(
    cleaned_dir: Path, *, vendor: str = "unitree", product: str = "go2",
) -> VendorKnowledgePackage:
    """扫描 cleaned/ 下的 IDL、文档与协议头，产出知识包（含 revision 与定位）。

    **只吃厂商语料**：排除 SOURCES.md / LICENSE / README 等我们自己的元文件——
    它们不是厂商文档，若被当语料会污染抽取（真实踩坑，见问题台账）。
    """
    pkg = VendorKnowledgePackage(vendor=vendor, product=product)
    root = cleaned_dir
    for f in sorted(root.rglob("*")):
        if not f.is_file():
            continue
        name = f.name
        if _is_meta_file(name):
            continue
        is_idl = name.endswith(".idl.txt")
        is_md = name.endswith(".md")
        is_h = name.endswith(".h")
        if not (is_idl or is_md or is_h):
            continue
        rev = make_revision(f, root=root, vendor=vendor, product=product)
        pkg.revisions.append(rev.revision_id)
        text = f.read_text(encoding="utf-8", errors="replace")
        if is_idl:
            pkg.fields.extend(extract_fields(text, rev))
        elif is_h:
            specs, codes = extract_protocol_header(text, rev)
            pkg.numeric_specs.extend(specs)
            pkg.error_codes.extend(codes)
        else:
            pkg.numeric_specs.extend(extract_numeric_specs(text, rev))
    return pkg


def flag_unconfirmed(pkg: VendorKnowledgePackage, keys: list[str]) -> None:
    """显式登记"文档里没有、不能当厂商事实"的条目（如电机 75/90°C 阈值）。

    不生成、不编造，只如实标注为待确认——供下游决定用 assumptions 还是转人工。
    """
    for k in keys:
        pkg.unconfirmed.append(KnowledgeItem(
            key=k, value=None, confidence="unconfirmed",
            reason="厂商文档中未找到明确数值来源（不得标为厂商事实，见架构 §8.2）",
        ))
