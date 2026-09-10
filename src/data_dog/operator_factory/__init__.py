# -*- coding: utf-8 -*-
"""operator_factory —— 离线算子工厂（P1）。

把"厂商文档 → 结构化知识 → 声明式/DSL 算子 → 校验/回放"做成一条可跑、可测的链路。
子模块：
  dsl         受限 DSL 的安全求值器（P0-3 规范的落地子集）
  knowledge   文档 Revision 管理 + 带来源/页码的 VendorKnowledgePackage 抽取
  distill     从文档抽取阈值/字段规格（**只抽文档真有的**，抽不到就标待确认，不编造）
  pipeline    组装算子 + Fixture 回放验证
"""
