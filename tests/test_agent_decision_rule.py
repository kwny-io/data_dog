"""Agent 裁决口径（LLM 误报治理）：守住"证据不足"与"证据充分但正常"的区分。

为什么值得一条专门的测试
----------------------
真 LLM 那栏的 **normal 误报率 0.72**（175 台健康设备里 123 台被判 `needs_human_review`）
不是模型"能力不够"，而是**我们的口径写错了**：原 SYSTEM_PROMPT 说

    若证据不足以给出确定结论，应判为 inconclusive 或 needs_human_review

而健康设备是"**证据齐全、且全部正常**"——它被这句话误导向 `needs_human_review`。
这条规则把「**证据不足**」和「**证据充分但显示正常**」混成了一件事，而后者恰恰是一个
**确定的结论**（应判 `rejected`）。

口径写错 → 误报 → 值班人被淹没，这是**生产上最难发现的失败模式之一**（系统一切"正常"，
只是没人看告警了）。所以这里把它钉住：谁把这条区分改掉，测试立刻红。

⚠️ 本文件只守**口径文本**，不宣称治理后的效果——效果要靠真 LLM 重跑（本机 key 余额为零，
故 README 里那栏标注为未实测）。
"""
from __future__ import annotations

from data_dog.agent.loop import SYSTEM_PROMPT

# 原口径里那句把两件事混为一谈的话。它必须**不再出现**。
_OLD_CONFLATING_RULE = "若证据不足以给出确定结论"


def test_old_conflating_rule_is_gone():
    """「证据不足 → needs_human_review」这条泛化规则必须已被移除。

    留着它，模型就会继续把"查完一切正常"当成"证据不足"。
    """
    assert _OLD_CONFLATING_RULE not in SYSTEM_PROMPT, (
        "裁决口径又变回「证据不足就 needs_human_review」了——健康设备会被继续误报")


def test_prompt_states_an_ordered_decision_rule():
    """四档必须是**有序**给出的：先 rejected（正常），最后 needs_human_review（兜不住才用）。

    无序罗列会让模型按"哪个看起来更安全"挑，而"更安全"在这里恰好是误报的来源。
    """
    assert "裁决口径" in SYSTEM_PROMPT
    block = SYSTEM_PROMPT.split("裁决口径", 1)[1]
    i_rej = block.index("rejected")
    i_human = block.index("needs_human_review")
    assert i_rej < i_human, "rejected 没排在 needs_human_review 前面（顺序就是口径的一部分）"


def test_prompt_separates_insufficient_evidence_from_normal():
    """必须**显式**写明"没发现异常 ≠ 证据不足"——这正是模型犯错的地方。"""
    assert "没发现异常" in SYSTEM_PROMPT and "证据不足" in SYSTEM_PROMPT
    assert "≠" in SYSTEM_PROMPT, "缺少显式的区分表述"


def test_prompt_scopes_needs_human_review_to_missing_evidence():
    """`needs_human_review` 的适用条件必须被收窄到"关键证据拿不到 / 冲突无法消解"。"""
    assert "关键证据拿不到" in SYSTEM_PROMPT or "关键证据" in SYSTEM_PROMPT


def test_prompt_still_keeps_the_four_dispositions_and_json_contract():
    """治理不能把接口改坏：四档闭集与 JSON 契约仍在（下游按它解析）。"""
    for d in ("confirmed", "rejected", "inconclusive", "needs_human_review"):
        assert d in SYSTEM_PROMPT, f"裁决档位 {d} 不在提示词里了"
    assert "只能输出这个 JSON" in SYSTEM_PROMPT
