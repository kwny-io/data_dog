"""CWRU 数据装载：采样率分组 + DE 变量选取——两个**已翻车过**的点，钉死。

为什么单独一组
--------------
1. **采样率不是全库一个数**：正常基线是 48 kHz，12k 驱动端故障是 12 kHz。
   旧实现把所有文件都按 12 kHz 算包络 → normal 的 env_* 落在 4× 偏的频带上，
   跨工况比较因此失真（详见 scripts/cwru_eval.py 文件头与 docs/cross_dataset_cwru.md §8）。
2. **99.mat 是"两条记录一个文件"**：它同时含 X098_DE_time 与 X099_DE_time。
   旧实现取"第一个含 DE 的变量" → op_2/normal 实际读到 **98.mat 的同一条信号**，
   于是 op_1 与 op_2 两折的 normal **完全重复**（fold 不独立，聚合误报率被同一段数据重复计权）。

两个都不需要真实数据集文件就能钉住。
"""
from __future__ import annotations

import importlib.util
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_script(name: str):
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_dd_{name}", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_file_sample_rate_matches_official_groups():
    m = _load_script("cwru_eval")
    # 正常基线（官方 Normal Baseline 页，97~100）→ 48 kHz
    for n in (97, 98, 99, 100):
        assert m.file_sample_rate(f"op_0/normal/{n}.mat") == 48000.0, n
    # 12k 驱动端故障 → 12 kHz
    for n in (105, 106, 118, 130, 169, 185, 197, 222, 234):
        assert m.file_sample_rate(f"op_0/inner_07/{n}.mat") == 12000.0, n
    # 48k 驱动端故障（官方 48k 页号段）→ 48 kHz
    for n in (109, 122, 135, 174, 189, 201, 213, 226, 238):
        assert m.file_sample_rate(f"op_0/inner_07/{n}.mat") == 48000.0, n
    # 敏感性开关：把正常基线按 12 kHz 处理（仅用于对照，不是默认）
    assert m.file_sample_rate("op_0/normal/97.mat", 12000.0) == 12000.0
    print("PASS test_file_sample_rate_matches_official_groups")


def test_de_key_picks_record_matching_file_number():
    m = _load_script("cwru_eval")
    vars_ = {"X098_DE_time": 1, "X098_FE_time": 2, "X099_DE_time": 3, "X099_FE_time": 4}
    # 99.mat 里必须先取 X099_DE_time——取到 X098 就意味着 op_2 的 normal 复制了 op_1 的
    assert m.de_key(vars_, "op_2/normal/99.mat") == "X099_DE_time"
    assert m.de_key(vars_, "op_1/normal/98.mat") == "X098_DE_time"
    # 单变量文件与无编号文件都要有兜底
    assert m.de_key({"X105_DE_time": 1, "X105_FE_time": 2}, "op_0/inner_07/105.mat") == "X105_DE_time"
    assert m.de_key({"X105_DE_time": 1}, "weird.mat") == "X105_DE_time"
    assert m.de_key({"X105_FE_time": 1}, "op_0/inner_07/105.mat") is None
    print("PASS test_de_key_picks_record_matching_file_number")


def test_load_windows_does_not_duplicate_op1_op2_normal():
    """有数据就做一次真装载：op_1 与 op_2 的 normal 窗口**不能**逐点相同。"""
    m = _load_script("cwru_eval")
    data = ROOT / "data" / "cwru"
    if not (data / "op_2" / "normal" / "99.mat").exists():   # pragma: no cover
        print("SKIP test_load_windows_does_not_duplicate_op1_op2_normal（无 data/cwru）")
        return
    wins = m.load_windows(data, per_file=1, wlen=24000, stride=24000)
    assert wins and all(len(w) == 4 for w in wins)          # (label, load, window, fs)
    n1 = [w[2] for w in wins if w[0] == "normal" and w[1] == 1]
    n2 = [w[2] for w in wins if w[0] == "normal" and w[1] == 2]
    assert n1 and n2
    assert not (n1[0].shape == n2[0].shape and (n1[0] == n2[0]).all()), "op_2 normal 又复制了 op_1"
    fs = {w[1]: w[3] for w in wins if w[0] == "normal"}
    assert fs == {0: 48000.0, 1: 48000.0, 2: 48000.0, 3: 48000.0}, fs
    print("PASS test_load_windows_does_not_duplicate_op1_op2_normal")
