# G8 算子验证流水线补全（编译扫描 + SBOM）

## 1. 目标

算子验证流水线现有**黄金回放 + Fuzz + 新旧差分**，缺两环：
① **编译期安全扫描**（算子包/依赖的静态与 SCA 检查）；② **SBOM**（软件物料清单）。
补齐后，算子从"能跑"到"可追溯、可审计"。

## 2. 现状（已核实）

| 已有 | 位置 | 说明 |
|---|---|---|
| Fuzz | `operator_factory/verification.py:49-82` `fuzz_operator` | 固定种子畸形输入，不崩、不误命中 |
| 差分 | 同上 `:105-127` `diff_replay` | 新旧算子差分 + 白名单可解释 |
| 黄金回放 | `operator_factory/pipeline.py:130-156` | `replay`/`replay_all`（Fixture 6/6） |
| 状态机 | `operator_factory/lifecycle.py` | `Validating` 阶段门控（部分就位） |

**缺**：bandit/safety/pip-audit/SBOM/cyclonedx（全仓库 grep 零命中）；`pyproject.toml` 无任何
安全扫描/SCA/SBOM 工具。

## 3. 设计

### 3.1 编译期安全扫描（静态 + SCA）

在 `Validating` 阶段（lifecycle）插两步：

```python
# operator_factory/security_scan.py（新建）
@dataclass(frozen=True)
class ScanFinding:
    severity: str      # low/medium/high/critical
    kind: str          # dsl_static / dependency
    detail: str
    location: str

def scan_operator(dsl_source: str) -> list[ScanFinding]:
    """① DSL 静态扫描：编译后 AST 走一遍，检出违规构造。"""

def scan_dependencies(lockfile: str) -> list[ScanFinding]:
    """② 依赖 SCA：已知漏洞检查（safety / pip-audit，可选依赖）。
       本项提供**接口**；工具缺失时跳过并明确记 skipped（非静默通过）。"""
```

- **DSL 静态扫描**（自研，零依赖）：DSL 无网络/文件/循环，但可检——
  **超长表达式**（疑混淆）、**超深嵌套**（资源风险）、**超大常量**、**可疑模式**。
  与 P4-1 的 step 预算**互补**（静态前置 + 动态兜底）。
- **依赖 SCA**：算子可声明依赖（若允许）→ 查已知漏洞。工具用 `pip-audit`
  （纯 Python、易接）为**可选依赖**，缺失时 `skipped` 并在报告注明——**不假装通过了**。

### 3.2 SBOM 生成

- 用 **CycloneDX**（`cyclonedx-bom` / `cyclonedx-py`，可选依赖）生成 **SPDX/CycloneDX** 格式
  SBOM，覆盖算子包 + 依赖树。
- 产出到构建物（`dist/sbom.json`），随算子签名版本（P4-2）归档——**可追溯到每个组件的版本**。

```python
# operator_factory/sbom.py（新建）
def generate_sbom(*, out_path: str, components: list[str] | None = None) -> str:
    """生成 CycloneDX SBOM；缺 cyclonedx 依赖 → 报清晰错误（指出装什么）。"""
```

### 3.3 编排进流水线

`Validating` 阶段顺序：
```
golden replay → fuzz → diff → 【新】security_scan → 【新】sbom
   任一 critical/high 失败 → Rejected（对齐 lifecycle 门控）
   工具缺失 → skipped（记录，不拦）
```

`verification.py` 加一个 `verify_full()` 聚合，返回结构化结果（各步 pass/fail/skipped）。

### 3.4 CI

`.github/workflows/ci.yml` 加：
- `pip-audit` 依赖扫（仅告警，不硬拦——现有依赖可能已有 CVE）；
- SBOM 生成并作为**构建产物上传**（artifact）。

## 4. 要建/要改的文件

| 文件 | 动作 |
|---|---|
| `src/data_dog/operator_factory/security_scan.py` | **新建**：DSL 静态扫描 + 依赖 SCA 接口 |
| `src/data_dog/operator_factory/sbom.py` | **新建**：CycloneDX SBOM 生成 |
| `src/data_dog/operator_factory/verification.py` | 加 `verify_full()` 聚合 |
| `src/data_dog/operator_factory/lifecycle.py` | `Validating` 门控接入新两步 |
| `pyproject.toml` | 加可选依赖 `scan = ["pip-audit"]`、`sbom = ["cyclonedx-bom"]` |
| `.github/workflows/ci.yml` | 加 SCA + SBOM artifact |
| `tests/test_security_scan.py` | **新建** |

## 5. 验收

1. **DSL 扫描**：超深嵌套/超长表达式算子 → 报 finding；正常算子 → 无 finding。
2. **SCA 接口**：有 `pip-audit` → 真扫；无 → `skipped`（**明确标注，不静默通过**）。
3. **SBOM**：生成合法 CycloneDX JSON（可被 `cyclonedx` 校验器解析）；含算子与依赖组件。
4. **门控**：critical finding → `Validating → Rejected`。
5. **流水线聚合**：`verify_full` 返回各步状态（pass/fail/skipped）结构完整。
6. **CI**：pip-audit 步骤运行（告警不拦）；SBOM 作为 artifact 可下载。
7. **零回归**：现有 `test_operator_factory*`、回放/Fuzz/差分全绿。

## 6. 诚实边界

- **SCA/SBOM 是可选依赖**：缺失时**跳过并标注**，不是"通过了"——报告须区分
  `pass` / `fail` / `skipped`（**skipped ≠ pass**，这是关键诚实点）。
- **DSL 静态扫描是启发式**：能找到可疑模式，**不能证明无漏洞**——它是纵深防御的一层，
  不是保证（动态防线是 P4-1 沙箱）。
- **SBOM 反映"声明的"组件**：未声明的传递依赖可能不全；要完整需锁文件 + 递归解析。
- **CVE 库时效**：SCA 依赖漏洞库更新，离线环境可能过期。

## 7. 依赖

- 与 **P4-1 沙箱**互补（静态 + 动态）；
- SBOM 归档与 **P4-2 控制面**（签名/版本）衔接（SBOM 随签名版本存档）。
