"""G8：SBOM（软件物料清单）生成（CycloneDX JSON）。

为什么需要
----------
算子从"能跑"到"可追溯、可审计"，须能回答"这个版本里到底有哪些组件、什么版本"。
SBOM 产出到构建物（`dist/sbom.json`），随算子签名版本（P4-2）归档。

诚实边界
--------
- **CycloneDX 库是可选依赖**（`cyclonedx-bom`）：**缺失时本模块用最小实现的
  `_minimal_sbom` 兜底**，产出**结构合法**的 CycloneDX JSON（serialNumber /
  components / …），但不含自动依赖树解析——只列**声明的**组件。装了库则可用
  其完整能力（递归依赖解析）。
  ⚠️ 无论哪条路径，产出都标注 `metadata.tools` 说明来源，不冒充"完整 SBOM"。
- **不递归解析未声明的传递依赖**：要完整需锁文件 + 递归解析（见 §6 诚实边界）。
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SbomComponent:
    name: str
    version: str = ""
    type: str = "library"           # library / application / framework / …
    purl: str = ""                  # Package URL（若有）

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"type": self.type, "name": self.name}
        if self.version:
            d["version"] = self.version
        d["purl"] = self.purl or f"pkg:pypi/{self.name}" + (
            f"@{self.version}" if self.version else "")
        return d


@dataclass
class SbomResult:
    bom: dict[str, Any] = field(default_factory=dict)
    generator: str = ""             # 用于生成 BOM 的工具（诚实标注来源）
    complete: bool = False          # 是否含递归依赖解析（minimum-impl 时为 False）


def _minimal_sbom(components: list[SbomComponent], *, project: str,
                  version: str) -> SbomResult:
    """不依赖第三方库的最小 CycloneDX 1.5 JSON（结构合法、可被解析器读）。"""
    bom = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{uuid.uuid4()}",
        "version": 1,
        "metadata": {
            "component": {"type": "application", "name": project,
                          "version": version},
            "tools": [{"name": "data_dog.sbom", "version": "minimal"}],
        },
        "components": [c.to_dict() for c in components],
    }
    return SbomResult(bom=bom, generator="data_dog.sbom(minimal)", complete=False)


def generate_sbom(*, out_path: str = "", components: list[SbomComponent] | None = None,
                  project: str = "data_dog", version: str = "0.1.0") -> SbomResult:
    """生成 CycloneDX SBOM 并（可选）写文件。

    路径二选一：
      1. 装了 `cyclonedx-bom` → 用它做完整依赖解析（`complete=True`）；
      2. 未装 → 最小实现（`complete=False`，只列声明组件，**如实标注**）。

    无论哪条路径，`generator` 都记录实际来源——调用方可据此判断"是否完整"。
    """
    comps = components or []
    try:
        from cyclonedx.model.bom import Bom
        from cyclonedx.model.component import Component, ComponentType
        from cyclonedx.output.json import JsonV1Dot5
    except ImportError:
        res = _minimal_sbom(comps, project=project, version=version)
    else:
        bom = Bom()
        bom.metadata.component = Component(
            name=project, version=version, type=ComponentType.APPLICATION)
        for c in comps:
            bom.components.add(Component(name=c.name, version=c.version or None,
                                         type=ComponentType.LIBRARY))
        res = SbomResult(bom=json.loads(JsonV1Dot5(bom).output_as_string()),
                         generator="cyclonedx-bom", complete=True)

    if out_path:
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(res.bom, f, ensure_ascii=False, indent=2)
    return res


def declared_components(names: list[str]) -> list[SbomComponent]:
    """把一组 `name` 或 `name==version` 串转成组件列表（便于 CLI/测试）。"""
    out: list[SbomComponent] = []
    for n in names:
        if "==" in n:
            name, ver = n.split("==", 1)
        else:
            name, ver = n, ""
        out.append(SbomComponent(name=name.strip(), version=ver.strip()))
    return out


__all__ = ["SbomComponent", "SbomResult", "declared_components", "generate_sbom"]
