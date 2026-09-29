"""反馈→评估回灌的纯逻辑测试（`eval_feedback`）。

这些是**确定性**的归类与聚合，不依赖 DB/HTTP：给定 (人工 kind, Agent disposition)
必然落到一个确定的类别，聚合率也必然确定。重点守住：
  - 误报/漏报的方向不能反（最致命的度量错误）；
  - `no_action` / 未知 kind 不作标签，**不污染**一致率分母；
  - 交人工（escalated）既不算对也不算错，单列。
"""
from __future__ import annotations

from data_dog.eval_feedback import classify, summarize_feedback

# ---------------------------------------------------------------- classify

def test_agent_rejected_human_false_positive_agree():
    """人工说误报、Agent 当初也判 rejected → 一致。"""
    assert classify("rejected", "false_positive") == "agree"


def test_agent_confirmed_human_false_positive_is_false_positive():
    """人工说误报、Agent 却确认了 → **误报**（报了不该报的）。"""
    assert classify("confirmed", "false_positive") == "false_positive"


def test_agent_confirmed_human_repaired_agree():
    """人工说已修、Agent 也判 confirmed → 一致。"""
    assert classify("confirmed", "repaired") == "agree"


def test_agent_rejected_human_repaired_is_false_negative():
    """人工说真故障、Agent 却排除了 → **漏报**（该报没报）。"""
    assert classify("rejected", "repaired") == "false_negative"


def test_escalated_never_counts_as_agree_or_error():
    """Agent 交人工（needs_human_review / inconclusive）→ 单列 escalated。"""
    assert classify("needs_human_review", "false_positive") == "escalated"
    assert classify("needs_human_review", "repaired") == "escalated"
    assert classify("inconclusive", "repaired") == "escalated"


def test_no_action_is_unlabeled():
    """no_action 语义模糊（可能"小问题不改"也可能"误报"）→ 不作标签。"""
    assert classify("confirmed", "no_action") == "unlabeled"
    assert classify("rejected", "no_action") == "unlabeled"


def test_unknown_kind_is_unlabeled():
    assert classify("confirmed", "banana") == "unlabeled"


def test_blank_disposition_is_unlabeled():
    """Agent 裁决缺失（报告未落库）→ 如实归 unlabeled，不臆造。"""
    assert classify("", "repaired") == "unlabeled"
    assert classify(None, "false_positive") == "unlabeled"


# ---------------------------------------------------------------- summarize

def _row(kind, disp):
    return {"kind": kind, "agent_disposition": disp}


def test_summarize_rates_over_labeled_only():
    """一致率/误报率的分母只含**有标签**的行；no_action 不拉低数字。"""
    rows = [
        _row("false_positive", "rejected"),     # agree
        _row("repaired", "confirmed"),          # agree
        _row("false_positive", "confirmed"),    # false_positive
        _row("repaired", "rejected"),           # false_negative
        _row("no_action", "confirmed"),         # unlabeled
    ]
    s = summarize_feedback(rows)
    assert s["total"] == 5
    assert s["labeled"] == 4
    assert s["agree"] == 2
    assert s["false_positive"] == 1
    assert s["false_negative"] == 1
    assert s["unlabeled"] == 1
    assert s["agreement_rate"] == 0.5
    assert s["false_positive_rate"] == 0.25
    assert s["false_negative_rate"] == 0.25


def test_summarize_empty_is_all_zero():
    s = summarize_feedback([])
    assert s["total"] == 0 and s["labeled"] == 0
    assert s["agreement_rate"] == 0.0 and s["false_positive_rate"] == 0.0


def test_summarize_escalated_rate_uses_total():
    """escalated_rate 分母用**全部**行（"是否转人工"是覆盖率问题，不是准确率）。"""
    rows = [_row("repaired", "needs_human_review"), _row("repaired", "confirmed")]
    s = summarize_feedback(rows)
    assert s["escalated"] == 1
    assert s["labeled"] == 1              # escalated 不计入 labeled
    assert s["escalated_rate"] == 0.5     # 1 / 2 total
