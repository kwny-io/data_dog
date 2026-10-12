"""受控调查 Tool：Agent 唯一能访问的外部世界入口（对齐架构 13.2）。

所有 Tool 都是只读、带权限校验（仅允许白名单部件）、返回最小证据，
不让 Agent 拿到全量原始日志、密钥或越低权直接副作用。
"""
from __future__ import annotations

import base64
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

    def __init__(self, store: EvidenceSource, ctx: RuntimeContext | None = None,
                 visual: Any | None = None):
        self.store = store
        self.ctx = ctx or default_context()
        # 无损视觉记忆（VISTA-1 式；可选）。None = 不暴露视觉工具（纯文本链路零回归）。
        self.visual = visual

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
        out = {"device_sn": device_sn, "component": component,
               "since": since, "changes": res.get("changes", [])}
        if res.get("source"):
            out["source"] = res["source"]      # G7：标注变更数据来源，便于追责/排查
        return ok(out)

    def query_causal_chain(
        self, device_sn: str, component: str, window_start: str, window_end: str
    ) -> dict[str, Any]:
        """查该部件的疑似因果链（G6：跨部件、带方向与滞后）。

        与 `query_neighbor_events`（同窗共现）互补：这里给出"先跌的是 X，滞后 t 秒
        Y 跟着异常"的**有序**关联。证据源若不实现该方法（只实现四查询的最小
        Protocol）则返回空链——不冒充有数据。
        """
        fn = getattr(self.store, "query_causal_chain", None)
        if fn is None:
            return ok({"device_sn": device_sn, "component": component,
                       "window": f"{window_start}-{window_end}",
                       "causal_chains": [], "truncated": False,
                       "note": "该证据源未提供因果链"})
        res = fn(device_sn, component, window_start, window_end)
        if isinstance(res, dict) and res.get("ok") is False:
            return res  # 已统一错误（如 denied）
        return ok(res)

    def query_prior_investigations(
        self, device_sn: str, component: str, signal: str = ""
    ) -> dict[str, Any]:
        """查该部件**历史上被调查过的结论**（跨调查记忆）。

        用途是"先验"，不是本次证据：Agent 仍须以当前遥测/知识为准；当前证据与既往
        相反时判当前。证据源未提供该能力时返回空列表并**显式说明**（不冒充有）。
        """
        fn = getattr(self.store, "query_prior_investigations", None)
        if fn is None:
            return ok({"device_sn": device_sn, "component": component,
                       "signal": signal, "prior_investigations": [],
                       "truncated": False,
                       "note": "该证据源未提供跨调查记忆"})
        res = fn(device_sn, component, signal)
        if isinstance(res, dict) and res.get("ok") is False:
            return res  # 已统一错误（如 denied）
        return ok(res)

    def query_visual_evidence(
        self, device_sn: str, component: str, window_start: str, window_end: str
    ) -> dict[str, Any]:
        """列出该部件在时间窗内**无损留存**的相机原始帧（只返回元数据）。

        拿到 frame_id 后用 view_visual_frame 调取原帧；需要看局部细节时带 box 放大。
        """
        if self.visual is None:
            return ok({"frames": [], "note": "未启用视觉证据通道"})
        frames = self.visual.list_frames(
            device_sn=device_sn, component_ref=component,
            window_start=window_start, window_end=window_end, limit=20)
        return ok({"device_sn": device_sn, "component": component,
                   "window": f"{window_start}-{window_end}", "frames": frames,
                   "note": "仅元数据；用 view_visual_frame 调取某帧，可带 box='l,t,r,b' 放大局部。"})

    def view_visual_frame(self, frame_id: str, box: str = "") -> dict[str, Any]:
        """调取一帧原始图像（base64 内联给多模态模型）；box='l,t,r,b' 时先裁剪放大。

        返回里的 __images__ 会被 AgentLoop 转成一条带图的消息，并从 tool 文本里摘掉
        （避免 base64 白烧 token）。原帧永不被修改，裁剪产出新帧。
        """
        if self.visual is None:
            return ok({"frame_id": frame_id, "note": "未启用视觉证据通道"})
        if box:
            try:
                nums = [int(x) for x in str(box).replace(" ", "").split(",")]
            except ValueError:
                return err(ToolErrorCode.INVALID_INPUT, f"box 需为 'l,t,r,b'：{box}")
            if len(nums) != 4:
                return err(ToolErrorCode.INVALID_INPUT, f"box 需 4 个整数：{box}")
            meta = self.visual.crop(frame_id, nums)
        else:
            meta = self.visual.meta(frame_id)
        if not meta:
            return err(ToolErrorCode.NOT_FOUND, f"无此帧或无法裁剪：{frame_id}")
        # A4：帧也要过可信上下文——否则 Agent 能拿别设备/越权部件的帧（存储是全局的）。
        if meta.get("device_sn") and meta["device_sn"] != self.ctx.device_sn:
            return err(ToolErrorCode.DENIED, "帧不属于当前设备")
        comp = meta.get("component_ref", "")
        if comp and not self.ctx.can_access(self.ctx.device_sn, comp):
            return err(ToolErrorCode.DENIED, comp)
        raw = self.visual.get_bytes(meta["frame_id"])
        if raw is None:
            return err(ToolErrorCode.NOT_FOUND, f"帧文件缺失：{frame_id}")
        payload = dict(meta)
        payload["__images__"] = [{
            "mime_type": meta.get("mime_type", "image/png"),
            "data_base64": base64.b64encode(raw).decode("ascii"),
        }]
        return ok(payload)

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
        "query_causal_chain": {
            "name": "query_causal_chain",
            "description": (
                "查询某部件在时间窗内的跨部件疑似因果链（带方向与滞后）："
                "谁先异常、谁随后跟随，沿供电/信号层级。用于区分'因'与'果'，"
                "比同窗共现更进一步。返回的是工程启发式结果，非确定因果。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "device_sn": {"type": "string"},
                    "component": {"type": "string", "description": "部件标识，如 joint_motor.front_left_hip"},
                    "window_start": {"type": "string", "description": "窗口开始，如 09:55"},
                    "window_end": {"type": "string", "description": "窗口结束，如 10:00"},
                },
                "required": ["device_sn", "component", "window_start", "window_end"],
            },
        },
        "query_prior_investigations": {
            "name": "query_prior_investigations",
            "description": (
                "查询该部件历史上被调查过的结论（跨调查记忆）：既往裁决 / 置信度 / "
                "摘要 / 证据引用。这是**先验，不是本次证据**——可能过时或工况不同，"
                "必须与本次遥测/知识交叉验证后再裁决，不得直接复制既往结论。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "device_sn": {"type": "string"},
                    "component": {"type": "string", "description": "部件标识，如 joint_motor.front_left_hip"},
                    "signal": {"type": "string", "description": "可选：只看同类信号（如 temperature_high）"},
                },
                "required": ["device_sn", "component"],
            },
        },
        "query_visual_evidence": {
            "name": "query_visual_evidence",
            "description": (
                "列出该部件在时间窗内**无损留存**的设备相机原始帧（只返回元数据：frame_id / "
                "时间 / 尺寸 / 标签）。需要用 view_visual_frame 调取原帧。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "device_sn": {"type": "string"},
                    "component": {"type": "string", "description": "部件标识，如 joint_motor.front_left_hip"},
                    "window_start": {"type": "string"},
                    "window_end": {"type": "string"},
                },
                "required": ["device_sn", "component", "window_start", "window_end"],
            },
        },
        "view_visual_frame": {
            "name": "view_visual_frame",
            "description": (
                "调取一帧原始图像直接给模型看（不做文字描述/压缩）；box='l,t,r,b'（像素）时"
                "先裁剪放大局部。原则：需要细节就调原帧再放大。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "frame_id": {"type": "string", "description": "query_visual_evidence 返回的 frame_id"},
                    "box": {"type": "string", "description": "可选：'left,top,right,bottom' 像素框，用于放大局部"},
                },
                "required": ["frame_id"],
            },
        },
    }

    def specs(self) -> list:
        """返回给模型的工具定义列表（供 Provider.chat 用）。

        query_prior_investigations **只在证据源确实提供跨调查记忆时**才暴露：
        没有既往记录的设备不该多给模型一个只会返回空的工具（白耗一步，还可能
        诱导模型编造"以前查过"）。
        """
        specs = [dict(s) for s in self.TOOL_SPECS.values()]
        if not getattr(self.store, "supports_prior_memory", False):
            specs = [s for s in specs if s["name"] != "query_prior_investigations"]
        # 视觉工具只在**确实挂了无损视觉记忆且 Pillow 可用**时暴露；否则纯文本链路不变。
        if self.visual is None or not getattr(self.visual, "available", False):
            specs = [s for s in specs
                     if s["name"] not in ("query_visual_evidence", "view_visual_frame")]
        return specs

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
        return InvestigationTools(self.store, new_ctx, visual=self.visual)

    def with_context(self, ctx: RuntimeContext) -> InvestigationTools:
        """绑定一个完整的外部可信上下文（如来自 context-injector MCP）。"""
        return InvestigationTools(self.store, ctx, visual=self.visual)
