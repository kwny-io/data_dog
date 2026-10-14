"""配置体检（`data-dog config check`）：把"配错了但没人知道"变成红/黄灯。

为什么单独一组
-------------
这类检查最容易做成"看起来在检查"。这里只钉**能用环境变量判定**的判定：未知键、
非法数值、坏 JSON、危险组合——它们此前全都是**静默**的（`settings` 回落默认值）。
"""
from __future__ import annotations

from data_dog.config_check import check, known_keys, render, typed_keys


def _keys(report: dict, level: str) -> set[str]:
    return {item["key"] for item in report[level]}


def test_unknown_key_is_an_error():
    """拼错的键此前被完全忽略（程序照默认值跑）——必须报错。"""
    report = check({"DD_TYPO_KEY": "1"})
    assert not report["ok"]
    assert _keys(report, "errors") == {"DD_TYPO_KEY"}


def test_bad_number_is_an_error():
    """数值写错时 _get_int/_get_float 会静默回落默认值——要在部署前拦住。"""
    report = check({"DD_SERVICE_PORT": "80x"})
    assert "DD_SERVICE_PORT" in _keys(report, "errors")


def test_typed_keys_come_from_settings_source():
    """数值键的清单从 settings.py 解析而来（代码即真源），且都必须是已知键。"""
    typed = typed_keys()
    assert len(typed) >= 40, "settings.py 的 _get_int/_get_float 没被解析到"
    assert typed["DD_SERVICE_PORT"] == "int"
    assert all(key in known_keys() for key in typed)


def test_bad_json_and_bad_enum_are_errors():
    """坏 JSON / 非法枚举本来就是失败关闭的（503），体检要提前说清是哪一项。"""
    report = check({"DD_TENANT_KEYS": "{bad", "DD_DEVICE_SIGNING": "maybe",
                    "DD_STATE_BACKEND": "postgresx", "DD_DEVICE_KEYS": "[]"})
    errs = _keys(report, "errors")
    assert {"DD_TENANT_KEYS", "DD_DEVICE_KEYS", "DD_DEVICE_SIGNING",
            "DD_STATE_BACKEND"} <= errs


def test_dangerous_combinations_are_warnings_not_errors():
    """危险组合只该是警告（部署者可能**就是**在做这个选择），不能把体检判死。"""
    report = check({"DD_INVESTIGATE_ASYNC": "1", "DD_AUTO_MIGRATE": "1",
                    "DD_NOTIFY_ENABLED": "1", "DD_NOTIFY_ALLOW_PRIVATE": "1",
                    "DD_RETENTION_AUDIT_DAYS": "30", "DD_RETENTION_RAW_DAYS": "-1"})
    warns = _keys(report, "warnings")
    assert {"DD_API_KEY", "DD_STATE_BACKEND", "DD_AUTO_MIGRATE",
            "DD_NOTIFY_ALLOW_PRIVATE", "DD_RETENTION_AUDIT_DAYS",
            "DD_RETENTION_RAW_DAYS"} <= warns
    assert report["ok"], "危险组合不该是 error"


def test_clean_config_has_no_findings():
    report = check({"DD_API_KEY": "k", "DD_SERVICE_PORT": "8000",
                    "DD_STATE_BACKEND": "postgres",
                    "DD_DATABASE_URL": "postgresql+psycopg://u:p@h/db"})
    assert report["ok"] and not report["warnings"], report
    assert report["checked_keys"] > 100, report


def test_render_shows_every_finding():
    report = check({"DD_TYPO_KEY": "1", "DD_INVESTIGATE_ASYNC": "1"})
    text = render(report)
    assert "DD_TYPO_KEY" in text and "DD_STATE_BACKEND" in text
    assert "错误" in text and "警告" in text


def test_cli_exit_codes(monkeypatch):
    """错误 → 非零；只有警告 → 0（`--strict` 时才非零）。部署门禁靠这个码。"""
    from data_dog import config_check as cc

    clean = {"ok": True, "errors": [], "warnings": [], "checked_keys": 1}
    warn = {"ok": True, "errors": [],
            "warnings": [{"key": "X", "message": "m", "hint": ""}], "checked_keys": 1}
    bad = {"ok": False, "errors": [{"key": "X", "message": "m", "hint": ""}],
           "warnings": [], "checked_keys": 1}

    monkeypatch.setattr(cc, "check", lambda: clean)
    assert cc.main([]) == 0
    monkeypatch.setattr(cc, "check", lambda: warn)
    assert cc.main([]) == 0
    assert cc.main(["--strict"]) == 1
    monkeypatch.setattr(cc, "check", lambda: bad)
    assert cc.main(["--json"]) == 1
    print("PASS test_cli_exit_codes")
