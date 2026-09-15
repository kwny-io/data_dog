# -*- coding: utf-8 -*-
"""alerting —— 策略门 + 正式事件生命周期 + 上报闭环（P3）。

对齐架构 §14（候选事件关联与正式上报）+ §16（可靠性）+ §12.5（SaaS AlertEvent）：

    InvestigationReport ──► ReportPolicyGate（§14.2 确定性决策）
                              │ 通过 → AlertEvent 创建/更新
                              ▼
                        AlertLifecycle（§14.3 状态机）
                              │
                              ▼
                        AlertOutbox（幂等投递 SaaS，§16.1）
                              │
                              ▼
                        SaaSTestAdapter（接收/确认/反馈）

子模块：
  policy     确定性 Report Policy 决策表（含冲突消解/优先级）
  lifecycle  Alert 生命周期状态机（Detected→…→Closed）
  outbox     幂等 outbox + 幂等键构成规则
  saas       测试 SaaS Adapter（接收正式事件、返回确认与反馈）
  service    端到端编排：调查结论 → 上报决策 → 事件 → 投递
"""
