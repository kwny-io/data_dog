"""内置供应商注册表：各家 LLM 供应商的元数据。

目前所有供应商都走 OpenAI 兼容协议，只是 base_url 和一个示例模型名不同。
因此「接一个新供应商」通常只需要在这里加一行注册，不用再写一份接入代码。
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class VendorSpec:
    """一家供应商所需的全部接入信息。"""
    id: str              # 稳定标识，写入配置用
    label: str           # 展示给用户的名字
    base_url: str        # OpenAI 兼容的 base_url
    default_model: str   # 建议的默认模型
    requires_env_key: str  # 若用环境变量存 key，对应的变量名
    doc: str             # 一句话说明，用于交互式引导


BUILTIN_VENDORS: list[VendorSpec] = [
    VendorSpec(
        id="deepseek",
        label="DeepSeek（推荐，便宜 + 国内直连）",
        base_url="https://api.deepseek.com",
        default_model="deepseek-chat",
        requires_env_key="DEEPSEEK_API_KEY",
        doc="国内可用，价格低，Agent/工具调用能力不错，测试阶段首选。",
    ),
    VendorSpec(
        id="openai",
        label="OpenAI",
        base_url="https://api.openai.com/v1",
        default_model="gpt-4o-mini",
        requires_env_key="OPENAI_API_KEY",
        doc="全球最常见，海外网络与付费较方便，成本偏高。",
    ),
    VendorSpec(
        id="zhipu",
        label="智谱 GLM",
        base_url="https://open.bigmodel.cn/api/paas/v4",
        default_model="glm-4-flash",
        requires_env_key="ZHIPU_API_KEY",
        doc="国内直连，智谱官方 agent 生态，glm-4-flash 有免费档。",
    ),
]

BY_ID: dict[str, VendorSpec] = {v.id: v for v in BUILTIN_VENDORS}


def vendor_ids() -> list[str]:
    return [v.id for v in BUILTIN_VENDORS]


def get_vendor(vendor_id: str) -> VendorSpec:
    try:
        return BY_ID[vendor_id]
    except KeyError:
        known = ", ".join(vendor_ids())
        raise ValueError(f"未知供应商: {vendor_id!r}，可用: {known}") from None
