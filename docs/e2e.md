# E2E 整链回放（架构 §19.3 验收路径）

> 把已实现的四段用**一条命令**串起来，证明从厂商文档到 SaaS 事件的整链贯通。
> 全程**离线、确定性**（Agent 用确定性 Provider，不调 LLM），结果可复现。

---

## 1. 一句话

```
厂商文档 →[P1 算子工厂]→ 算子 →[P2 数据面]→ 候选 →[Agent]→ 报告 →[P3]→ 策略门 → SaaS → 反馈
```

```bash
python -m data_dog.e2e.replay_demo          # 人读输出
python -m data_dog.e2e.replay_demo --json   # 机器可读
pytest tests/test_e2e.py -q                 # 5 项
```

实测输出：
```
[P1] 算子 local.joint_motor-motorstate-detector（7 字段 / 2 规则）
[P2] 收到 4 条 → 绑定 3 → 失败关闭 1 → 候选 1
       ▸ cand_b654abdb3269 [critical] joint_motor.front_left_hip
[Agent] ▸ inv_...: confirmed (conf 0.90)  温度峰值 98.0°C，判为明确热故障
[P3] ▸ ✅ 上报 severity=critical  命中: 条件①严重码; 条件③confirmed+置信度
     SaaS 已接收 1 条正式事件；人工反馈 1 条
整链贯通 ✅
```

---

## 2. 模块

| 文件 | 职责 |
|---|---|
| `bridge.py` | 证据桥：把 P2 遥测接成 Agent 查询接口（与 `EvidenceStore` 同形，Agent 不改） |
| `replay.py` | 编排 P1→P2→Agent→P3；`_CandidateAwareProvider` 按候选设备/部件查证据 |
| `replay_demo.py` | 演示入口 |

---

## 3. 关键设计

- **换数据源不改 Agent（第三次兑现）**：桥接证据源实现与 `EvidenceStore` 相同接口——P1"换算子不改 Agent"、MuJoCo"换数据源不改 Agent"、这里"整链接进 Agent 也不改"。
- **确定性**：Agent 用 `_CandidateAwareProvider`（物理阈值驱动），同输入两次回放裁决一致（`test_e2e_deterministic`）。
- **失败关闭贯穿**：P2 固件不兼容那条日志被拒；桥接源拒绝跨设备/未知部件查询。

---

## 4. 回放中发现并修复的真实 bug（值得记）

**现象**：首轮 E2E，候选是 98°C critical，但 Agent 只判 `needs_human_review` 置信度 0.2。
**根因**：上游 `MujocoDeterministicProvider` **写死**去查 `joint_motor.front_left_knee` + `device-synth`，而 P2 产生的候选是 `front_left_hip` + `device-go2-0001`——工具返回 `not_found`，证据缺失 → 裁决退化。
**修复**：E2E 用 `_CandidateAwareProvider`，**从候选实际设备/部件推导查询参数**，保证整链数据对齐。
**教训**：**跨模块拼接时，硬编码的"示例常量"是头号杀手**——单测各自绿，一拼就错。E2E 回放的价值正在于此：它专抓"各段都对、合起来不对"。修复后 98°C → confirmed(0.9) → 上报 critical。

---

## 5. 与架构条目对照

| 架构 | 状态 |
|---|---|
| §19.3 真实回放验收路径（文档→…→SaaS→反馈） | ✅ 本地确定性回放贯通 |
| §6.5 反馈闭环 | 🟡 Adapter 已收反馈；回灌到 Agent/评估待接 |
| 真实脱敏 Fixture 回放（§19.3 要求"只用合成数据不能上线验收"） | ❌ 待真实硬件 Fixture |
