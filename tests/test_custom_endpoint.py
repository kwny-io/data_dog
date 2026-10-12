"""自带模型 / 网关（自定义 OpenAI 兼容端点）：DD_LLM_VENDOR=custom。

产品落地时使用者自带模型与端点，不改代码即可接入；这也是多模态（视觉通道）能落地的
前提。钉住：端点解析（settings / .env 两条路）、规范名兼容、base_url 覆盖、缺端点报错。

约定：每个 test 结尾 print("PASS <name>")。
"""
from __future__ import annotations

import pytest

from data_dog.providers.client import Provider
from data_dog.providers.config import load_config
from data_dog.providers.registry import CUSTOM_VENDOR_ID, get_vendor

BASE = "https://gw.example/v1"


def test_custom_vendor_reads_settings(monkeypatch):
    monkeypatch.setenv("DD_LLM_BASE_URL", BASE)
    monkeypatch.setenv("DD_LLM_MODEL", "some-vl-model")
    spec = get_vendor(CUSTOM_VENDOR_ID)
    assert spec.base_url == BASE
    assert spec.default_model == "some-vl-model"
    assert spec.requires_env_key == "DD_LLM_API_KEY"
    print("PASS test_custom_vendor_reads_settings")


def test_custom_vendor_falls_back_to_env_file(monkeypatch, tmp_path):
    for k in ("DD_LLM_BASE_URL", "DD_LLM_MODEL"):
        monkeypatch.delenv(k, raising=False)
    envfile = tmp_path / ".env"
    envfile.write_text(f"DD_LLM_BASE_URL={BASE}\nDD_LLM_MODEL=m-from-file\n",
                       encoding="utf-8")
    monkeypatch.setattr("data_dog.providers.config.load_env", lambda root=None: (
        {"DD_LLM_BASE_URL": BASE, "DD_LLM_MODEL": "m-from-file"}, tmp_path))
    spec = get_vendor(CUSTOM_VENDOR_ID)
    assert spec.base_url == BASE and spec.default_model == "m-from-file"
    print("PASS test_custom_vendor_falls_back_to_env_file")


def test_load_config_canonical_names(tmp_path):
    (tmp_path / ".env").write_text(
        "DD_LLM_VENDOR=custom\n"
        f"DD_LLM_BASE_URL={BASE}\n"
        "DD_LLM_API_KEY=sk-test-123456\n"
        "DD_LLM_MODEL=m1\n", encoding="utf-8")
    cfg = load_config(tmp_path)
    assert cfg["vendor_id"] == "custom"
    assert cfg["base_url"] == BASE
    assert cfg["model"] == "m1"
    assert cfg["api_key"] == "sk-test-123456"
    # 历史别名仍可读
    (tmp_path / ".env").write_text(
        "DATA_DOG_VENDOR=deepseek\nDEEPSEEK_API_KEY=sk-old\nDATA_DOG_MODEL=deepseek-chat\n",
        encoding="utf-8")
    cfg2 = load_config(tmp_path)
    assert cfg2["vendor_id"] == "deepseek" and cfg2["model"] == "deepseek-chat"
    assert cfg2["base_url"] == ""
    print("PASS test_load_config_canonical_names")


def test_from_config_overrides_base_url():
    p = Provider.from_config(CUSTOM_VENDOR_ID, "sk-test-123456", base_url=BASE)
    assert p.vendor.base_url == BASE
    print("PASS test_from_config_overrides_base_url")


def test_from_config_custom_requires_base_url(monkeypatch, tmp_path):
    monkeypatch.delenv("DD_LLM_BASE_URL", raising=False)
    monkeypatch.delenv("DATA_DOG_BASE_URL", raising=False)
    monkeypatch.setattr("data_dog.providers.config.load_env", lambda root=None: ({}, tmp_path))
    with pytest.raises(ValueError):
        Provider.from_config(CUSTOM_VENDOR_ID, "sk-test-123456")
    print("PASS test_from_config_custom_requires_base_url")
