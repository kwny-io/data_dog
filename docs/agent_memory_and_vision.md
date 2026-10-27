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
- 落点：`src/data_dog/tools/investigation.py`、`src/data_dog/agent/loop.py`、`src/data_dog/service/evidence.py`
  （`RepoEvidenceBridge`）、`src/data_dog/service/runtime.py`（`_prior_report_items`）。
- **MVP 边界**：`reports` 行不含 device / component，故现在按 `(device, component)` 取候选
  建立映射，再在租户**最近 200 条报告**里内存过滤；规模化应给 `reports` 加
  `(device_sn, component_ref)` 列 + 索引，直接 SQL 过滤。
- **记忆不绑模型 / key（产品口径）**：报告落在项目自己的 DB（`reports` 表），检索走
  `_prior_report_items`，**完全不经过 provider**——换模型 / 换 key / 换厂商都不丢记忆，
  新接入方**直接继承**同一部署里已积累的裁决；视觉帧同理（内容寻址目录）。
  **按租户隔离**：换 key 不会读到别的租户的记忆（`tests/test_prior_memory_persistence.py`）。

## 2. 无损视觉记忆（`query_visual_evidence` / `view_visual_frame`）

做法：
- 帧**按内容寻址**（sha256）落在本地目录：同内容只存一份（无损、天然去重）；裁剪（zoom）
  产出**新帧**并记 `parent_frame_id`，原帧永不修改。
- Agent 先列元数据，再调原帧；要看局部细节就带 `box` 放大。
- 图像以 OpenAI 兼容的"内容分片"（data URL）送多模态模型；**base64 不写进 tool 文本**
  （避免白烧 token），而是作为一条**独立观察**消息注入上下文。
- 调帧要过可信上下文（A4）：跨设备 / 越权部件 → `denied`。
- 落点：`src/data_dog/evidence/visual.py`、`src/data_dog/providers/client.py`（`LLMChatMessage.images` + `_to_api`）、
  `src/data_dog/tools/investigation.py`、`src/data_dog/agent/loop.py`（`VISUAL_HARNESS_PROMPT`）、`src/data_dog/service/runtime.py`。

### 开关与依赖

| 项 | 值 | 说明 |
|---|---|---|
| `DD_VISUAL_EVIDENCE_DIR` | 空（默认） | 设了才启用视觉通道；空 = 工具不暴露（纯文本链路**零变化**） |
| `DD_LLM_VISION` | `0`（默认） | 当前模型是否**能接收原始图像**。`0` = `view_visual_frame` 只回帧元数据（尺寸/时间戳/部件）并在结果里写明“视觉通道未启用，设 `DD_LLM_VISION=1`”，**不发 base64、不调上游多模态接口**；`1` = 才把原帧当 content parts 送进模型。两个开关分开的理由：帧存不存在、模型能不能看图，是两件事——旧版把它们当一件事，配纯文本模型时上游 400 会让**整条调查失败关闭**（没配多模态 → 分析不了） |
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

## 3. 视觉通道实测：两次对照，结论相反——差别在"图里有没有文本没有的信息"

模型 `deepseek-v4.1`（自定义端点；文本 / 视觉 / 函数调用三项均实测可用）。两次都是同一
Agent、只切"有 / 无视觉通道"。

### 3.1 把遥测画成曲线：**负结果**（`scripts/visual_eval.py`，n=3/类=18）

| 条件 | n | accuracy | normal 误报率 | 真的调了视觉工具 |
|---|---|---|---|---|
| 关视觉 | 18 | **1.00** | **0.00** | — |
| 开视觉 | 18 | **0.89** | **0.67** | 0.44 |

图是**温度 / 电流曲线 + 阈值线**，与文本证据**同源**：没带来文本没有的信息，却可能让模型
"看着曲线觉得可疑"→ 误报。（"开视觉"里只有 44% 真调了工具，本身也不是干净对照。）

### 3.2 外观故障（遥测正常、只有画面有故障）：**正结果**（`scripts/appearance_eval.py`）

设计：遥测**全正常**；画面是 **MuJoCo 真渲染**的 Go2，叠加**外观故障**（漏油 / 形变 / 异物）
或干净；ground truth：有故障 → `needs_human_review`，干净 → `rejected`。
`candidate_id` 保持**中性**（曾因把 defect 名写进 id 导致一次污染，已修）。

| 条件 | accuracy | 故障召回 | 干净误报 | 调了视觉工具 |
|---|---|---|---|---|
| 关视觉 | **0.583** | 0.778 | **1.00** | — |
| 开视觉 | **1.00** | **1.00** | **0.00** | 1.00 |

**关视觉时干净误报 100%**：遥测正常、又拿不到画面，Agent 只能"拿不准就交人工"，把干净的
也一起交出去；**开视觉后完全分开**（1.00 / 1.00 / 0.00），且 100% 的调查真的调了视觉工具。

### 结论（一句话）

**视觉的价值 = 它携带了文本没有的信息。** 同源信息（把数字画成图）→ 无增益；
异源信息（真渲染画面上的外观缺陷）→ 显著增益。这也正说明真实产品必须接**真机相机**：
外观缺陷是遥测天生看不见的那一类。

### 边界
- 两次各 n=3/类，CI 极宽；外观故障是**画面叠加**（非物理仿真）；单模型、单窗口。
- 两次的"关视觉"基线**不可横向比**（3.1 是合成遥测故障，3.2 是外观故障）。
- 仍未接真机相机（Go2 WebRTC）。

## 4. 跨调查记忆怎么验（同口径）

先在一批上产出**真实**报告，再在**不相交**测试集上开 / 关记忆对照——**不要**把答案喂成
"既往报告"（那是造假）。

> 一句话：**名字不值钱，实测才值钱。** 本仓对视觉通道跑了两轮对照：先如实记下**负结果**
> （同源信息无增益、可能有代价），再在"文本拿不到的信息"场景下复现出**正结果**。
> 两轮都是"先有数、再下结论"。
