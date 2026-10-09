"""属性测试：用固定种子的随机用例守"不变量"，而不是列例子。

为什么不用 hypothesis：本仓刻意保持零测试依赖（除 pytest）。随机 + 固定种子能达到
同样的目的——覆盖"我没想到的输入"，而失败可复现（同一个种子必然重现）。

为什么这几处值得用属性而不是例子：它们的正确性本质是**不变量**
（区间单调且在界内、键稳定且对输入敏感、限流永不超发、解释器与 Python 同值同判），
列几个例子只能证明"这几个点对"，随机扫才能碰到边界。
"""
from __future__ import annotations

import random

from data_dog.alerting.outbox import make_event_id, make_idempotency_key
from data_dog.operator_factory.dsl import WindowContext, compile_rule, evaluate
from data_dog.state.memory import InMemoryStateStore
from data_dog.synth.dataset_eval import wilson_ci


def _dsl_bool(when: str, fields: dict[str, float]) -> bool:
    rule = compile_rule({"rule_id": "P", "when": when}, declared_fields=list(fields))
    return bool(evaluate(rule, fields, WindowContext(series={})))


def test_property_wilson_ci_invariants():
    """Wilson 区间：含点估计、落在 [0,1]、随命中数单调、两端互为镜像。"""
    rng = random.Random(7)
    for _ in range(300):
        n = rng.randint(1, 500)
        hit = rng.randint(0, n)
        lo, hi = wilson_ci(hit, n)
        p = hit / n
        assert 0.0 <= lo <= p <= hi <= 1.0, (hit, n, lo, p, hi)
        if hit < n:
            lo2, _ = wilson_ci(hit + 1, n)
            assert lo2 >= lo, ("命中更多时下界不应下降", hit, n)
    for n in (1, 5, 20, 200):
        lo0, hi0 = wilson_ci(0, n)
        lo1, hi1 = wilson_ci(n, n)
        assert lo0 == 0.0 and hi1 == 1.0
        assert abs((1.0 - hi0) - lo1) < 1e-12, "0/n 与 n/n 的区间应互为镜像"
    print("PASS test_property_wilson_ci_invariants")


def test_property_idempotency_key_stable_and_sensitive():
    """幂等键 / event_id：同输入恒等；任一组成变化必变。

    敏感性是安全属性：若设备/类型/Episode 之一变了而键不变，两条不同故障会被
    合并成一条 → **静默漏报**。
    """
    rng = random.Random(11)
    for _ in range(200):
        dev = f"dev-{rng.randint(0, 50)}"
        ev = rng.choice(["over_temp", "packet_loss", "current_bias"])
        ep = f"ep-{rng.randint(0, 50)}"
        assert make_idempotency_key(dev, ev, ep) == make_idempotency_key(dev, ev, ep)
        assert make_event_id(dev, ev, ep) == make_event_id(dev, ev, ep)
        assert make_idempotency_key(dev + "x", ev, ep) != make_idempotency_key(dev, ev, ep)
        assert make_idempotency_key(dev, ev + "x", ep) != make_idempotency_key(dev, ev, ep)
        assert make_idempotency_key(dev, ev, ep + "x") != make_idempotency_key(dev, ev, ep)
    print("PASS test_property_idempotency_key_stable_and_sensitive")


def test_property_rate_limit_never_over_burst():
    """限流桶：任意时间线下放行数不超过"初始 burst + 期间补充"（多放行 = 限流失效）。"""
    rng = random.Random(13)
    for _ in range(60):
        burst = rng.randint(1, 5)
        rate = rng.choice([0.0, 0.5, 1.0, 5.0])
        store = InMemoryStateStore()
        now = 0.0
        allowed = 0
        for _ in range(40):
            now += rng.choice([0.0, 0.01, 0.2, 1.0, 3.0])   # 含同一时刻连打（逼 burst 边界）
            if store.rate_limit_allow("dev", now=now, rate_per_sec=rate, burst=burst):
                allowed += 1
        ceiling = burst + int(rate * now) + 1               # +1 容忍浮点取整
        assert allowed <= ceiling, (allowed, ceiling, burst, rate, now)
    print("PASS test_property_rate_limit_never_over_burst")


def test_property_dsl_matches_python_comparison():
    """受限 DSL 的比较/布尔组合与 Python 同值同判（解释器等价性随机抽查）。"""
    rng = random.Random(17)
    for _ in range(300):
        x = round(rng.uniform(-50, 150), 3)
        k = round(rng.uniform(-50, 150), 3)
        fields = {"x": x}
        assert _dsl_bool(f"x > {k}", fields) == (x > k)
        assert _dsl_bool(f"x < {k}", fields) == (x < k)
        assert _dsl_bool(f"x >= {k}", fields) == (x >= k)
        assert _dsl_bool(f"x <= {k}", fields) == (x <= k)
        assert _dsl_bool(f"not (x > {k})", fields) == (not (x > k))
        assert _dsl_bool(f"(x > {k}) and (x < 200)", fields) == ((x > k) and (x < 200))
        assert _dsl_bool(f"(x > {k}) or (x > 100)", fields) == ((x > k) or (x > 100))
    print("PASS test_property_dsl_matches_python_comparison")
