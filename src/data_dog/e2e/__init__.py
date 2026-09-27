"""e2e —— 端到端整链回放（对齐架构 §19.3 验收路径）。

把已实现的四段用**一条命令**串起来，证明从厂商文档到 SaaS 事件整链贯通：

    厂商文档 →[P1 算子工厂]→ 算子
             →[P2 在线数据面]→ 候选异常(CandidateEvent)
             →[Agent 受控调查]→ InvestigationReport
             →[P3 上报闭环]→ 策略门 → 正式事件 → SaaS Adapter → 反馈

子模块：
  bridge   证据桥：把 P2 处理链的样本接成 Agent 能查的证据源
  replay   编排：跑完 P1→P2→Agent→P3，返回可核对的中间产物
"""
