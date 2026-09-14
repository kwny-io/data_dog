# -*- coding: utf-8 -*-
"""data_plane —— 在线数据面（P2）。

把"源源不断的原始日志"变成"少量、结构化、带证据的候选异常"，热路径不碰大模型。
对齐架构 §9（日志到正式异常的高频在线流程）：

    Telemetry Gateway（§9.1）
      → Compatibility Resolver（§9.2）
      → Parser → Normalizer → Enricher（§9.3）
      → Window/Feature → Detector（执行 P1 生成的算子）
      → Candidate Builder（§9.3 第 6 步）

子模块：
  events     标准事件模型（RawLogEnvelope / NormalizedTelemetryEvent）
  gateway    接入网关：身份注入、前置校验、分区、限流（失败关闭）
  resolver   兼容性绑定：按 vendor/model/firmware/protocol 选算子，拿不准失败关闭
  chain      处理链：解析/规范化/增强/窗口/检测
  candidate  候选构建：合并、去重、证据引用
  fetch      受控引用解析（仅 http/https，拒绝内网/环回/保留地址）
"""
