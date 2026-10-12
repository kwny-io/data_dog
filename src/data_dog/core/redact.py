"""统一脱敏：**任何可能离开进程的文本**（错误详情 / 日志字段 / 报告字段）先过这里。

为什么需要它
-----------
provider 的 SDK 异常消息里可能带请求上下文——`Authorization: Bearer <key>`、
`?api_key=<key>`，甚至（多模态请求）整段 `data:image/png;base64,...`。这些消息会经
`LLMCallError.detail` 流到 **HTTP 错误体**、**调查报告的 summary/missing_evidence** 与日志。
故：**在异常归一化的那一处**就脱敏（源头），另在服务错误出口与报告落库前再兜一层。

口径
----
- **按值脱敏**：调用方传入真实密钥，出现即换成 ***（最强，不依赖正则）。
- 再按模式脱敏：Authorization / Bearer / api_key= / token= / secret= / X-API-Key、
  `data:*;base64,...`、超长裸 base64。
- 最后**截断**（默认 500 字符）：错误详情不该无界增长（多模态请求体可能极大）。

诚实边界
--------
这是**尽力而为**的脱敏，不是数据防泄漏（DLP）产品：正则无法穷举所有密钥形态。
真正的保证来自**根本不把密钥写进这些路径**——本模块只是最后一层皮。
"""
from __future__ import annotations

import re

_MASK = "***"

_PATTERNS = [
    # ⚠️ 顺序要紧：Bearer 必须**先**跑。否则 "Authorization: Bearer <key>" 会被
    # authorization 规则先吃掉 "Bearer" 这半截，留下 "*** <key>" —— 密钥反而漏了。
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-+/=]{8,}"),
    re.compile(r"(?i)\bauthorization\b\s*[:=]\s*[^\r\n,;|]+"),
    # api_key=xxx / access_token=xxx / secret=xxx / password=xxx
    re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|secret|password|passwd|pwd)"
               r"\b\s*[:=]\s*['\"]?[^\s,;'\"]+"),
    re.compile(r"(?i)\bx-api-key\b\s*[:=]\s*[^\s,;'\"]+"),
    # 内联图片 / 大块 base64（多模态请求体可能被 SDK 回显）
    re.compile(r"data:[a-z0-9.+-]+/[a-z0-9.+-]+;base64,[A-Za-z0-9+/=]{16,}"),
    re.compile(r"[A-Za-z0-9+/]{200,}={0,2}"),
]


def redact(text: object, *, secrets: list[str] | tuple[str, ...] | None = None,
           max_len: int = 500) -> str:
    """把可能含密钥/大块载荷的文本脱敏并截断；非字符串按 str() 处理。"""
    out = "" if text is None else str(text)
    for s in (secrets or ()):              # 1) 按值：真实密钥出现即抹掉（最强）
        if s and len(s) >= 6:
            out = out.replace(s, _MASK)
    for pat in _PATTERNS:                  # 2) 按模式
        out = pat.sub(_MASK, out)
    if max_len and len(out) > max_len:     # 3) 截断
        out = out[:max_len] + "…(truncated)"
    return out
