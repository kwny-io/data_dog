"""受控调查 Tool：Agent 唯一能访问的外部世界入口（对齐架构 13.2）。

所有 Tool 都是只读、带权限校验（仅允许白名单部件）、返回最小证据，
不让 Agent 拿到全量原始日志、密钥或越低权直接副作用。
"""
from __future__ import annotations

from typing import Any

from ..context import RuntimeContext, default_context
from ..errors import ToolErrorCode, err, ok
from ..evidence.store import EvidenceSource


class InvestigationTools:
    """把 EvidenceStore 包装成 Agent 能调用的命名 Tool。

    A4 注入：工具绑定一个可信 RuntimeContext。Agent（调用方）传的 device_sn /
    component 只是"请求"，真正生效的访问范围以 ctx 为准——在 call() 里被注入 /
    校验 / 收紧，越权返回 denied。Agent 无法通过改参数越权。
    """

    def __init__(self, store: EvidenceSource, ctx: RuntimeContext | None = None):
        self.store = store
        self.ctx = ctx or default_context()

    def query_candidate_evidence(
        self, device_sn: str, component: str, window_start: str, window_end: str
    ) -> dict[str, Any]:
        """查同部件在一个时间窗内的遥测与参照。"""
        res = self.store.query_telemetry(
            device_sn, component, window_start, window_end
        )
        if res is None:
            return err(ToolErrorCode.NOT_FOUND, "该窗口无数据")
        if isinstance(res, dict) and res.get("ok") is False:
            return res  # 已统一错误（如 denied）
        return ok(res)

    def query_vendor_knowledge(
        self, component_type: str, question: str = ""
    ) -> dict[str, Any]:
        """查询与当前组件型号绑定的厂商知识条目。"""
        items = self.store.query_vendor_knowledge(component_type, question)
        return ok({"component_type": component_type, "items": items})

    def query_neighbor_events(
        self, device_sn: str, component: str, window_start: str, window_end: str
    ) -> dict[str, Any]:
        """查同设备其他部件在相同时窗内是否有异常（支持关联）。"""
        evs = self.store.query_neighbor_events(
            device_sn, component, window_start, window_end
        )
        return ok({"device_sn": device_sn, "events": evs})

    def query_recent_changes(
        self, device_sn: str, component: str, since: str
    ) -> dict[str, Any]:
        """查某部件近期固件/算子/配置变更（排查"变更新引入异常"）。"""
        res = self.store.query_recent_changes(device_sn, component, since)
        if isinstance(res, dict) and res.get("ok") is False:
            return res  # 已统一错误（如 denied）
        return ok({"device_sn": device_sn, "component": component,
                   "since": since, "changes": res.get("changes", [])})

    # ---- 工具描述，供模型 / 生成 tool 定义用 ----#
    TOOL_SPECS: dict[str, dict[str, Any]] = {
        "query_candidate_evidence": {
            "name": "query_candidate_evidence",
            "description": (
                "查询某个部件在指定时间窗内的标准化遥测指标，"
                "以及同部件更早一窗的参照值，用于判断趋势。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "device_sn": {"type": "string", "description": "设备序列号"},
                    "component": {"type": "string", "description": "部件标识，如 joint_motor.front_left_hip"},
                    "window_start": {"type": "string", "description": "窗口开始，如 09:55"},
                    "window_end": {"type": "string", "description": "窗口结束，如 10:00"},
                },
                "required": ["device_sn", "component", "window_start", "window_end"],
            },
        },
        "query_vendor_knowledge": {
            "name": "query_vendor_knowledge",
            "description": "查询与指定组件类型绑定的厂商知识条目（阈值、原因、安全限制）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "component_type": {"type": "string", "description": "组件类型，如 joint_motor"},
                    "question": {"type": "string", "description": "要查的问题（可选）"},
                },
                "required": ["component_type"],
            },
        },
        "query_neighbor_events": {
            "name": "query_neighbor_events",
            "description": "查询同设备其他部件在同一时间窗内是否有异常，用于多事件关联。",
            "parameters": {
                "type": "object",
                "properties": {
                    "device_sn": {"type": "string"},
                    "component": {"type": "string"},
                    "window_start": {"type": "string"},
                    "window_end": {"type": "string"},
                },
                "required": ["device_sn", "component", "window_start", "window_end"],
            },
        },
        "query_recent_changes": {
            "name": "query_recent_changes",
            "description": "查询某部件近期固件/算子/配置变更，用于排查异常是否由变更新引入（对 13.1 调查流程第4步）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "device_sn": {"type": "string"},
                    "component": {"type": "string", "description": "部件标识，如 joint_motor.front_left_hip"},
                    "since": {"type": "string", "description": "回溯时间起点，如 2026-08-10"},
                },
                "required": ["device_sn", "component", "since"],
            },
        },
    }

    def specs(self) -> list:
        """返回给模型的工具定义列表（供 Provider.chat 用）。"""
        return list(self.TOOL_SPECS.values())

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        """按名字+参数调用某个 Tool（AgentLoop 用）。

        A4 注入点：
        - device_sn 以 ctx.device_sn 为准，忽略调用方传的（防跨设备）。
        - component 用 ctx 授权范围校验，越权 → denied，不真正执行。
        - 注：不做 "如果调用方带 tenant 之类字段就直接执行" —— 工具不接受这些。
        """
        # 注入可信 device：任何工具请求都强制为该上下文设备
        if "device_sn" in args:
            args["device_sn"] = self.ctx.device_sn
        # 组件越权校验：需要 component 的工具都必须在授权范围内
        comp = args.get("component")
        if comp is not None and not self.ctx.can_access(self.ctx.device_sn, comp):
            return err(ToolErrorCode.DENIED, comp)

        fn = getattr(self, name, None)
        if fn is None:
            return err(ToolErrorCode.EXECUTION_FAILED, f"unknown_tool: {name}")
        try:
            return fn(**args)
        except TypeError as e:
            return err(ToolErrorCode.INVALID_INPUT, str(e))

    def with_task(self, device_sn: str, allowed_components=None) -> InvestigationTools:
        """绑定到一次具体任务的可信上下文（设备+授权范围），返回新实例。"""
        new_ctx = RuntimeContext(
            tenant_id=self.ctx.tenant_id,
            agent_id=self.ctx.agent_id,
            device_sn=device_sn,
            allowed_components=(
                frozenset(allowed_components)
                if allowed_components is not None
                else self.ctx.allowed_components
            ),
        )
        return InvestigationTools(self.store, new_ctx)

    def with_context(self, ctx: RuntimeContext) -> InvestigationTools:
        """绑定一个完整的外部可信上下文（如来自 context-injector MCP）。"""
        return InvestigationTools(self.store, ctx)
