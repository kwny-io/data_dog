# -*- coding: utf-8 -*-
"""P2 兼容性绑定解析（对齐架构 §9.2）。

按 `vendor + model + firmware + protocol_version` 选择**唯一**算子集合。
匹配到多个 → `ambiguous`；匹配不到 → `not_found`。**绝不猜测。**
绑定结果不可变，并对设备保持粘性（变更只能经控制面生成新 Revision——本层只做选择）。

说明：这里把"算子包"抽象成 OperatorBinding（含 parser/normalizer/detector 三层与
兼容范围）。P1 生成的算子可注册为其中一个 detector。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..errors import ToolErrorCode, err


def _ver_tuple(v: str) -> Tuple[int, ...]:
    """宽松版本解析：非数字段忽略，用于比较。"""
    out = []
    for part in str(v).replace("-", ".").split("."):
        out.append(int(part) if part.isdigit() else 0)
    return tuple(out) or (0,)


def _in_range(ver: str, spec: Optional[str]) -> bool:
    """支持 None（不约束）/ 精确 "3.2.1" / 区间 ">=3.1,<4.0"。"""
    if spec is None or spec == "":
        return True
    v = _ver_tuple(ver)
    if spec.startswith((">=", "<=", ">", "<", "=")):
        for cond in spec.split(","):
            cond = cond.strip()
            for op in (">=", "<=", ">", "<", "="):
                if cond.startswith(op):
                    target = _ver_tuple(cond[len(op):])
                    if op == ">=" and not v >= target:
                        return False
                    if op == "<=" and not v <= target:
                        return False
                    if op == ">" and not v > target:
                        return False
                    if op == "<" and not v < target:
                        return False
                    if op == "=" and not v == target:
                        return False
                    break
        return True
    return v == _ver_tuple(spec)      # 精确版本


@dataclass(frozen=True)
class Compatibility:
    """算子的兼容范围（§9.2 绑定维度）。"""
    vendor: str
    models: Tuple[str, ...] = ()
    firmware: Optional[str] = None          # 精确或区间
    protocol_versions: Tuple[str, ...] = ()


@dataclass
class OperatorBinding:
    """一组算子绑定（一个 detector + 可选的 parser/normalizer 名）。"""
    operator_id: str
    version: str
    compat: Compatibility
    parser: Optional[str] = None
    normalizer: Optional[str] = None
    detector: Optional[str] = None           # 对应 P1 生成的检测算子 id
    extra: Dict[str, Any] = field(default_factory=dict)

    @property
    def revision(self) -> str:
        return f"{self.operator_id}@{self.version}"


class CompatibilityResolver:
    """把一个（vendor/model/firmware/protocol）请求解析为唯一绑定。"""

    def __init__(self, bindings: Optional[List[OperatorBinding]] = None):
        self._bindings: List[OperatorBinding] = list(bindings or [])

    def register(self, binding: OperatorBinding) -> None:
        self._bindings.append(binding)

    def resolve(self, *, vendor: str, model: str, firmware: str,
                protocol_version: str = "") -> Tuple[bool, Any]:
        """返回 (ok, binding_or_error)。匹配不唯一/无匹配 → 失败（不猜）。"""
        matches = [b for b in self._bindings if self._match(b.compat, vendor, model,
                                                             firmware, protocol_version)]
        if not matches:
            return False, err(ToolErrorCode.NOT_FOUND,
                              f"无匹配绑定: {vendor}/{model}/fw{firmware}/p{protocol_version}")
        if len(matches) > 1:
            return False, err(ToolErrorCode.AMBIGUOUS,
                              f"匹配到 {len(matches)} 个绑定: {[m.revision for m in matches]}")
        return True, matches[0]

    @staticmethod
    def _match(c: Compatibility, vendor: str, model: str, firmware: str,
               protocol_version: str) -> bool:
        if c.vendor != vendor:
            return False
        if c.models and model not in c.models:
            return False
        if not _in_range(firmware, c.firmware):
            return False
        if c.protocol_versions and protocol_version not in c.protocol_versions:
            return False
        return True
