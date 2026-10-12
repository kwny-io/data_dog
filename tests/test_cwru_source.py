"""CWRU 跨数据集证据源：解析器 / 特征 / 授权，三条都钉住（不依赖数据集文件）。"""
from __future__ import annotations

import struct

import numpy as np

from data_dog.evidence.cwru import BEARING_COMPONENT, CWRUEvidenceSource, features, read_mat_vector


def _tag(t: int, data: bytes) -> bytes:
    pad = (8 - len(data) % 8) % 8
    return struct.pack("<II", t, len(data)) + data + b"\x00" * pad


def _make_mat(name: str, arr: np.ndarray) -> bytes:
    """手工造一个最小 MAT v5（mxDOUBLE_CLASS），用来测解析器（无需下载数据集）。"""
    flags = _tag(4, struct.pack("<II", 6, 0))                 # mxClassID=6(double)
    dims = _tag(5, struct.pack("<ii", len(arr), 1))
    nm = _tag(1, name.encode("ascii"))
    data = _tag(9, arr.astype("<f8").tobytes())
    return b"\x00" * 128 + _tag(14, flags + dims + nm + data)


def test_read_mat_v5_roundtrip(tmp_path):
    arr = np.linspace(-1.5, 2.5, 1000)
    p = tmp_path / "x.mat"
    p.write_bytes(_make_mat("X105_DE_time", arr))
    name, got = read_mat_vector(p)
    assert name == "X105_DE_time"
    assert got.shape == (1000,) and np.allclose(got, arr)
    print("PASS test_read_mat_v5_roundtrip")


def test_features_separate_impulses_from_sine():
    t = np.linspace(0, 1, 12000, endpoint=False)
    sine = np.sin(2 * np.pi * 50 * t)
    imp = sine.copy()
    imp[::600] += 8.0                     # 周期冲击（轴承缺陷的典型形态）
    fs, fi = features(sine), features(imp)
    assert fi["kurtosis"] > fs["kurtosis"] * 2, (fs, fi)
    assert fi["crest_factor"] > fs["crest_factor"]
    print("PASS test_features_separate_impulses_from_sine")


def test_source_returns_metrics_and_denies_others():
    x = np.sin(np.linspace(0, 40, 12000))
    src = CWRUEvidenceSource(x)
    res = src.query_telemetry("device-cwru", BEARING_COMPONENT, "w0", "w0")
    for k in ("metrics", "reference_window_metrics"):
        assert k in res
    for k in ("rms", "kurtosis", "crest_factor"):
        assert k in res["metrics"]
    assert "ground_truth" not in res
    bad = src.query_telemetry("device-cwru", "joint_motor.front_left_hip", "w0", "w0")
    assert bad["ok"] is False and bad["error"] == "denied"
    bad2 = src.query_telemetry("other-device", BEARING_COMPONENT, "w0", "w0")
    assert bad2["ok"] is False
    print("PASS test_source_returns_metrics_and_denies_others")
