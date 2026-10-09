"""MuJoCo 数据源的**纯逻辑**测试：不跑真仿真，只测可判定的确定性部分。

为什么要有这个文件
------------------
`mujoco_sim/` 的四个模块里，"真跑仿真"只占极少数几行（`simulate.simulate` 里的 `mj_step`
循环）。其余全是纯函数/纯数据变换：步态目标角（`gait`）、噪声叠加（`noise`）、试验条件网格
（`conditions`）、样本结构与 ground truth 标签（`to_sample`）。这些逻辑一旦错，会**静默**
污染整个数据集与评估口径——而真仿真跑一次是秒级到分钟级，不适合放进单测。

因此本文件的做法是：手工构造与 `simulate.simulate` 输出同构的 `SimResult`（形状/字段一致，
数值可控），把纯逻辑逐条钉住。**不调用 `simulate()`**（`make_mujoco_batch` 也是用 monkeypatch
桩掉 `simulate` 后才测它的批次组装逻辑）；唯一加载 MuJoCo 资产的是 `_get_model` 那条用例
（只改数组、不 `mj_step`，理由写在该用例 docstring 里）。

`synth/synth_eval.py` 的评估内循环用例放在 `tests/test_synth.py`（同一条数据→评估链路）。

约定：每个 test 结尾 `print("PASS <name>")`；发现"真实行为与注释/宣称不符"的地方，写"记录
当前真实行为"并在注释里标明，**不改 src**。
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict
from types import SimpleNamespace

import numpy as np
import pytest

from data_dog.mujoco_sim.conditions import (
    FAULT_COMPONENTS,
    FAULTS,
    SamplingSpec,
    TrialConfig,
    config_from_dict,
    config_to_dict,
    make_configs,
)
from data_dog.mujoco_sim.gait import (
    ACTUATORS,
    HOME_KNEE,
    HOME_THIGH,
    GaitParams,
    gains,
    is_fallen,
    joint_targets,
)
from data_dog.mujoco_sim.noise import NoiseConfig, apply_noise
from data_dog.mujoco_sim.simulate import DEFAULT_MODEL, SimResult, _pd_target
from data_dog.mujoco_sim.to_sample import make_mujoco_batch, to_synth_sample
from data_dog.synth.generator import DEFAULT_PARAMS, FAULT_DEFS


def _fake_sim(n_steps: int = 200, nj: int = 12, dt: float = 0.01,
              seed: int = 0, torque_scale: float = 2.0) -> SimResult:
    """手工构造一个与 `simulate.simulate` 同构的 SimResult（不加载 mujoco）。"""
    rng = np.random.default_rng(seed)
    ts = np.arange(n_steps) * dt
    torque = rng.normal(0.0, torque_scale, size=(n_steps, nj))
    return SimResult(
        joints=list(ACTUATORS),
        ts=ts,
        pos=rng.normal(0.0, 0.1, size=(n_steps, nj)),
        vel=rng.normal(0.0, 0.5, size=(n_steps, nj)),
        torque=torque,
        current=np.abs(torque) / DEFAULT_PARAMS["kt"],
        temperature=25.0 + np.cumsum(np.abs(torque), axis=0) * 0.05,
        base_pos=np.tile([0.0, 0.0, 0.3], (n_steps, 1)),
        base_quat=np.tile([1.0, 0.0, 0.0, 0.0], (n_steps, 1)),
    )


# ============================================================ gait.py

def test_gait_micro_swing_matches_legacy_pd_target():
    """`micro_swing` 档必须与旧 `simulate._pd_target` **逐字一致**（保回归）。

    这一档是"最后兜底"：它被刻意保留为旧口径（膝目标≈0、无 range 裁剪），所以任何
    "顺手统一一下公式"的改动都会让历史数据集口径漂移。用例直接对拍旧函数。
    """
    p = GaitParams(mode="micro_swing")
    for t in (0.0, 0.05, 0.3, 1.7, 9.99):
        expected = np.array([_pd_target(t, name, 0.01) for name in ACTUATORS])
        assert np.allclose(joint_targets(t, p), expected)
    print("PASS test_gait_micro_swing_matches_legacy_pd_target")


def test_gait_trot_diagonal_phases_and_shape():
    """trot 的对角同相结构：FL↔RR 同相、FR↔RL 同相（两组差 π），且输出 12 维。"""
    p = GaitParams(freq=1.0, mode="free")
    for t in (0.0, 0.13, 0.5, 0.77):
        out = joint_targets(t, p)
        assert out.shape == (12,)
        fl, fr, rl, rr = out[0:3], out[3:6], out[6:9], out[9:12]
        assert np.allclose(fl, rr), "FL 与 RR 必须同相"
        assert np.allclose(fr, rl), "FR 与 RL 必须同相"
        assert not np.allclose(fl, fr), "两组对角腿相差 π，不该重合"
    print("PASS test_gait_trot_diagonal_phases_and_shape")


def test_gait_trot_stance_sweeps_back_and_swing_lifts_knee():
    """支撑相线性后扫 + 膝伸直承重；摆动相快速回摆 + 抬膝（膝更弯）。"""
    p = GaitParams(freq=1.0, mode="free")
    # FL 相位 0：t=0 支撑相起点，t=0.4 支撑相末段，t=0.6 摆动相早段
    t0, t_stance, t_swing = joint_targets(0.0, p), joint_targets(0.4, p), joint_targets(0.6, p)
    assert t0[1] > t_stance[1], "支撑相大腿角应随时间后扫（减小）"
    assert t0[2] == pytest.approx(HOME_KNEE)
    assert t_stance[2] == pytest.approx(HOME_KNEE), "支撑相膝保持 home（伸直承重）"
    assert t_swing[2] < HOME_KNEE, "摆动相膝更弯（抬膝离地）"
    assert t0[1] == pytest.approx(HOME_THIGH + p.stroke * 0.5)
    # 对角腿此刻正在摆动相起点（相位差 π）
    assert t0[4] == pytest.approx(HOME_THIGH - p.stroke * 0.5)
    print("PASS test_gait_trot_stance_sweeps_back_and_swing_lifts_knee")


def test_gait_clips_to_joint_ranges_front_vs_back():
    """极端幅度下所有关节角都被裁进 go2.xml 的 range，且前/后腿大腿范围不同。

    这条用例是"裁剪真的生效"的证据：给 10 倍幅度，输出必须仍落在 range 内；同时
    FL（前腿）与 RL（后腿）在同一个 t 上被裁到**不同**的边界，说明用的是各自的 range。
    """
    p = GaitParams(freq=1.0, mode="free", stroke=10.0, lift=10.0, a_hip=10.0)
    hi = joint_targets(0.02, p)
    lo = joint_targets(0.52, p)
    assert hi[0] == pytest.approx(1.0472)         # hip 上界
    assert hi[1] == pytest.approx(3.4907)         # 前腿 thigh 上界
    assert hi[7] == pytest.approx(-0.5236)        # 后腿 thigh 下界（与前腿的 -1.5708 不同）
    assert lo[0] == pytest.approx(-1.0472)        # hip 下界
    assert lo[1] == pytest.approx(-1.5708)        # 前腿 thigh 下界
    assert lo[7] == pytest.approx(4.5379)         # 后腿 thigh 上界
    assert lo[2] == pytest.approx(-2.7227)        # knee 下界
    # knee 上界：反向抬膝（lift<0）才能顶到
    up = joint_targets(0.75, GaitParams(freq=1.0, mode="free", lift=-3.0))
    assert up[2] == pytest.approx(-0.83776)
    # 全时段扫一遍：任何 t 都不越界
    for t in np.linspace(0.0, 2.0, 41):
        out = joint_targets(float(t), p)
        assert abs(out[0::3]).max() <= 1.0472 + 1e-9
        assert out[2::3].min() >= -2.7227 - 1e-9
        assert out[2::3].max() <= -0.83776 + 1e-9
    print("PASS test_gait_clips_to_joint_ranges_front_vs_back")


def test_gait_freq_zero_is_static_but_not_home_pose():
    """记录当前真实行为（**与注释不符，未修**）：`freq=0` 是"静止"但不是"站立 home 姿态"。

    `GaitParams.freq` 的注释写的是"0 表示不迈步（站立）"，但 trot 分支在 freq=0 时把两组
    对角腿分别停在 `HOME_THIGH ± stroke/2`（默认 1.05 / 0.75 rad），既不是 `HOME_THIGH`
    也不是对称姿态——机身会处于一个恒定的、对角线不对称的静态站姿。这不会随时间变化
    （可复现），但"freq=0 == 站立"的直觉是错的：想表达 home 站立应显式给 `stroke=0`。
    """
    p = GaitParams(freq=0.0, mode="free")
    t0, t5 = joint_targets(0.0, p), joint_targets(5.0, p)
    assert np.allclose(t0, t5), "freq=0 时目标角不随时间变化（确为静止）"
    assert t0[1] == pytest.approx(HOME_THIGH + p.stroke / 2)
    assert t0[4] == pytest.approx(HOME_THIGH - p.stroke / 2)
    assert t0[1] != pytest.approx(HOME_THIGH), "静止姿态 != home 站立姿态"
    # 显式 stroke=0 才是真站立
    stand = joint_targets(0.0, GaitParams(freq=0.0, mode="free", stroke=0.0))
    assert stand[1] == pytest.approx(HOME_THIGH) and stand[4] == pytest.approx(HOME_THIGH)
    print("PASS test_gait_freq_zero_is_static_but_not_home_pose")


def test_gait_gains_follow_actuator_kind():
    """PD 增益按执行器名的后缀（hip/thigh/calf）分配，顺序与 ACTUATORS 严格一致。"""
    p = GaitParams(kp_hip=11.0, kp_thigh=22.0, kp_knee=33.0,
                   kd_hip=0.1, kd_thigh=0.2, kd_knee=0.3)
    kp, kd = gains(p)
    assert kp.shape == (12,) and kd.shape == (12,)
    for i, name in enumerate(ACTUATORS):
        kind = name.split("_")[-1]
        assert kp[i] == {"hip": 11.0, "thigh": 22.0, "calf": 33.0}[kind]
        assert kd[i] == {"hip": 0.1, "thigh": 0.2, "calf": 0.3}[kind]
    print("PASS test_gait_gains_follow_actuator_kind")


def test_gait_is_fallen_detection():
    """翻倒判据：空/NaN 视为翻倒；基座过低或 up 向量 z 分量过小（倾斜）也判翻倒。"""
    upright = np.tile([1.0, 0.0, 0.0, 0.0], (5, 1))
    pos_ok = np.tile([0.0, 0.0, 0.30], (5, 1))
    assert not is_fallen(pos_ok, upright)

    assert is_fallen(np.zeros((0, 3)), np.zeros((0, 4))), "空轨迹视为翻倒（无证据=不安全）"
    nan_pos = pos_ok.copy()
    nan_pos[2, 2] = np.nan
    assert is_fallen(nan_pos, upright)

    low = pos_ok.copy()
    low[3, 2] = 0.10                      # 任一时刻过低
    assert is_fallen(low, upright)

    # 绕 x 轴 90°：up_z = 1 - 2(x²+y²) = 0 → 翻倒；60° 倾斜 up_z=0.5 也判翻倒
    rolled = np.tile([0.70710678, 0.70710678, 0.0, 0.0], (5, 1))
    assert is_fallen(pos_ok, rolled)
    tilted = np.tile([np.cos(np.pi / 6), np.sin(np.pi / 6), 0.0, 0.0], (5, 1))
    assert is_fallen(pos_ok, tilted)
    # 阈值可调：把 min_z 放到很低时，仅高度不构成翻倒
    assert not is_fallen(low, upright, min_z=0.05)
    print("PASS test_gait_is_fallen_detection")


# ============================================================ noise.py

def test_noise_config_active_switches():
    """`NoiseConfig.active()` 是噪声清单的唯一来源：关闭时为空，各开关逐项生效且顺序固定。"""
    assert NoiseConfig().active() == []
    assert NoiseConfig(enable=True).active() == [
        "quantize", "latency", "cogging", "battery_sag", "current_meas", "thermal",
    ]
    assert NoiseConfig(enable=True, cog_amp=0.0).active() == [
        "quantize", "latency", "battery_sag", "current_meas", "thermal",
    ]
    assert NoiseConfig(enable=True, jitter_p=0.1).active() == [
        "quantize", "latency", "cogging", "jitter", "battery_sag",
        "current_meas", "thermal",
    ]
    assert NoiseConfig(enable=True, r_int=0.0).active() == [
        "quantize", "latency", "cogging", "current_meas", "thermal",
    ]
    assert NoiseConfig(enable=True, cur_noise_a=0.0, cur_bias_sigma=0.0,
                       cur_gain_sigma=0.0).active() == [
        "quantize", "latency", "cogging", "battery_sag", "thermal",
    ]
    assert NoiseConfig(enable=True, temp_obs_sigma=0.0, temp_proc_sigma=0.0).active() == [
        "quantize", "latency", "cogging", "battery_sag", "current_meas",
    ]
    assert NoiseConfig(enable=True, loss_p=0.2).active()[-1] == "packet_loss"
    print("PASS test_noise_config_active_switches")


def test_noise_disabled_is_identity():
    """`enable=False` 必须等价于什么都不做：返回**同一个对象**、无记录、数组不变。"""
    sim = _fake_sim()
    before = sim.pos.copy()
    out, rec = apply_noise(sim, NoiseConfig(), np.random.default_rng(0))
    assert out is sim and rec == []
    assert np.array_equal(sim.pos, before)
    print("PASS test_noise_disabled_is_identity")


def test_noise_enabled_layers_order_and_effects():
    """全开噪声：作用顺序固定、不改入参、量化/延迟可验算、母线电压有工程下限。"""
    sim = _fake_sim(n_steps=40, seed=3)
    pos_before, torque_before, temp_before = (
        sim.pos.copy(), sim.torque.copy(), sim.temperature.copy())

    cfg = NoiseConfig(enable=True)
    out, rec = apply_noise(sim, cfg, np.random.default_rng(11))
    assert [r["noise"] for r in rec] == ["quantize", "latency", "cogging",
                                         "battery_sag", "current_meas", "thermal"]
    dq = 2.0 * np.pi / (2 ** cfg.quantize_bits)
    assert rec[0]["dq_rad"] == pytest.approx(dq)
    assert np.allclose(np.round(out.pos / dq) * dq, out.pos), "位置必须落在量化格点上"

    # 不原地改输入
    assert np.array_equal(sim.pos, pos_before)
    assert np.array_equal(sim.torque, torque_before)
    assert np.array_equal(sim.temperature, temp_before)

    # 形状/字段一致
    assert out.pos.shape == sim.pos.shape and out.vel.shape == sim.vel.shape
    assert out.temperature.shape == sim.temperature.shape
    assert out.valid.shape == sim.valid.shape if sim.valid.size else True
    assert out.valid.all(), "默认 loss_p=0 → 不应有丢包"

    # 延迟 1 步：第 i 行 = 量化后第 i-1 行（首行重复），故错位一行
    quant = np.round(sim.pos / dq) * dq
    assert np.allclose(out.pos[1:], quant[:-1])
    assert np.allclose(out.pos[0], quant[0])

    # 齿槽转矩给力矩叠加了非零扰动，但幅度受 amp 约束
    assert not np.allclose(out.torque, sim.torque)
    assert np.abs(out.torque - sim.torque).max() <= cfg.cog_amp + 1e-9

    # 电流测量：围绕 |τ|/kt 抖动（增益/偏置/逐点噪声都很小）
    assert np.abs(out.current - np.abs(out.torque) / 0.25).max() < 1.5
    # 母线电压：sag 后不超过开路电压，且不低于 0.7*V_oc 的下限
    assert out.v_bus.max() <= cfg.v_oc + 1e-9
    assert out.v_bus.min() >= 0.7 * cfg.v_oc - 1e-9
    print("PASS test_noise_enabled_layers_order_and_effects")


def test_noise_is_reproducible_and_temperature_bounded():
    """同一 seed → 逐位相同的输出（评估可复现的前提）；温度只被小幅扰动。"""
    sim = _fake_sim(n_steps=30, seed=4)
    a, _ = apply_noise(sim, NoiseConfig(enable=True), np.random.default_rng(5))
    b, _ = apply_noise(sim, NoiseConfig(enable=True), np.random.default_rng(5))
    assert np.array_equal(a.pos, b.pos)
    assert np.array_equal(a.torque, b.torque)
    assert np.array_equal(a.current, b.current)
    assert np.array_equal(a.temperature, b.temperature)
    assert abs(float(a.temperature.mean() - sim.temperature.mean())) < 2.0
    print("PASS test_noise_is_reproducible_and_temperature_bounded")


def test_noise_jitter_and_packet_loss():
    """抖动（时间索引平移）与丢包（有效性掩码）都要按记录里的 p 生效。

    量化与延迟都不消耗 rng，所以可以先用 jitter_p=0 跑一次拿到"抖动前"的位置数组，
    再断言 jitter_p=1.0 时 `pos[1:-1]` 恰好等于该数组的 `[:-2]`。
    """
    sim = _fake_sim(n_steps=200, nj=12, seed=6)
    pre, _ = apply_noise(sim, NoiseConfig(enable=True, jitter_p=0.0),
                         np.random.default_rng(1))
    out, rec = apply_noise(sim, NoiseConfig(enable=True, jitter_p=1.0, loss_p=0.3),
                           np.random.default_rng(1))
    assert "jitter" in [r["noise"] for r in rec]
    assert np.allclose(out.pos[1:-1], pre.pos[:-2]), "全抖动时中段整体前移一步"
    # 记录当前真实行为（**注释与代码不符 + 物理不一致，未修**）：
    #   1) `jitter_p` 的注释说"把该行时间戳索引平移 ±1 步"，实际只会往前挪 1 步
    #      （用前一步的值），不存在 +1 方向；
    #   2) 抖动只重写了 `pos`，`vel` 原样不动——于是同一行的位置与速度不再对应同一时刻，
    #      而"采样抖动"按语义应整体平移一个样本（pos 与 vel 同时挪）。
    assert np.allclose(out.vel[1:-1], pre.vel[1:-1]), "vel 未被抖动（仅 pos 被改写）"

    loss_rec = next(r for r in rec if r["noise"] == "packet_loss")
    assert loss_rec["p"] == pytest.approx(0.3)
    assert 0.6 < float(out.valid.mean()) < 0.8, "2400 个伯努利样本，0.3 丢包率偏差很小"
    assert loss_rec["loss_rate"] == pytest.approx(1.0 - float(out.valid.mean()))
    assert out.valid.dtype == bool
    print("PASS test_noise_jitter_and_packet_loss")


def test_noise_short_series_edges():
    """极短序列的边界：n=1 不做延迟平移且 dt 回退、n<=2 不做抖动、空 base_pos 保持为空。"""
    sim1 = _fake_sim(n_steps=1, seed=7)
    out1, rec1 = apply_noise(sim1, NoiseConfig(enable=True, jitter_p=0.5),
                             np.random.default_rng(2))
    lat = next(r for r in rec1 if r["noise"] == "latency")
    assert lat["steps"] == 1 and lat["dt"] == 0.0, "n<=1 时算不出 dt，应回退 0.0"
    assert "jitter" not in [r["noise"] for r in rec1], "n<=2 时跳过抖动"
    assert out1.pos.shape == (1, 12)
    assert abs(float(out1.temperature.mean()) - float(sim1.temperature.mean())) < 2.0

    sim2 = _fake_sim(n_steps=2, seed=8)
    out2, _ = apply_noise(sim2, NoiseConfig(enable=True), np.random.default_rng(2))
    assert np.allclose(out2.pos[1], out2.pos[0]), "n=2 且 d=1：后一行来自前一行"

    empty = _fake_sim(n_steps=5, seed=9)
    empty.base_pos = np.zeros((0, 3))
    empty.base_quat = np.zeros((0, 4))
    out3, _ = apply_noise(empty, NoiseConfig(enable=True), np.random.default_rng(2))
    assert out3.base_pos.size == 0
    assert out3.base_quat.size == 0
    # 记录当前真实行为（小不一致，未修）：base_pos 被 copy，base_quat 却是同一对象
    # （`getattr(sim, "base_quat", ...)` 直接透传）→ 下游若原地改 base_quat 会污染输入。
    assert out3.base_quat is empty.base_quat
    print("PASS test_noise_short_series_edges")


def test_noise_battery_sag_floor_and_off_switch():
    """大电流下母线电压被 0.7·V_oc 工程下限托住；关掉内阻则不产生该层也不再降额。"""
    sim = _fake_sim(n_steps=5, seed=10, torque_scale=0.0)
    sim.torque = np.full((5, 12), 30.0)          # |τ|/kt = 120 A/关节 → 总电流远超降额
    out, rec = apply_noise(sim, NoiseConfig(enable=True, cog_amp=0.0),
                           np.random.default_rng(3))
    assert out.v_bus.min() == pytest.approx(0.7 * 48.0), "不允许出现非物理的个位数母线电压"

    off, rec_off = apply_noise(sim, NoiseConfig(enable=True, cog_amp=0.0, r_int=0.0),
                               np.random.default_rng(3))
    assert "battery_sag" not in [r["noise"] for r in rec_off]
    assert np.allclose(off.v_bus, 48.0)
    # 齿槽关闭 + 电流测量关闭 → 电流严格等于 |τ|/kt（顺序 5/6 无副作用）
    clean = NoiseConfig(enable=True, cog_amp=0.0, r_int=0.0, cur_noise_a=0.0,
                        cur_bias_sigma=0.0, cur_gain_sigma=0.0,
                        temp_obs_sigma=0.0, temp_proc_sigma=0.0)
    out2, _ = apply_noise(sim, clean, np.random.default_rng(3))
    assert np.array_equal(out2.torque, sim.torque)
    assert np.allclose(out2.current, np.abs(sim.torque) / 0.25)
    assert np.allclose(out2.temperature, sim.temperature)
    print("PASS test_noise_battery_sag_floor_and_off_switch")


def test_noise_packet_loss_does_not_nan_signals():
    """记录当前真实行为（**注释与代码不符，未修**）：丢包只给掩码，不做 NaN 标记。

    `noise.py` 顺序 8 的注释写的是"总线丢包（NaN 标记无效步，由下游按 validmask 处理）"，
    但代码只构造了 `valid` 布尔掩码，**没有**把 pos/current/temperature 置 NaN。真正做
    NaN 的是 `to_sample.to_synth_sample` 的 F3 分支。这不是漏洞，但注释会误导下游以为
    "NaN 即丢包"，于是漏用 validmask 的消费者会把丢包点当成有效读数。
    """
    sim = _fake_sim(n_steps=20, seed=11)
    out, rec = apply_noise(sim, NoiseConfig(enable=True, loss_p=1.0),
                           np.random.default_rng(4))
    assert float(out.valid.mean()) == 0.0
    assert not np.isnan(out.current).any(), "丢包未产生 NaN（与注释宣称不符）"
    assert not np.isnan(out.temperature).any()
    assert not np.isnan(out.torque).any()
    assert rec[-1]["noise"] == "packet_loss" and rec[-1]["loss_rate"] == pytest.approx(1.0)
    print("PASS test_noise_packet_loss_does_not_nan_signals")


# ============================================================ conditions.py

def _cfg(fault: str | None = "F1_stall", **kw) -> TrialConfig:
    base = dict(
        trial_id=0, seed=1, freq=1.0, payload_kg=3.0, slope_deg=5.0, t_amb=25.0,
        v_bus_oc=48.0, wear_friction=1.0, wear_coulomb=1.0, wear_rth=1.0,
        fault=fault, severity=1.0,
        component="joint_motor.front_left_hip", gait_mode="free",
        duration_s=2.0, dt=0.01,
    )
    base.update(kw)
    return TrialConfig(**base)


def test_trial_config_gait_and_label():
    """一条 trial 的步态出口与 ground truth 标签：normal 无标签，故障标签取 FAULT_DEFS。"""
    cfg = _cfg(fault="F2_temp_rise", freq=1.5, gait_mode="suspended")
    g = cfg.gait()
    assert isinstance(g, GaitParams) and g.freq == 1.5 and g.mode == "suspended"

    lab = cfg.label()
    assert lab is not None
    assert lab["sample_id"] == "F2_temp_rise"
    assert lab["fault"] == "F2_temp_rise"
    assert lab["component"] == cfg.component
    assert lab["severity"] == FAULT_DEFS["F2_temp_rise"]["severity"]
    assert lab["desc"] == FAULT_DEFS["F2_temp_rise"]["desc"]
    assert lab["window"] == "00:00~00:01", "默认窗由 to_sample 覆写为真实窗"

    assert _cfg(fault=None, severity=0.0).label() is None
    print("PASS test_trial_config_gait_and_label")


def test_make_configs_deterministic_and_within_grid():
    """条件网格只能是预设取值，且同 seed 逐条可复现（数据集可重放的前提）。"""
    spec = SamplingSpec()
    a = make_configs(60, spec, seed=7)
    b = make_configs(60, spec, seed=7)
    assert a == b, "同 seed 必须逐条相同"
    assert [c.trial_id for c in a] == list(range(60))
    assert a != make_configs(60, spec, seed=8)

    for c in a:
        assert c.freq in {0.0, 0.5, 1.0, 1.5, 2.0}
        assert c.payload_kg in {0.0, 3.0, 8.0, 15.0}
        assert c.slope_deg in {0.0, -5.0, 5.0, -10.0, 10.0}
        assert c.t_amb in {15.0, 25.0, 35.0, 45.0}
        assert c.v_bus_oc in {52.0, 48.0, 44.0, 40.0}
        assert c.wear_friction in {1.0, 1.3, 1.8}
        assert c.wear_coulomb in {1.0, 1.5, 2.0}
        assert c.wear_rth in {1.0, 1.2, 1.4}
        assert c.gait_mode == spec.gait_mode
        assert c.duration_s == spec.duration_s and c.dt == spec.dt
        assert 0 <= c.seed < 2 ** 31 - 1
    assert make_configs(0, spec, seed=1) == []
    print("PASS test_make_configs_deterministic_and_within_grid")


def test_make_configs_layers_normal_and_faults():
    """分层占比：normal 逼近 normal_frac；故障条的严重度/部件落在预设集合内。

    normal 占比是"误报率可评估"的前提，因此既查总量也查 normal 条本身的规范形态
    （fault=None、severity=0、部件取第一个）。
    """
    spec = SamplingSpec(normal_frac=0.35, severities=(0.5, 0.8, 1.0))
    cfgs = make_configs(600, spec, seed=3)
    normals = [c for c in cfgs if c.fault is None]
    assert 180 <= len(normals) <= 240, f"normal 占比应≈35%，实际 {len(normals)}/600"
    for c in normals:
        assert c.severity == 0.0 and c.component == FAULT_COMPONENTS[0]

    faulty = [c for c in cfgs if c.fault is not None]
    assert faulty, "600 条里应有故障样本"
    for c in faulty:
        assert c.fault in FAULTS
        assert c.severity in spec.severities
        assert c.component in FAULT_COMPONENTS
    assert {c.fault for c in faulty} == set(FAULTS), "默认权重下五类故障都该出现"
    print("PASS test_make_configs_layers_normal_and_faults")


def test_make_configs_fault_weight_controls_distribution():
    """`fault_weight` 是分层的旋钮：权重 0 的故障绝不出现，只留一个键则全采它。"""
    spec = SamplingSpec(fault_weight={f: (0.0 if f == "F1_stall" else 1.0) for f in FAULTS})
    cfgs = make_configs(400, spec, seed=4)
    faults = {c.fault for c in cfgs if c.fault}
    assert "F1_stall" not in faults
    assert faults == set(FAULTS) - {"F1_stall"}

    only = SamplingSpec(fault_weight={"F2_temp_rise": 1.0})
    cfgs2 = make_configs(50, only, seed=5)
    assert {c.fault for c in cfgs2 if c.fault} == {"F2_temp_rise"}
    print("PASS test_make_configs_fault_weight_controls_distribution")


def test_config_dict_round_trip():
    """manifest 的序列化口径：`config_to_dict` → `config_from_dict` 必须无损。"""
    cfg = _cfg(fault="F5_load_spike", severity=0.5, gait_mode="micro_swing")
    d = config_to_dict(cfg)
    assert isinstance(d, dict) and d["fault"] == "F5_load_spike"
    assert config_from_dict(d) == cfg
    assert config_from_dict(json.loads(json.dumps(d))) == cfg, "须能过 JSON 往返"
    print("PASS test_config_dict_round_trip")


# ============================================================ to_sample.py

_MAPPED = ["joint_motor.front_left_knee", "joint_motor.front_right_knee",
           "joint_motor.front_left_hip", "joint_motor.front_right_hip"]


def test_to_sample_normal_mapping_and_structure():
    """正常样本：4 个 Agent 认识的部件的映射顺序、列取值、dt、有效性掩码都要对。"""
    sim = _fake_sim(n_steps=200, seed=21)
    s = to_synth_sample(sim, sample_id="mj_test")
    assert s.sample_id == "mj_test"
    assert s.joints == _MAPPED
    assert s.n_steps == 200 and s.dt == pytest.approx(0.01)
    assert s.ground_truth is None
    assert np.allclose(s.valid_mask, 1.0)
    # 映射来源：FL_thigh→front_left_knee、FR_thigh→front_right_knee、FL_hip/FR_hip→髋
    for col, actuator in enumerate(["FL_thigh", "FR_thigh", "FL_hip", "FR_hip"]):
        j = sim.joints.index(actuator)
        assert np.array_equal(s.torque[:, col], sim.torque[:, j])
        assert np.array_equal(s.current[:, col], sim.current[:, j])
        assert np.array_equal(s.temperature[:, col], sim.temperature[:, j])
    print("PASS test_to_sample_normal_mapping_and_structure")


def test_to_sample_fault_window_and_ground_truth():
    """故障窗 = [40%, 90%)；ground truth 的窗口字符串由同一口径算出。"""
    sim = _fake_sim(n_steps=200, seed=22)
    s = to_synth_sample(sim, active_fault="F1_stall",
                        fault_component="joint_motor.front_left_hip")
    gt = s.ground_truth
    assert gt is not None
    assert gt["sample_id"] == "F1_stall" and gt["fault"] == "F1_stall"
    assert gt["component"] == "joint_motor.front_left_hip"
    assert gt["severity"] == FAULT_DEFS["F1_stall"]["severity"]
    assert gt["desc"] == FAULT_DEFS["F1_stall"]["desc"]
    assert gt["window"] == "00:00~00:01", "200 步 × 0.01s → 0.0s~1.8s 取整后为 00:00~00:01"
    # 窗口外保持原值，窗口内等于堵转力矩
    j = s.joints.index("joint_motor.front_left_hip")
    assert s.torque[0, j] == pytest.approx(sim.torque[0, sim.joints.index("FL_hip")])
    assert np.allclose(s.torque[80:180, j], DEFAULT_PARAMS["stall_torque"])
    assert np.allclose(s.current[80:180, j], 23.7 / 0.25 + 2.0)
    assert s.temperature[179, j] > sim.temperature[79, sim.joints.index("FL_hip")]
    print("PASS test_to_sample_fault_window_and_ground_truth")


def test_to_sample_fault_injection_matrix():
    """五类故障的注入语义：各自改哪个量、参照窗是否干净分离。

    重点在 F4：参照窗由 generator 取"故障前"段（REF_WINDOW_END_FRAC < FAULT_START_FRAC），
    所以偏置必须只加在故障窗，故障前电流必须保持基线——否则"偏置"会被基线稀释。
    """
    sim = _fake_sim(n_steps=200, seed=23)
    j_sim = sim.joints.index("FL_hip")

    # F5 负载骤增：在基线上叠加 60% 堵转力矩
    s5 = to_synth_sample(sim, active_fault="F5_load_spike",
                         fault_component="joint_motor.front_left_hip")
    j5 = s5.joints.index("joint_motor.front_left_hip")
    assert np.allclose(s5.torque[80:180, j5],
                       sim.torque[80:180, j_sim] + 23.7 * 0.6)
    assert not np.allclose(s5.torque[0:80, j5], sim.torque[0:80, j_sim] + 23.7 * 0.6)

    # F4 电流偏置：故障窗 +5A，窗内温度随之演化（参照窗保持不变）
    s4 = to_synth_sample(sim, active_fault="F4_current_bias",
                         fault_component="joint_motor.front_left_hip")
    j4 = s4.joints.index("joint_motor.front_left_hip")
    assert np.allclose(s4.current[80:180, j4], sim.current[80:180, j_sim] + 5.0)
    assert np.allclose(s4.current[0:80, j4], sim.current[0:80, j_sim]), "故障前必须是干净基线"
    assert np.array_equal(s4.torque, sim.torque[:, [sim.joints.index(a) for a in
                                                    ["FL_thigh", "FR_thigh", "FL_hip",
                                                     "FR_hip"]]])

    # F2 热退化：电流 = |τ|/kt + 0.3，且窗内温度单调爬升
    s2 = to_synth_sample(sim, active_fault="F2_temp_rise",
                         fault_component="joint_motor.front_left_hip")
    j2 = s2.joints.index("joint_motor.front_left_hip")
    assert np.allclose(s2.current[80:180, j2], np.abs(s2.torque[80:180, j2]) / 0.25 + 0.3)
    assert s2.temperature[179, j2] > s2.temperature[80, j2]

    # F3 丢包：只在故障窗内、只作用于该列
    s3 = to_synth_sample(sim, active_fault="F3_packet_loss",
                         fault_component="joint_motor.front_left_hip",
                         rng=np.random.default_rng(1))
    j3 = s3.joints.index("joint_motor.front_left_hip")
    dropped = np.flatnonzero(s3.valid_mask[:, j3] == 0.0)
    assert dropped.size > 0
    assert dropped.min() >= 80 and dropped.max() < 180, "丢弃点必须落在故障窗内"
    assert np.isnan(s3.current[dropped, j3]).all()
    assert np.isnan(s3.torque[dropped, j3]).all()
    assert np.isnan(s3.temperature[dropped, j3]).all()
    assert np.allclose(s3.valid_mask[:, 0], 1.0), "其它部件不受影响"
    print("PASS test_to_sample_fault_injection_matrix")


def test_to_sample_severity_scaling_and_unknown_component_fallback():
    """severity 线性缩放注入幅度；未知部件名回退到第一个映射部件并同步改 ground truth。"""
    sim = _fake_sim(n_steps=200, seed=24)
    weak = to_synth_sample(sim, active_fault="F1_stall", severity=0.2,
                           fault_component="joint_motor.front_left_hip")
    j = weak.joints.index("joint_motor.front_left_hip")
    assert np.allclose(weak.torque[80:180, j], 23.7 * 0.2), "亚阈值负例：幅度按 severity 缩"

    strong = to_synth_sample(sim, active_fault="F1_stall", severity=2.0,
                             fault_component="joint_motor.front_left_hip")
    # 记录当前真实行为（无入参校验，未修）：docstring 说 severity∈0..1，但 2.0 不会被夹住，
    # 也不报错 → 会造出"两倍堵转力矩"这种非物理样本。调用方需自守。
    assert np.allclose(strong.torque[80:180, j], 23.7 * 2.0)

    fb = to_synth_sample(sim, active_fault="F1_stall",
                         fault_component="joint_motor.front_right_elbow")
    assert fb.ground_truth is not None
    assert fb.ground_truth["component"] == _MAPPED[0], "未知部件回退到第一个映射部件"
    assert np.allclose(fb.torque[80:180, 0], DEFAULT_PARAMS["stall_torque"])
    print("PASS test_to_sample_severity_scaling_and_unknown_component_fallback")


def test_to_sample_normal_fault_strings_and_noise_path():
    """`None`/空串/`"normal"` 都表示无故障；`noise=True` 先加噪再注入且可复现。"""
    sim = _fake_sim(n_steps=120, seed=25)
    for f in (None, "", "normal"):
        s = to_synth_sample(sim, active_fault=f)
        assert s.ground_truth is None and np.allclose(s.valid_mask, 1.0)

    noisy = to_synth_sample(sim, active_fault="F1_stall", noise=True,
                            fault_component="joint_motor.front_left_hip",
                            rng=np.random.default_rng(9))
    noisy2 = to_synth_sample(sim, active_fault="F1_stall", noise=True,
                             fault_component="joint_motor.front_left_hip",
                             rng=np.random.default_rng(9))
    assert np.array_equal(noisy.temperature, noisy2.temperature)
    assert np.array_equal(noisy.torque, noisy2.torque)
    j = noisy.joints.index("joint_motor.front_left_hip")
    # 加噪后再注入：故障窗内仍是堵转力矩（注入覆盖噪声）
    assert np.allclose(noisy.torque[48:108, j], DEFAULT_PARAMS["stall_torque"])
    # rng=None 时内部回退固定种子 → 也要能跑通且确定性
    auto = to_synth_sample(sim, active_fault="F4_current_bias", noise=True,
                           fault_component="joint_motor.front_left_hip")
    auto2 = to_synth_sample(sim, active_fault="F4_current_bias", noise=True,
                            fault_component="joint_motor.front_left_hip")
    assert np.array_equal(auto.current, auto2.current)
    print("PASS test_to_sample_normal_fault_strings_and_noise_path")


def test_to_sample_single_step_and_valid_mask_merge():
    """n_steps=1 时 dt 回退 0.01（不越界取值）；噪声层的丢包掩码按列并入 valid_mask。"""
    one = _fake_sim(n_steps=1, seed=26)
    s = to_synth_sample(one)
    assert s.dt == pytest.approx(0.01) and s.n_steps == 1

    sim = _fake_sim(n_steps=10, seed=27)
    sim.valid = np.ones((10, 12), dtype=bool)
    sim.valid[3, sim.joints.index("FL_hip")] = False
    s2 = to_synth_sample(sim)
    assert s2.valid_mask[3, 2] == 0.0
    assert s2.valid_mask[3, 0] == 1.0
    print("PASS test_to_sample_single_step_and_valid_mask_merge")


def test_make_mujoco_batch_without_simulation(monkeypatch):
    """批次组装（默认故障清单、sample_id 命名、共享同一段仿真）用桩 simulate 验证。"""
    from data_dog.mujoco_sim import simulate as simulate_mod
    calls = []

    def fake_simulate(**kw):
        calls.append(kw)
        return _fake_sim(n_steps=100, seed=28)

    monkeypatch.setattr(simulate_mod, "simulate", fake_simulate)
    batch = make_mujoco_batch(fault_component="joint_motor.front_left_hip")
    assert len(batch) == 6
    assert [s.ground_truth["fault"] if s.ground_truth else None for s in batch] == [
        None, "F1_stall", "F2_temp_rise", "F3_packet_loss", "F4_current_bias", "F5_load_spike"]
    assert [s.sample_id for s in batch] == [
        "mj_normal_0", "mj_F1_stall_1", "mj_F2_temp_rise_2",
        "mj_F3_packet_loss_3", "mj_F4_current_bias_4", "mj_F5_load_spike_5"]
    assert calls == [{"duration_s": 2.0, "dt": 0.01, "seed": 0}], "只跑一段仿真"

    only = make_mujoco_batch(faults=["normal"], duration_s=1.0, dt=0.02, seed=5)
    assert len(only) == 1 and only[0].sample_id == "mj_normal_0"
    assert calls[-1] == {"duration_s": 1.0, "dt": 0.02, "seed": 5}
    print("PASS test_make_mujoco_batch_without_simulation")


# ============================================================ gen_dataset 分片

def test_shard_writes_jsonl_atomically_and_resume_skips(tmp_path, monkeypatch):
    """分片落盘是"临时文件 + 原子替换 + .done 标记"；已完成分片续跑时直接跳过。"""
    from data_dog.mujoco_sim import gen_dataset
    monkeypatch.setattr(gen_dataset, "run_trial",
                        lambda cfg: {"trial_id": cfg.trial_id, "fault": cfg.fault})
    cfgs = make_configs(3, SamplingSpec(duration_s=1.0), seed=0)
    job = (0, [asdict(c) for c in cfgs], str(tmp_path))

    sid, n = gen_dataset._run_shard(job)
    assert (sid, n) == (0, 3)
    shard = tmp_path / "shard_00000.jsonl"
    assert shard.exists()
    assert not (tmp_path / "shard_00000.jsonl.tmp").exists(), "临时文件必须被原子替换掉"
    assert (tmp_path / ".done" / "00000.ok").read_text(encoding="utf-8") == "ok"
    recs = [json.loads(line) for line in shard.read_text(encoding="utf-8").splitlines()]
    assert [r["trial_id"] for r in recs] == [0, 1, 2]
    content = shard.read_text(encoding="utf-8")

    # 断点续跑：已完成的 shard 返回 -1，且不重写文件
    sid2, n2 = gen_dataset._run_shard(job)
    assert (sid2, n2) == (0, -1)
    assert shard.read_text(encoding="utf-8") == content
    print("PASS test_shard_writes_jsonl_atomically_and_resume_skips")


def test_generate_splits_trials_into_fixed_size_shards(tmp_path, monkeypatch):
    """`generate` 按 shard_size 切片分派：1200 条 / 500 → [500, 500, 200]。

    这里把 `gen_dataset.time` 换成自增的计时器：一是让用例不依赖真实耗时，二是绕开
    `generate` 末尾 `trials / dt` 的零除问题（该缺陷单独用一条用例钉住，见下一条）。
    """
    from data_dog.mujoco_sim import gen_dataset
    seen: list[tuple[int, int]] = []

    def fake_shard(job):
        seen.append((job[0], len(job[1])))
        return job[0], len(job[1])

    ticks = iter(range(1, 1000))
    monkeypatch.setattr(gen_dataset, "time", SimpleNamespace(time=lambda: float(next(ticks))))
    monkeypatch.setattr(gen_dataset, "_run_shard", fake_shard)
    out = gen_dataset.generate(1200, workers=1, out_dir=str(tmp_path), seed=0,
                               shard_size=500, spec=SamplingSpec(duration_s=1.0))
    assert out == str(tmp_path)
    assert seen == [(0, 500), (1, 500), (2, 200)]
    print("PASS test_generate_splits_trials_into_fixed_size_shards")


def test_generate_zero_elapsed_throughput_crashes_current_behavior(tmp_path, monkeypatch):
    """记录当前真实行为（**缺陷，未修**）：分片吞吐统计未防零除。

    `generate` 末尾用 `trials / (time.time() - t0)` 算吞吐。当所有分片都**立即**返回时
    （最典型就是 `--resume` 下 `.done` 标记全都已存在，`_run_shard` 直接 return -1），
    经过时间可能落在时钟粒度之内等于 0.0 → 整个 `generate` 在**所有数据都已成功落盘
    之后**抛 ZeroDivisionError，命令以异常收尾。断点续跑是它的主打场景，这个崩溃点很刺眼。
    修法：`dt = max(dt, 1e-9)` 或先判零。本用例用"时钟恒为 0"确定性地复现它。
    """
    from data_dog.mujoco_sim import gen_dataset
    monkeypatch.setattr(gen_dataset, "_run_shard", lambda job: (job[0], -1))
    monkeypatch.setattr(gen_dataset, "time", SimpleNamespace(time=lambda: 0.0))
    with pytest.raises(ZeroDivisionError):
        gen_dataset.generate(10, workers=1, out_dir=str(tmp_path), seed=0,
                             shard_size=5, spec=SamplingSpec(duration_s=1.0))
    print("PASS test_generate_zero_elapsed_throughput_crashes_current_behavior")


def test_get_model_applies_conditions_and_resets_from_baseline():
    """`_get_model` 把一条 trial 的工况写进 MjModel：负载/坡度/磨损，且每次都从基线重置。

    这里**只加载 XML 并改数组**，不 `mj_step`（不是仿真，秒级以内）。它值得单独测，因为
    "从基线重置"是防止跨 trial 污染的关键：`_MODEL_CACHE` 是进程内单例，若某次工况残留
    （例如上一次的坡度重力没被重置），后续所有 trial 的物理都会静默偏移。
    """
    pytest.importorskip("mujoco")           # 无 mujoco 时不影响纯逻辑用例
    from data_dog.mujoco_sim.simulate import _MODEL_BASELINE, _get_model
    if not DEFAULT_MODEL.exists():
        pytest.skip(f"缺少 Go2 模型文件：{DEFAULT_MODEL}")

    plain = _get_model(DEFAULT_MODEL, 0.0, 0.0)
    base = _MODEL_BASELINE[str(DEFAULT_MODEL)]
    assert plain.nu == len(ACTUATORS), "执行器数必须与 ACTUATORS 对齐"
    assert _get_model(DEFAULT_MODEL, 0.0, 0.0) is plain, "同一模型只缓存一个实例"

    loaded = _get_model(DEFAULT_MODEL, 15.0, 10.0, 1.3, 2.0)
    assert loaded.body_mass[1] == pytest.approx(base["body_mass"][1] + 15.0), "负载加在基座"
    g = 9.81
    assert loaded.opt.gravity[0] == pytest.approx(-g * np.sin(np.deg2rad(10.0)))
    assert loaded.opt.gravity[2] == pytest.approx(-g * np.cos(np.deg2rad(10.0)))
    nonzero = base["dof_frictionloss"] > 0
    assert nonzero.any(), "该模型确实有非零库仑摩擦项（否则磨损乘子会变成空操作）"
    assert np.allclose(loaded.geom_friction[:, 0], base["geom_friction"][:, 0] * 1.3)
    assert np.allclose(loaded.dof_frictionloss[nonzero],
                       base["dof_frictionloss"][nonzero] * 2.0)

    again = _get_model(DEFAULT_MODEL, 0.0, 0.0)
    assert np.allclose(again.body_mass, base["body_mass"]), "下次调用必须回到基线质量"
    assert np.allclose(again.opt.gravity, base["gravity"]), "重力必须被重置（否则坡度残留）"
    assert np.allclose(again.geom_friction, base["geom_friction"])
    assert np.allclose(again.dof_frictionloss, base["dof_frictionloss"])
    print("PASS test_get_model_applies_conditions_and_resets_from_baseline")


def test_gen_dataset_main_wires_cli_args_to_generate(monkeypatch, tmp_path):
    """命令行入口：CLI 参数必须原样落到 `generate`（分片大小/时长/保真度档/种子）。"""
    from data_dog.mujoco_sim import gen_dataset
    seen: dict = {}

    def fake_generate(trials, workers, out_dir, seed=0, shard_size=500, spec=None):
        seen.update(trials=trials, workers=workers, out_dir=out_dir, seed=seed,
                    shard_size=shard_size, duration_s=spec.duration_s,
                    gait_mode=spec.gait_mode)
        return out_dir

    monkeypatch.setattr(gen_dataset, "generate", fake_generate)
    monkeypatch.setattr(sys, "argv", [
        "gen_dataset", "--trials", "20", "--workers", "1", "--out", str(tmp_path),
        "--shard-size", "7", "--duration", "3.5", "--gait-mode", "suspended", "--seed", "9"])
    gen_dataset.main()
    assert seen == {"trials": 20, "workers": 1, "out_dir": str(tmp_path), "seed": 9,
                    "shard_size": 7, "duration_s": 3.5, "gait_mode": "suspended"}
    print("PASS test_gen_dataset_main_wires_cli_args_to_generate")

