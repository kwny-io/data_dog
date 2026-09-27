"""资源路径解析回归测试。

背景（真实 bug）：各模块曾用 `Path(__file__).resolve().parents[3]` 推"仓库根"。
源码态对，但 `pip install` 后代码进了 site-packages，上溯 3 层不再是仓库根，
于是 `vendor_docs/` 找不到 → 知识包为空 → 过温算子规则缺失 → 服务**静默**不产候选。
这个 bug 只在 Docker 实跑时暴露，本地测试全绿也抓不到。

本测试锁定两件事：
    1. 路径解析**由环境变量驱动**，不再依赖 __file__ 的相对层级。
    2. 算子规则真的来自厂商文档（缺文档时必须能被察觉，而不是静默降级）。
"""
from __future__ import annotations

from pathlib import Path

from data_dog.paths import operators_dir, project_root, vendor_docs_dir

# ---------------------------------------------------------------- 路径解析

def test_vendor_docs_dir_respects_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DD_VENDOR_DOCS", str(tmp_path))
    assert vendor_docs_dir() == tmp_path


def test_operators_dir_respects_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DD_OPERATORS_DIR", str(tmp_path))
    assert operators_dir() == tmp_path


def test_project_root_respects_env(monkeypatch, tmp_path):
    monkeypatch.setenv("DD_PROJECT_ROOT", str(tmp_path))
    assert project_root() == tmp_path


def test_resolution_is_not_file_layout_dependent(monkeypatch, tmp_path):
    """把文档根指到一个"非仓库层级"的目录，解析也必须跟随——证明不靠 __file__。"""
    fake = tmp_path / "somewhere" / "else"
    (fake / "unitree" / "cleaned").mkdir(parents=True)
    monkeypatch.setenv("DD_VENDOR_DOCS", str(fake))

    from data_dog.data_plane.p2_demo import cleaned_dir
    assert cleaned_dir() == fake / "unitree" / "cleaned"


# ---------------------------------------------------------------- 算子规则来源

def test_detector_config_loads_overheat_rule_from_vendor_docs():
    """厂商文档在位时，过温规则(≥90 critical)必须被加载。"""
    from data_dog.data_plane.p2_demo import _detector_config
    cfg = _detector_config()
    ids = {r.rule_id for r in cfg.rules}
    assert "MOTOR_TEMP_PROTECT" in ids, f"过温规则缺失，只有: {ids}"


def test_missing_vendor_docs_degrades_detectably(monkeypatch, tmp_path):
    """厂商文档缺失时，规则退化为兜底（1 条）——说明会"降级"，但必须可观测。

    这条断言是防回归的关键：如果哪天解析又退回 __file__ 相对路径、
    在安装态下静默丢文档，这里会从 2 条掉到 1 条而暴露。
    """
    monkeypatch.setenv("DD_VENDOR_DOCS", str(tmp_path / "empty"))
    (tmp_path / "empty").mkdir()
    from data_dog.data_plane.p2_demo import _detector_config
    cfg = _detector_config()
    ids = {r.rule_id for r in cfg.rules}
    assert "MOTOR_TEMP_PROTECT" not in ids      # 无文档 → 无过温规则
    assert "FIELD_SANITY_TEMP_INVALID" in ids   # 兜底规则仍在


def test_dockerfile_sets_vendor_docs_env():
    """镜像必须显式告知文档位置，否则安装态下又会找不到。"""
    root = Path(__file__).resolve().parents[1]
    dockerfile = root / "Dockerfile"
    assert dockerfile.exists()
    text = dockerfile.read_text(encoding="utf-8")
    assert "DD_VENDOR_DOCS" in text or "DD_PROJECT_ROOT" in text
