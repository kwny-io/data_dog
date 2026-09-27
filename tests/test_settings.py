"""集中配置（settings.py）测试。

重点：**调用时读取、绝不缓存**——测试大量用 monkeypatch.setenv 后再调用，
若在 import 时求值就会全部失效。别名兼容（DATA_DOG_* → DD_*）也要守住。
"""
from __future__ import annotations

from data_dog import settings


def test_defaults(monkeypatch):
    for k in ("DD_DEFAULT_TENANT", "DD_SERVICE_NAME", "DD_LLM_VENDOR",
              "DATA_DOG_VENDOR", "DD_LLM_MODEL", "DATA_DOG_MODEL"):
        monkeypatch.delenv(k, raising=False)
    assert settings.default_tenant() == "demo"
    assert settings.service_name() == "data-dog"
    assert settings.llm_vendor() == "deepseek"
    assert settings.llm_model() == ""


def test_reads_at_call_time_not_import(monkeypatch):
    """import 之后再改环境变量也要生效（不缓存）。"""
    monkeypatch.delenv("DD_SERVICE_PORT", raising=False)
    assert settings.service_port() == 8000
    monkeypatch.setenv("DD_SERVICE_PORT", "9123")
    assert settings.service_port() == 9123


def test_blank_counts_as_unset(monkeypatch):
    """显式空串视为未设置，回落到默认（避免 DD_X="" 意外覆盖）。"""
    monkeypatch.setenv("DD_SERVICE_NAME", "   ")
    assert settings.service_name() == "data-dog"


def test_alias_fallback(monkeypatch):
    """规范名优先；未设时回落到历史别名。"""
    monkeypatch.delenv("DD_LLM_VENDOR", raising=False)
    monkeypatch.setenv("DATA_DOG_VENDOR", "legacy-vendor")
    assert settings.llm_vendor() == "legacy-vendor"
    monkeypatch.setenv("DD_LLM_VENDOR", "canonical")
    assert settings.llm_vendor() == "canonical"


def test_cors_origins_split(monkeypatch):
    monkeypatch.delenv("DD_CORS_ORIGINS", raising=False)
    assert settings.cors_origins() == []
    monkeypatch.setenv("DD_CORS_ORIGINS",
                       "https://a.example.com, https://b.example.com ,")
    assert settings.cors_origins() == ["https://a.example.com",
                                       "https://b.example.com"]


def test_bool_parsing(monkeypatch):
    for raw, expected in [("1", True), ("0", False), ("true", True),
                          ("false", False), ("no", False), ("off", False),
                          ("yes", True)]:
        monkeypatch.setenv("DD_AUTO_MIGRATE", raw)
        assert settings.auto_migrate() is expected, raw


def test_llm_timeout_zero_means_unlimited(monkeypatch):
    monkeypatch.setenv("DD_LLM_TIMEOUT_S", "0")
    assert settings.llm_timeout_s() is None
    monkeypatch.setenv("DD_LLM_TIMEOUT_S", "-5")
    assert settings.llm_timeout_s() is None
    monkeypatch.setenv("DD_LLM_TIMEOUT_S", "12.5")
    assert settings.llm_timeout_s() == 12.5
    monkeypatch.setenv("DD_LLM_TIMEOUT_S", "not-a-number")
    assert settings.llm_timeout_s() == 60.0     # 非法值回落默认，不抛
