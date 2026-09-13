# 语料来源说明（Unitree 公开资料）

本目录的语料来自 Unitree Robotics **公开发布**的资料，用于 `data_dog` 离线算子工厂（P1）的
"文档 → 知识 → 算子"链路验证。**每个文件的来源与许可如下**，使用时请遵守对应许可。

| 文件 | 来源 | 许可 |
|---|---|---|
| `unitree_go2_user_manual.md`（原始 `raw/go2_user_manual.pdf`） | Unitree 官方 Go2 用户手册（公开） | Unitree 官网公开文档 |
| `raw/unitree_sdk2__*.idl.txt` | [unitreerobotics/unitree_sdk2](https://github.com/unitreerobotics/unitree_sdk2) 的 IDL 定义 | BSD-3-Clause |
| `raw/unitree_actuator_sdk/motor_msg_GO-M8010-6.h` | [unitreerobotics/unitree_actuator_sdk](https://github.com/unitreerobotics/unitree_actuator_sdk) 电机协议头 | BSD-3-Clause |
| `raw/unitree_actuator_sdk/LICENSE` | 上述仓库 LICENSE（署名要求） | BSD-3-Clause |
| `go2_webrtc*.md` | [tfoldi/go2-webrtc](https://github.com/tfoldi/go2-webrtc) 社区解码文档 | 见原仓库 |
| `westonrobot_go2_diag.md`（原始 `raw/*.html`） | Weston Robot Go2 诊断指南（公开） | 见原站点 |

## 关键：电机温度保护阈值（权威出处）

`raw/unitree_actuator_sdk/motor_msg_GO-M8010-6.h` 第 45–46 行（**厂商官方开源，BSD-3-Clause**）：

```c
int8_t   temp;          // 电机温度: -128~127°C 90°C时触发温度保护
uint8_t  MError :3;     // 电机错误标识: 0.正常 1.过热 2.过流 3.过压 4.编码器故障 5-7.保留
```

**意义**：
- `90°C`（温度保护/停机阈）→ 由 `unconfirmed` **升级为 `verified`**（有厂商官方出处）。
- `75°C`（warning 阈）→ **此开源协议中未出现**，仍为 `unconfirmed` / 项目假设。
- `MError` 枚举（正常/过热/过流/过压/编码器故障）→ 可抽为结构化错误码知识。

> 这正是"诚实抽取"机制的价值：**能证实的才升级为厂商事实，证不了的保持待确认**，不因为"反正差不多"就把 75°C 也说成官方数据。
