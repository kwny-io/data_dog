"""Evidence Ref 解析协议（对齐架构 §5 术语表 / §12 / §5「证据优先」原则）。

## 问题（缺口 E-8）

架构把 Evidence Ref 定义为"指向原始日志、指标窗口、文档条目或回放结果的
**权限受控引用**"（§5 术语表）。链路里到处在**产出**它：

  - `CandidateBuilder` 给每条候选追加 `metricwin://{candidate_id}`
    （`data_plane/candidate.py`）
  - `RawLogEnvelope.payload_ref` 是外置正文引用（`data_plane/gateway.py`）
  - 厂商文档条目、回放结果同理

但**没有任何解析器能把 ref 变回可读证据**。Agent / 审计方拿到一个不透明
字符串，变不成内容，也就无法在报告里引用、无法复查。这正是缺口 E-8。

## 本模块提供什么

1. **严格语法**：`scheme://authority(/path...)`，scheme 闭集白名单，拒绝畸形输入。
2. **解析 → 结构化** `ParsedRef`（scheme / authority / path）。
3. **受控解析**：`EvidenceResolver.resolve()` 按 scheme 分派数据源，
   并做**权限校验**（部件白名单）——越权返回 `denied`。
4. **完整性校验**：可选 `expected_digest`，不符返回 `contract_mismatch`（§721）。
5. **有效期**：可选 `issued_at` + ttl，过期返回 `not_found`（§682）。
6. **统一失败语义**：复用 `errors.ToolErrorCode`，不新造错误码。

## 与 `data_plane/fetch.py` 的分工

`fetch.py` 管**外置正文的真实网络取数**（http/https + SSRF 防护）；
本模块管**不透明内部引用的解析与权限**（metricwin/rawlog/vendordoc/replay）。
两者互补：`rawlog://` 这类引用的正文若真由对象存储托管，取数走 fetch，
本模块负责"ref → 该去哪个数据源 + 有没有权限 + 内容是否完整"。

## 安全设计

- **不把 ref 当 URL 去 fetch**。解析只在内存里做字符串→结构，取数走注入的数据源，
  因此不存在 SSRF / 路径穿越面。
- **scheme 用闭集白名单**：不透明引用若放任任意 scheme，解析器就成了安全入口。
- **越权与不存在返回不同错误码**（denied / not_found），但都不回显内部数据结构。
"""
from __future__ import annotations

import hashlib
import json as _json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from ..errors import ToolErrorCode, err, ok
from .store import ALLOWED_COMPONENTS, TELEMETRY

# scheme 闭集（见模块 docstring）
SCHEMES = frozenset({"metricwin", "rawlog", "vendordoc", "replay"})

# 引用语法：scheme://authority(/path...)
# 字符集刻意收紧，让"看着正常但夹带 ../ 或 %2e%2e"的输入在语法层就被挡掉。
# path 段额外允许 ':' 与 '~' —— 真实窗口键形如 "09:55-10:00" / "0955~1000"。
# 之所以安全：本模块**不做任何路径拼接 / 文件访问 / URL fetch**，
# 且 "." 与 ".." 段在 parse_ref 里被显式拒绝（见下）。
_REF_RE = re.compile(
    r"^(?P<scheme>[a-z][a-z0-9+.\-]*)://"
    r"(?P<authority>[A-Za-z0-9._\-]+)"
    r"(?P<path>(?:/[A-Za-z0-9._\-:~]+)*)$"
)


@dataclass(frozen=True)
class ParsedRef:
    """解析后的结构化引用（不可变）。"""
    scheme: str
    authority: str
    path: list[str] = field(default_factory=list)

    @property
    def raw(self) -> str:
        p = "".join(f"/{seg}" for seg in self.path)
        return f"{self.scheme}://{self.authority}{p}"

    def to_dict(self) -> dict[str, Any]:
        return {"scheme": self.scheme, "authority": self.authority,
                "path": list(self.path)}


def parse_ref(raw: str) -> ParsedRef:
    """解析引用字符串。

    Raises:
        ValueError: 语法不合法，或 scheme 不在闭集白名单内。
    """
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("evidence ref 不能为空")
    s = raw.strip()
    m = _REF_RE.match(s)
    if not m:
        raise ValueError(f"evidence ref 语法非法: {raw!r}")
    scheme = m.group("scheme")
    if scheme not in SCHEMES:
        raise ValueError(f"未知 evidence ref scheme: {scheme!r}（闭集白名单内才有）")
    path = [seg for seg in m.group("path").split("/") if seg]
    # 显式拒绝 "." / ".." 段：正则字符集允许点号，但路径穿越语义必须在语义层挡掉。
    # 本模块不做路径拼接，这里再挡一道是为了让"看起来像路径"的输入不被误当成合法引用。
    if any(seg in (".", "..") for seg in path):
        raise ValueError(f"evidence ref 含非法路径段（穿越）: {raw!r}")
    return ParsedRef(scheme=scheme, authority=m.group("authority"), path=path)


def is_valid_ref(raw: str) -> bool:
    """不抛异常的语法校验（供上游过滤脏数据）。"""
    try:
        parse_ref(raw)
        return True
    except ValueError:
        return False


def digest(payload: Any) -> str:
    """内容摘要：稳定 JSON 序列化后 sha256（供完整性校验与调用方比对）。"""
    blob = _json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- #
# 数据源与解析
# --------------------------------------------------------------------------- #

# 数据源回调签名：(authority, path) -> (found: bool, payload: Any)
# found=False → resolve() 收敛为 not_found；抛 PermissionError → denied。
SourceFn = Callable[[str, list[str]], tuple[bool, Any]]


class EvidenceResolver:
    """把 Evidence Ref 解析成可读证据，带权限/完整性/有效期校验。

    Args:
        sources: scheme -> 数据源回调。缺省用内置 mock 源。
        ttl_seconds: 引用有效期（秒）；<=0 表示不校验有效期。
        clock: 注入时钟（秒级 epoch），便于测试过期路径，不依赖真实时间。
    """

    def __init__(self, sources: dict[str, SourceFn] | None = None,
                 *, ttl_seconds: int = 3600,
                 clock: Callable[[], float] | None = None):
        # 注意：用 `is not None` 而非 `or`——传入空 dict 是"显式不要任何源"，
        # 应返回 unavailable，而不是悄悄回退到内置源。
        self._sources: dict[str, SourceFn] = sources if sources is not None else {
            "metricwin": _metricwin_source,
            "rawlog": _rawlog_source,
            "vendordoc": _vendordoc_source,
            "replay": _replay_source,
        }
        self.ttl_seconds = ttl_seconds
        self._clock = clock or (lambda: 0.0)

    def resolve(self, raw: str, *, issued_at: float | None = None,
                expected_digest: str | None = None) -> dict[str, Any]:
        """解析单个引用，返回统一成功/失败结构。

        Args:
            raw: 引用字符串。
            issued_at: 引用签发时间（epoch 秒）；给了且超 ttl → not_found。
            expected_digest: 期望内容摘要；给了且不符 → contract_mismatch。

        Returns:
            ok:True + {ref, source, payload}，或 err(code, detail)。
        """
        try:
            ref = parse_ref(raw)
        except ValueError as e:
            return err(ToolErrorCode.INVALID_INPUT, str(e))

        # ---- 有效期（失败关闭：过期视同不存在）----
        if issued_at is not None and self.ttl_seconds > 0:
            if self._clock() - float(issued_at) > self.ttl_seconds:
                return err(ToolErrorCode.NOT_FOUND, f"引用已过期: {raw}")

        fn = self._sources.get(ref.scheme)
        if fn is None:
            return err(ToolErrorCode.UNAVAILABLE, f"无匹配数据源: {ref.scheme}")

        try:
            found, payload = fn(ref.authority, ref.path)
        except PermissionError:
            return err(ToolErrorCode.DENIED, raw)          # 权限不足
        except Exception as e:                              # 数据源异常 → 兜底
            return err(ToolErrorCode.EXECUTION_FAILED, f"{type(e).__name__}: {e}")

        if not found:
            return err(ToolErrorCode.NOT_FOUND, raw)

        # ---- 完整性校验（架构 §721）----
        if expected_digest is not None and digest(payload) != expected_digest:
            return err(ToolErrorCode.CONTRACT_MISMATCH,
                       f"摘要不符: 期望 {expected_digest}，实际 {digest(payload)}")

        return ok({"ref": ref.to_dict(), "source": ref.scheme, "payload": payload})

    def resolve_all(self, refs: list[str], **kw: Any) -> dict[str, Any]:
        """批量解析：保序，逐条给结果（一条失败不影响其他条）。"""
        out = [self.resolve(r, **kw) for r in refs]
        return ok({"results": out,
                   "resolved": sum(1 for r in out if r.get("ok")),
                   "total": len(refs)})


# --------------------------------------------------------------------------- #
# 内置 mock 数据源（可插拔：真实部署替换这些函数即可，解析/校验逻辑不变）
# --------------------------------------------------------------------------- #

def _metricwin_source(authority: str, path: list[str]) -> tuple[bool, Any]:
    """`metricwin://<candidate_id>` —— 候选指标窗口证据。

    authority 是**候选 id**（与 `CandidateBuilder` 产出的真实格式一致）。
    mock 环境没有候选索引，这里从候选 id 无法反查部件，因此返回
    一个**结构化占位**并标注 not_indexed，而不是编造指标。

    真实部署应注入一个能按 candidate_id 查回 (component, window, metrics) 的源。
    """
    if not authority.startswith("cand_"):
        return False, None
    return True, {"candidate_id": authority,
                  "indexed": False,
                  "note": "mock 源未建立候选索引；接真实 Candidate Store 后返回指标窗口"}


def _rawlog_source(authority: str, path: list[str]) -> tuple[bool, Any]:
    """`rawlog://<event_id>` —— 原始日志正文（外置）最小摘要。"""
    known = {"opaque-ref", "e1", "e2", "e3", "e4"}
    if authority not in known:
        return False, None
    return True, {"event_id": authority, "digest": "sha256:mock",
                  "note": "原始日志正文外置（mock 摘要）"}


def _vendordoc_source(authority: str, path: list[str]) -> tuple[bool, Any]:
    """`vendordoc://<doc_id>` —— 厂商文档条目。"""
    docs = {
        "motor-x2-thermal": {
            "title": "Motor X2 热保护说明",
            "fact": {"key": "motor_temp_protect", "value_c": 90,
                     "status": "verified", "source": "unitree_actuator_sdk"},
            "unconfirmed": {"key": "motor_temp_warning", "value_c": 75,
                            "status": "unconfirmed", "source": ""},
        },
    }
    if authority not in docs:
        return False, None
    return True, docs[authority]


def _replay_source(authority: str, path: list[str]) -> tuple[bool, Any]:
    """`replay://<run_id>` —— 回放结果摘要。"""
    if authority != "latest":
        return False, None
    return True, {"run_id": "latest", "status": "passed",
                  "note": "E2E 回放结果摘要（mock）"}


def metricwin_ref_for(component: str, window: str) -> str:
    """构造指向**真实遥测窗口**的 metricwin 引用（供测试/演示用）。

    window 用 TELEMETRY 的真实键格式，如 "09:55-10:00"。

    注意与 `CandidateBuilder` 产出的 `metricwin://<candidate_id>` 形式不同：
    那条是"候选级"引用，这条是"窗口级"引用，authority 为部件。
    两者语义不同，是当前设计的已知不一致，记录在交付日志里，供后续统一。
    """
    return f"metricwin://{component}/{window}"


def default_resolver(**kw: Any) -> EvidenceResolver:
    """单机默认解析器（内置 mock 源）。"""
    return EvidenceResolver(**kw)


# 供测试/演示：把遥测窗口查询能力暴露成一个可注入的源，
# 便于演示"越权 → denied"而非只能拿到占位。
def make_telemetry_source(telemetry: dict[str, Any] | None = None) -> SourceFn:
    """构造一个按 `metricwin://<component>/<start>~<end>` 取真实遥测的源。

    部件不在白名单 → PermissionError（→ denied）；窗口无数据 → not_found。
    """
    data = telemetry if telemetry is not None else TELEMETRY

    def _src(authority: str, path: list[str]) -> tuple[bool, Any]:
        component = authority
        if component not in ALLOWED_COMPONENTS:
            raise PermissionError(component)
        window = path[0].replace("~", "-") if path else ""
        metrics = data.get(component, {}).get(window)
        if metrics is None:
            return False, None
        return True, {"component": component, "window": window, "metrics": metrics}

    return _src


def make_metricwin_source(telemetry: dict[str, Any] | None = None) -> SourceFn:
    """别名：语义上更贴近 scheme 名的构造器（供测试/演示注入）。

    与 `make_telemetry_source` 等价，保留两个名字是为了让调用点读起来
    与它解析的 scheme（metricwin）一致。
    """
    return make_telemetry_source(telemetry)
