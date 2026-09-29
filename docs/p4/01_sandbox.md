# P4-1 算子沙箱运行时

## 1. 目标

让「厂商字符串变不成可执行代码」这个安全故事**成真到运行时层**：即使受限 DSL 的解释器
存在漏洞，**被绕过的算子也不伤宿主**——不读宿主文件、不联网、不耗尽 CPU/内存、不挂死进程。

## 2. 现状（已核实）

| 已有 | 位置 | 说明 |
|---|---|---|
| 自建解释器 | `operator_factory/dsl.py` | `tokenize` → `Parser` → `Evaluator`；函数白名单闭集；编译期拒未知名；无 `exec/eval` |
| 同进程执行 | `operator_factory/pipeline.py` `Operator.run()` | **直接调用解释器，无任何隔离** |
| 编译期类型检查 | `dsl.py` | 布尔参与算术等编译期拒绝 |

**缺**：进程/子进程隔离、WASM、资源上限（timeout / step 预算 / 内存上限）、seccomp/容器。
（`p1_demo.py` 里仅有 "sandbox" 字样的文案，无实现。）

## 3. 设计：两级纵深防御

```
Operator.run(ctx)
  → ExecutorPool 取一个 Executor
      ├─ InlineExecutor（默认，单进程）  ←──── Tier 0：仍加 step/time 预算（最便宜）
      └─ SubprocessExecutor（隔离）      ←──── Tier 1：进程 + 资源上限 + 无网络
                                                   │
                                          （可选）WasmExecutor ← Tier 2：wasmtime 承载纯计算
```

**关键判断**：先做 **Tier 0（step 预算 + 墙钟超时）**——它几乎零成本，挡住 99% 的
"写死循环 / 大数组" 事故。Tier 1 子进程隔离是真正的安全边界。WASM（Tier 2）是
**可选加强**，不作为验收前提（避免引入重依赖拖慢交付）。

### 3.1 接口

```python
# operator_factory/sandbox.py
@dataclass(frozen=True)
class SandboxPolicy:
    wall_s: float = 0.05          # 单算子墙钟上限（检测在热路径，须极短）
    max_steps: int = 100_000      # 解释器步数预算（Tier 0，防超长表达式）
    mem_mb: int = 64              # Tier 1：子进程地址空间上限
    cpu_s: float = 0.1            # Tier 1：CPU 时间 rlimit
    allow_net: bool = False       # Tier 1：默认禁网
    allow_fs: bool = False        # Tier 1：默认禁文件系统写

class ExecutorPool:
    """按策略复用执行器；SubprocessExecutor 内部是常驻 worker（避免每算子 fork 开销）。"""
    def __init__(self, policy: SandboxPolicy, kind: str = "inline") -> None: ...
    def run(self, op: Operator, ctx: OperatorContext) -> OperatorResult: ...
    def close(self) -> None: ...

class SandboxViolation(RuntimeError):
    """违反策略（超时/超步/超内存/越权）。归类为统一错误码 invalid_input。"""
```

### 3.2 Tier 0：解释器内置预算（改 `dsl.py`）

在 `Evaluator` 主循环加：
- `self._steps` 计数，超过 `max_steps` → 抛 `SandboxViolation`；
- 每个窗口函数（`window.*`）调用前检查一次（防单个大聚合）；
- **不改**语义与现有测试（预算默认足够大）。

### 3.3 Tier 1：子进程执行器（新 `operator_factory/sandbox_exec.py`）

- 常驻 worker 进程（`multiprocessing`，Windows 用 `spawn` + 可 pickle 的入参）；
- worker 启动时设 `resource.setrlimit`（Linux/macOS）：`RLIMIT_CPU`、`RLIMIT_AS=mem_mb`、
  `RLIMIT_NOFILE=16`、`RLIMIT_NPROC=0`；
- Linux：`seccomp` 禁 `socket/connect/open/execve`（可用 `pyseccomp`，**可选依赖**）；
- 父进程 `join(wall_s)` 超时 → `terminate()` → 记 `SandboxViolation`；
- 越权/超时**只丢弃该算子的该次求值**（失败关闭），不中断整条流。

### 3.4 Tier 2（可选，`.[wasm]`）：WASM

把 DSL AST 编成极小栈机字节码，用 `wasmtime` 执行，宿主只暴露纯函数 import（无文件/网络）。
**仅当** Tier 1 在目标平台不可用（如无法设 rlimit 的环境）时才需要。设计预留
`WasmExecutor`，接口同 `ExecutorPool`。

## 4. 要建/要改的文件

| 文件 | 动作 |
|---|---|
| `src/data_dog/operator_factory/sandbox.py` | **新建**：`SandboxPolicy` / `ExecutorPool` / `SandboxViolation` |
| `src/data_dog/operator_factory/sandbox_exec.py` | **新建**：子进程 worker 入口（可被 spawn 导入） |
| `src/data_dog/operator_factory/dsl.py` | 改：`Evaluator` 加 `max_steps` 预算 |
| `src/data_dog/operator_factory/pipeline.py` | 改：`Operator.run()` 走 `ExecutorPool`（默认 inline） |
| `src/data_dog/settings.py` | 加：`DD_SANDBOX_KIND`(inline/subprocess/wasm)、`DD_SANDBOX_WALL_S` 等 |
| `pyproject.toml` | 加可选依赖 `wasm = ["wasmtime"]`、`seccomp = ["pyseccomp"]` |
| `tests/test_sandbox.py` | **新建** |

## 5. 验收

1. **step 预算**：构造超长表达式算子 → `SandboxViolation`，且**宿主未被拖慢**（< 200ms 返回）。
2. **墙钟超时**：构造死循环等价算子（如超大 `window.sum` 输入）→ 按期终止。
3. **内存上限**（Tier 1）：分配超 `mem_mb` → 子进程被杀，父进程存活。
4. **无网络/文件**（Tier 1，Linux）：算子尝试 `open()/socket()` → 拒绝。
5. **零回归**：现有 `test_operator_factory*` 全绿（默认 inline + 大预算，语义不变）。
6. **隔离性**：一个算子崩溃（段错误模拟）不导致服务进程挂掉。

## 6. 诚实边界

- **Windows 无 seccomp** → 网络/文件隔离降级为「进程 + rlimit 近似 + best-effort」，
  文档与日志写明当前档位。生产建议 Linux 容器。
- **WASM 不默认开**：它是加强项，不是验收前提。
- **子进程有 IPC 开销**：热路径默认仍 inline；subprocess 用于**不可信/待审**的算子，
  或对隔离有硬要求的部署。

## 7. 与其他 P4 的关系

- **P4-2 控制面**依赖本项：签名验过的算子，才由沙箱执行——「验签 + 沙箱」是完整信任链。
- 与「缺口-8 验证流水线（编译扫描）」互补：扫描是静态防线，沙箱是动态防线。
