# Agent 的记忆与视觉证据通道（VISTA-1 式）

> 本文记录两条**证据通道**：**跨调查记忆**（把 Agent 自己的历史裁决变成先验）与
> **无损视觉记忆**（把设备相机原始帧变成可检索、可放大的证据）。二者都源自同一篇论文的
> 方法论——"无损记忆 + 主动检索"。

## 0. 出处（只借这一条）

- 论文：**VISTA: A Visual Harness for Reasoning in an Interactive World**，
  Qiushi Han\*, Keya Hu\*, Linlu Qiu\*, Cathy Wu, Kaiming He（MIT），2026-08-05，
  <https://arxiv.org/abs/2610.02200>；主页 <https://vista-research.github.io>；
  代码 <https://github.com/joshhhhhan/VISTA>。
- 核心判断（本文借用的只有这一条）：**编码 / 压缩的那一刻，模型并不知道后面会需要哪些
  细节**——所以把原始观测**无损留存**，让模型**主动检索**、**按需放大**，而不是只喂压缩
  表征或文字摘要。ARC-AGI-3 上它把 Claude Opus 5.0 的相对人类行动效率从 40.68 提到满分。
- **边界**：VISTA 是交互式环境里的"动作-观察闭环"；data_dog 是**只读诊断**，
  Agent 不控制设备相机 / 机器人动作。我们只移植"**无损记忆 + 主动检索**"这一半，
  **不声称复现其基准结果**。

## 1. 跨调查记忆（`query_prior_investigations`）

问题：Agent 每次调查都是"失忆"的——对同一设备 / 部件的历史裁决一无所知；而实测里
真 LLM 的 **normal 误报率 0.72** 正是最痛的项（见 README）。

做法：
- 调查时取该 `(device, component)` 的既往报告（`reports` × `candidates` 关联），连同
  **当时的证据引用**（无损）一起给 Agent，新的在前、最多 5 条。
- **只在有既往记录时**才把工具暴露给模型（`specs()` 按 `supports_prior_memory` 过滤），
  系统提示同步追加"先验 ≠ 本次证据"的边界说明（`PRIOR_MEMORY_PROMPT`）。
- 落点：`tools/investigation.py`、`agent/loop.py`、`service/evidence.py`
  （`RepoEvidenceBridge`）、`service/runtime.py`（`_prior_report_items`）。
- **MVP 边界**：`reports` 行不含 device / component，故现在按 `(device, component)` 取候选
  建立映射，再在租户**最近 200 条报告**里内存过滤；规模化应给 `reports` 加
  `(device_sn, component_ref)` 列 + 索引，直接 SQL 过滤。

## 2. 无损视觉记忆（`query_visual_evidence` / `view_visual_frame`）

做法：
- 帧**按内容寻址**（sha256）落在本地目录：同内容只存一份（无损、天然去重）；裁剪（zoom）
  产出**新帧**并记 `parent_frame_id`，原帧永不修改。
- Agent 先列元数据，再调原帧；要看局部细节就带 `box` 放大。
- 图像以 OpenAI 兼容的"内容分片"（data URL）送多模态模型；**base64 不写进 tool 文本**
  （避免白烧 token），而是作为一条**独立观察**消息注入上下文。
- 调帧要过可信上下文（A4）：跨设备 / 越权部件 → `denied`。
- 落点：`evidence/visual.py`、`providers/client.py`（`LLMChatMessage.images` + `_to_api`）、
  `tools/investigation.py`、`agent/loop.py`（`VISUAL_HARNESS_PROMPT`）、`service/runtime.py`。

### 开关与依赖

| 项 | 值 | 说明 |
|---|---|---|
| `DD_VISUAL_EVIDENCE_DIR` | 空（默认） | 设了才启用视觉通道；空 = 工具不暴露（纯文本链路**零变化**） |
| Pillow | 可选依赖 `.[vision]` | 裁剪需要；缺失时视觉工具不暴露（不报错、不降级） |
| provider | **须多模态** | 纯文本模型（如 `deepseek-chat`）看不到图；开关只决定证据是否可得，不判断模型能力 |

> **为什么默认只接了 DeepSeek 也不影响**：产品落地时模型由使用者自带
> （`DD_LLM_VENDOR` + 自己的 key）。本通道把"图像怎么进模型"做成 provider 无关的
> 通用能力，换多模态 key 即可用，不需要改 Agent 逻辑。

### 诚实边界

- **未做真实相机接入**：帧来自 `VisualFrameStore.put()`；把真机相机（Go2 WebRTC）接进来
  是下一步。当前仓库里**没有任何真实图像数据**，也**没有跑过视觉版评估**——按本仓规矩，
  **不给未实测的准确率数字**。
- **存储是本地目录**：无副本、无生命周期、无跨进程锁；生产应由对象存储承担。
- 与 VISTA 的差别见 §0：无动作回环；不声称复现其基准。

## 3. 第一次实测：**负结果**（2026-10-12，n=3/类，合成渲染图）

用 `scripts/visual_eval.py`：同一批 18 条合成样本、同一 Agent，**只切"有 / 无视觉通道"**。
模型 `deepseek-v4.1`（自定义端点；文本 / 视觉 / 函数调用三项均实测可用）。

| 条件 | n | accuracy | normal 误报率 | 真的调了视觉工具 |
|---|---|---|---|---|
| 关视觉 | 18 | **1.00** | **0.00** | — |
| 开视觉 | 18 | **0.89** | **0.67** | 0.44 |

**结论：这一版视觉通道在合成任务上没帮忙，反而更差**（3 条 normal 里 2 条被误报）。
但**不能**因此说"视觉无用"，只能说到这个口径为止：

- **n=3/类，噪声极大**；且"开视觉"里只有 **44%** 的调查真的调了视觉工具（其余是纯文本），
  两栏**不是干净的对照**。
- 图是**合成遥测渲染**（温度 / 电流曲线 + 阈值线，与文本证据**同源**）——它没有带来文本
  没有的信息，却可能让模型"看着曲线觉得可疑"而**提高灵敏度 → 误报**。
- 换句话说：**把同一批数字画成图，没有增益**。视觉要成立，必须携带**文本拿不到的信息**
  （真实相机的外观 / 形变 / 漏油 / 异物）。

### 下一步（按优先级）
1. 用**真实相机帧**（Go2 WebRTC）或 MuJoCo **渲染的机器人画面**，而不是画曲线。
2. 只取"真的调了视觉工具"的样本做**干净子集对照**（阈值：`visual_tool_call_rate`）。
3. 每类放大到 ≥30 再下结论。

## 4. 跨调查记忆怎么验（同口径）

先在一批上产出**真实**报告，再在**不相交**测试集上开 / 关记忆对照——**不要**把答案喂成
"既往报告"（那是造假）。

> 一句话：**名字不值钱，实测才值钱。** 本仓已经用一次负结果兑现了这句话：视觉通道的
> 首测**没有**写进任何"更准"的说法，而是如实记为"无增益、可能有代价"。
