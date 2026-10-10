"""设备身份签名（⑦）：把"设备身份"从**网关注入的声称**抬到**设备自己证明**。

为什么需要它（与 ①/② 修的是同一类洞，只是粒度更细）
-------------------------------------------------
当前身份链是：调用方自报 `X-Device-Sn` → 网关把它当可信身份注入。在这条链上，
**任何持密钥的调用方都能声称自己是任意设备**。①/② 已经把"租户"这一层收紧了
（租户密钥环 + 冒充被拒），但**设备层没有**：同一租户下，A 设备可以冒充 B 设备。

设备级身份的**最终解法**是前置网关或 mTLS（见 `docs/delivery.md` 的差距表）。
本模块提供一个**零新依赖、可渐进上线**的过渡档：设备持有自己的密钥，对
"身份 + 本条数据"做 HMAC-SHA256，网关验签。

为什么签名必须覆盖**数据**、而不只是身份
--------------------------------------
只签设备号的签名可以被**重放**：攻击者录下一条合法签名，配上任意伪造的正文照样通过。
所以签名绑定 `tenant|device|event_id|occurred_at|sequence|sha256(payload)`：
换正文、换事件、换时间窗都会让验签失败。重放**同一条**报文仍会被既有的
`(幂等键, revision)` 幂等机制挡住——两层职责不重叠。

三档（`DD_DEVICE_SIGNING`）
--------------------------
- `off`（默认）：完全不验。**行为与改动前逐字节一致**（向后兼容，零回归）。
- `optional`：**按设备生效**——某个设备一旦在 `DD_DEVICE_KEYS` 里配了密钥，它的请求就
  **必须**带合法签名；没配密钥的设备放行。这是**渐进上线**用的档：可以一台设备一台设备地
  开强制，不需要"全网同一天切换"。把"该设备已配密钥"本身当作"该设备已纳入强制"的信号，
  省掉第二份需要人工保持同步的开关清单（两份清单必然漂移）。
- `required`：**所有**设备都必须配密钥且带合法签名；缺一即拒（失败关闭）。

⚠️ 为什么"签名不对"在任何档位都必须拒绝
--------------------------------------
若 `optional` 下"签名不对就当没带、放行"，攻击者只要**故意发垃圾签名**就等于绕过了
整个校验——这档位会变成一张纸。验签结果只有两种处理：**通过，或拒绝**，没有"忽略"。

⚠️ 诚实边界
----------
- 密钥仍是**预共享**（配置在 `DD_DEVICE_KEYS` 里），不是设备出厂证书/TPM 背书的密钥；
  密钥泄露的后果与"设备被克隆"同级。真正的设备身份要 mTLS/安全元件。
- 保护的是**数据面入口的完整性**：能证明"这条报文来自持有该密钥的设备"，
  不能证明"该设备未被物理控制"，也不防**重放同一条报文**（那由幂等键挡）。
- 与 ② 一样，密钥在**环境变量**里：无 KMS、无自动轮换。
"""
from __future__ import annotations

import hashlib
import hmac
from typing import Any

_SIG_ALG = "sha256"


def payload_digest(payload: str) -> str:
    """载荷摘要：把"正文"折成一个定长值，好让签名覆盖数据而不是只覆盖身份。

    空载荷也有确定的摘要（`sha256("")`），不是"没有摘要"——否则"不带载荷"与
    "载荷为空"会签名出同一个东西。
    """
    return hashlib.sha256((payload or "").encode("utf-8")).hexdigest()


def canonical_message(*, tenant_id: str, device_sn: str, event_id: str,
                      occurred_at: str, sequence: Any, payload: str) -> str:
    """待签名的规范化字符串（**设备侧与网关侧必须逐字节一致**）。

    为什么用 `\\n` 分隔而不是拼接：拼接会让 `("a","bc")` 与 `("ab","c")` 产生同一串
    （**歧义签名**，可被构造碰撞）。定界符让每个字段的边界唯一。
    字段顺序固定，且**不含**任何可选/可缺省字段——规范化必须无分支。
    """
    return "\n".join([
        str(tenant_id), str(device_sn), str(event_id),
        str(occurred_at), str(sequence), payload_digest(payload),
    ])


def signature_for(secret: str, **fields: Any) -> str:
    """算出签名（hex）。设备侧参照实现；网关侧用它验签、测试用它造样本。"""
    msg = canonical_message(**fields)
    return hmac.new(secret.encode("utf-8"), msg.encode("utf-8"),
                    hashlib.sha256).hexdigest()


def parse_header(value: str | None) -> str | None:
    """解析 `X-Device-Signature: sha256=<hex>`，返回小写 hex；格式不对返回 None。

    要求带算法前缀（而不是裸 hex）：将来换算法时能区分，且裸 hex 无法表达算法
    （"换算法"会变成一次隐式的全网破坏性变更）。
    """
    if not value:
        return None
    raw = value.strip()
    if "=" not in raw:
        return None
    alg, _, hexpart = raw.partition("=")
    if alg.strip().lower() != _SIG_ALG:
        return None
    hexpart = hexpart.strip().lower()
    if not hexpart or any(c not in "0123456789abcdef" for c in hexpart):
        return None
    return hexpart


def check_device_signature(*, mode: str, keys: dict[str, str], tenant_id: str,
                           device_sn: str, raw: dict[str, Any],
                           provided: str | None) -> tuple[bool, str, str]:
    """验签。返回 `(ok, reason, detail)`；`ok=False` 时调用方应**拒绝**该请求。

    `reason` 是有限闭集（与全仓错误语义一致，便于指标分类）：
      - `device_signature_missing`  ：required 档但没带签名
      - `device_signature_unknown`  ：设备没配密钥（required 档；optional 档带了签名也算）
      - `device_signature_invalid`  ：签名不对 / 头部格式非法
      - `device_signature_misconfigured`：配置坏（服务端问题 → 调用方应看到 503）
    """
    if mode == "off":
        return True, "", ""

    hexpart = parse_header(provided)
    if provided and hexpart is None:
        # 带了签名但格式不对 → 不是"没带"，是"给了坏数据"。任何档位都拒。
        return False, "device_signature_invalid", "X-Device-Signature 格式应为 sha256=<hex>"

    secret = keys.get(device_sn)
    if secret is None:
        if mode == "required":
            return False, "device_signature_unknown", f"设备 {device_sn} 未配置签名密钥"
        # optional + 没带签名 + 设备没配密钥 → 放行（渐进上线的正常态）
        if hexpart is None:
            return True, "", ""
        return False, "device_signature_unknown", f"设备 {device_sn} 未配置签名密钥"

    if hexpart is None:
        return False, "device_signature_missing", "缺少 X-Device-Signature"

    expected = signature_for(
        secret, tenant_id=tenant_id, device_sn=device_sn,
        event_id=raw.get("event_id", ""), occurred_at=raw.get("occurred_at", ""),
        sequence=raw.get("sequence", ""), payload=raw.get("payload_inline", "") or "",
    )
    # 常量时间比较：按字符逐位试探会泄露正确前缀（与 ② 的密钥比较同一口径）。
    if not hmac.compare_digest(expected, hexpart):
        return False, "device_signature_invalid", "设备签名校验失败"
    return True, "", ""
