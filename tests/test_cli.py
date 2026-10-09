"""`cli.py`（Typer 入口）的命令行测试。

为什么要这么测：CLI 是"人"的第一入口，它的分支几乎全是**配置 × 交互组合**
（未配 key、未知供应商、候选文件缺失、交互窗的 n/i/b/q 指令）。这些分支出错的
代价是用户看到一屏堆栈而不是可读提示，故逐个钉住**退出码 + 提示语**。

离线纪律
--------
- `data-dog run/test --offline` 走 `_FakeProvider`（确定性剧本，不发网络请求）；
- `init` 的两处外部副作用都打桩：`Provider.ping`（连通性校验，否则真联网）与
  `save_config`（否则会写仓库里的 `.env`）；`load_env` 也替成空配置，避免用例
  依赖本机 `.env` 的实际内容。

诚实边界：交互窗的**多轮对话**用 `CliRunner(input=...)` 驱动（CI 里稳定），
但没有覆盖"终端是 TTY 时的富文本着色"（那属于 rich 的职责，不在本模块）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from data_dog import cli
from data_dog.cli import app

_runner = CliRunner()
_DEFAULT_FIXTURE = Path("evidence/candidate_temperature.json")


def _candidate_json(candidate_id: str = "cand-imported",
                    component: str = "battery.main",
                    signal: str = "battery_state_check") -> str:
    """造一份合法的候选异常 JSON（字段与 schemas.events.CandidateEvent 对齐）。"""
    return json.dumps({
        "candidate_id": candidate_id,
        "device_sn": "device-abc123",
        "component_ref": component,
        "signal": signal,
        "severity_hint": "warning",
        "window_start": "09:55",
        "window_end": "10:00",
        "sample_count": 300,
        "rule_matches": ["MANUAL_IMPORT"],
        "anomaly_score": 0.5,
        "detector_provenance": "pytest-import",
    }, ensure_ascii=False)


@pytest.fixture()
def stubbed_init(monkeypatch, tmp_path):
    """把 init 的联网校验与写 `.env` 都换成桩，返回记录保存参数的 dict。"""
    saved: dict = {}

    def _fake_save(vendor_id, api_key, model, root=None):
        saved.update(vendor_id=vendor_id, api_key=api_key, model=model)
        return tmp_path / ".env"

    monkeypatch.setattr(cli, "save_config", _fake_save)
    monkeypatch.setattr("data_dog.providers.client.Provider.ping",
                        lambda self, model=None: True)
    # 不读本机真实 .env：让"环境变量已有 key"这条路径完全确定
    monkeypatch.setattr("data_dog.providers.config.load_env",
                        lambda root=None: ({}, tmp_path))
    return saved


# ---------------------------------------------------------------- run（离线）


def test_run_offline_uses_default_fixture():
    """`run --offline` 用默认候选文件跑完整调查并渲染报告（不需要任何 key）。"""
    if not _DEFAULT_FIXTURE.exists():
        pytest.skip("默认候选文件需在 cwd 下（请从仓库根跑 pytest）")

    res = _runner.invoke(app, ["run", "--offline"])
    assert res.exit_code == 0, res.output
    assert "调查候选" in res.output
    assert "cand_01JXYZ" in res.output                     # 默认文件的候选 id
    assert "InvestigationReport" in res.output
    assert "NEEDS_HUMAN_REVIEW" in res.output              # 左髋温度爬升剧本
    print("PASS test_run_offline_uses_default_fixture")


def test_run_offline_accepts_explicit_fixture_and_vendor_options(tmp_path):
    """显式 `--fixture/--vendor/--model` 都要被接受；离线时 vendor/model 不影响结论。"""
    fixture = tmp_path / "candidate.json"
    fixture.write_text(_candidate_json(component="joint_motor.front_right_hip"),
                       encoding="utf-8")

    res = _runner.invoke(app, ["run", "--offline", "--fixture", str(fixture),
                              "--vendor", "openai", "--model", "gpt-4o-mini"])
    assert res.exit_code == 0, res.output
    assert "cand-imported" in res.output
    assert "REJECTED" in res.output                        # 右髋数据正常 → 排除误报
    print("PASS test_run_offline_accepts_explicit_fixture_and_vendor_options")


def test_run_missing_fixture_exits_1(tmp_path):
    """候选文件不存在 → 明确提示 + 退出码 1（不抛裸异常堆栈）。"""
    missing = tmp_path / "nope.json"
    res = _runner.invoke(app, ["run", "--offline", "--fixture", str(missing)])
    assert res.exit_code == 1
    assert "找不到候选异常文件" in res.output
    assert str(missing) in res.output
    print("PASS test_run_missing_fixture_exits_1")


def test_run_without_offline_and_without_key_exits_1(monkeypatch):
    """未配 key 又没加 `--offline` → 提示两条出路并退出 1（不静默发假请求）。"""
    monkeypatch.setattr(cli, "load_config",
                        lambda root=None: {"vendor_id": "deepseek", "api_key": "",
                                           "model": ""})
    res = _runner.invoke(app, ["run"])
    assert res.exit_code == 1
    assert "尚未配置 API Key" in res.output
    assert "--offline" in res.output
    print("PASS test_run_without_offline_and_without_key_exits_1")


# ---------------------------------------------------------------- init（交互接入）


def test_init_with_vendor_uses_env_key_and_saves(monkeypatch, stubbed_init):
    """`--vendor` 直连：沿用环境变量里已有的 key，保存选中的模型。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-from-env")

    res = _runner.invoke(app, ["init", "--vendor", "deepseek"], input="\n\n")
    assert res.exit_code == 0, res.output
    assert "正在校验连接…" in res.output
    assert "已保存配置到" in res.output
    assert stubbed_init == {"vendor_id": "deepseek", "api_key": "sk-from-env",
                            "model": "deepseek-chat"}
    print("PASS test_init_with_vendor_uses_env_key_and_saves")


def test_init_warns_but_saves_when_ping_fails(monkeypatch, stubbed_init):
    """连通性校验失败**不阻止**保存（用户可离线先存好配置），但要明确告警。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-bad")
    monkeypatch.setattr("data_dog.providers.client.Provider.ping",
                        lambda self, model=None: False)

    res = _runner.invoke(app, ["init", "--vendor", "deepseek"], input="\n\n")
    assert res.exit_code == 0, res.output
    assert "连接校验失败" in res.output
    assert stubbed_init["vendor_id"] == "deepseek"
    print("PASS test_init_warns_but_saves_when_ping_fails")


def test_init_prompts_for_vendor_when_not_given(monkeypatch, stubbed_init):
    """不给 `--vendor` 就打印编号清单并让用户选（选 2 → openai）。"""
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai")

    res = _runner.invoke(app, ["init"], input="2\n\n\n")
    assert res.exit_code == 0, res.output
    assert "可选的 LLM 供应商" in res.output
    assert "[2] OpenAI" in res.output
    assert stubbed_init["vendor_id"] == "openai"
    assert stubbed_init["model"] == "gpt-4o-mini"
    print("PASS test_init_prompts_for_vendor_when_not_given")


def test_init_invalid_menu_choice_falls_back_to_deepseek(monkeypatch, stubbed_init):
    """编号不是数字 → 回落到默认 DeepSeek 并提示，而不是崩溃。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-env")

    res = _runner.invoke(app, ["init"], input="abc\n\n\n")
    assert res.exit_code == 0, res.output
    assert "无效编号" in res.output
    assert stubbed_init["vendor_id"] == "deepseek"
    print("PASS test_init_invalid_menu_choice_falls_back_to_deepseek")


def test_init_no_key_provided_exits_1(monkeypatch, stubbed_init):
    """环境变量与 .env 都没有 key 且用户直接回车 → 失败关闭，退出 1。"""
    monkeypatch.delenv("ZHIPU_API_KEY", raising=False)

    res = _runner.invoke(app, ["init", "--vendor", "zhipu"], input="\n")
    assert res.exit_code == 1
    assert "未提供 API Key" in res.output
    print("PASS test_init_no_key_provided_exits_1")


def test_init_unknown_vendor_exits_1():
    """未知供应商 id → 提示可选值 + 退出 1（`get_vendor` 的 ValueError 被翻译）。"""
    res = _runner.invoke(app, ["init", "--vendor", "not-a-vendor"])
    assert res.exit_code == 1
    assert "未知供应商" in res.output
    print("PASS test_init_unknown_vendor_exits_1")


# ---------------------------------------------------------------- test（交互窗）


def test_test_quit_immediately_reports_nothing_done():
    """只按 q → 列出候选清单后汇总"本轮未做任何调查"，退出 0。"""
    res = _runner.invoke(app, ["test", "--offline"], input="q\n")
    assert res.exit_code == 0, res.output
    assert "候选异常清单" in res.output
    assert "本轮未做任何调查" in res.output
    print("PASS test_test_quit_immediately_reports_nothing_done")


def test_test_runs_selected_case_and_renders_compare_table():
    """选编号 1 → 真跑一次调查并在汇总表里对比结论。"""
    res = _runner.invoke(app, ["test", "--offline"], input="1\nq\n")
    assert res.exit_code == 0, res.output
    assert "REJECTED" in res.output                        # 右髋数据正常
    assert "候选对比" in res.output
    print("PASS test_test_runs_selected_case_and_renders_compare_table")


def test_test_covers_other_cases_and_history_hint():
    """再跑左髋（查近期变更）与电池两个用例；b 有历史时提示可回看。"""
    res = _runner.invoke(app, ["test", "--offline"], input="2\n3\nb\nq\n")
    assert res.exit_code == 0, res.output
    assert "NEEDS_HUMAN_REVIEW" in res.output              # 左髋温度剧本
    assert "最近的调查如上" in res.output
    assert "候选对比" in res.output
    print("PASS test_test_covers_other_cases_and_history_hint")


def test_test_history_hint_without_previous_run():
    """还没查过就按 b → 如实提示"还没有上一步调查"。"""
    res = _runner.invoke(app, ["test", "--offline"], input="b\nq\n")
    assert res.exit_code == 0, res.output
    assert "还没有上一步调查" in res.output
    print("PASS test_test_history_hint_without_previous_run")


def test_test_invalid_choices_are_reported_not_fatal():
    """非数字与越界编号都只提示重输（ValueError / IndexError 两条都要兜住）。"""
    res = _runner.invoke(app, ["test", "--offline"], input="abc\n99\nq\n")
    assert res.exit_code == 0, res.output
    assert res.output.count("无效输入") == 2
    print("PASS test_test_invalid_choices_are_reported_not_fatal")


def test_test_import_cancelled_by_empty_path():
    """i 指令直接回车 → 取消导入，不报错。"""
    res = _runner.invoke(app, ["test", "--offline"], input="i\n\nq\n")
    assert res.exit_code == 0, res.output
    assert "已取消导入" in res.output
    print("PASS test_test_import_cancelled_by_empty_path")


def test_test_import_missing_file_reports_and_continues(tmp_path):
    """导入不存在的路径 → 提示无法加载并继续下一轮（不退出）。"""
    missing = tmp_path / "absent.json"
    res = _runner.invoke(app, ["test", "--offline"], input=f"i\n{missing}\nq\n")
    assert res.exit_code == 0, res.output
    assert "无法加载" in res.output
    print("PASS test_test_import_missing_file_reports_and_continues")


def test_test_import_valid_file_runs_investigation(tmp_path):
    """导入合法 JSON → 真跑一次调查，汇总表里带自定义标签。"""
    fixture = tmp_path / "custom.json"
    fixture.write_text(_candidate_json(), encoding="utf-8")

    res = _runner.invoke(app, ["test", "--offline"], input=f"i\n{fixture}\nq\n")
    assert res.exit_code == 0, res.output
    assert "自定义: cand-imported" in res.output
    assert "REJECTED" in res.output                        # battery.main → 排除
    print("PASS test_test_import_valid_file_runs_investigation")


def test_test_new_candidate_questionnaire_runs():
    """n 指令的问答式新建：填满字段 → 调查这个临时候选。"""
    res = _runner.invoke(app, ["test", "--offline"],
                         input="n\njoint_motor.front_left_hip\ntemperature_rising\n"
                               "09:55\n10:00\ncritical\n0.9\nq\n")
    assert res.exit_code == 0, res.output
    assert "临时新建" in res.output
    assert "临时: joint_motor.front_left_hip" in res.output
    assert "NEEDS_HUMAN_REVIEW" in res.output
    print("PASS test_test_new_candidate_questionnaire_runs")


def test_test_new_candidate_blank_component_cancels():
    """部件留空（只输空格）→ 取消新建，不产候选也不调查。"""
    res = _runner.invoke(app, ["test", "--offline"], input="n\n   \nq\n")
    assert res.exit_code == 0, res.output
    assert "已取消" in res.output
    assert "本轮未做任何调查" in res.output
    print("PASS test_test_new_candidate_blank_component_cancels")


def test_test_new_candidate_bad_score_falls_back():
    """异常分填了非数字 → 回落到 0.5 继续，而不是中途崩掉整轮会话。"""
    res = _runner.invoke(app, ["test", "--offline"],
                         input="n\nbattery.main\nbattery_state_check\n09:55\n10:00\n"
                               "warning\nabc\nq\n")
    assert res.exit_code == 0, res.output
    assert "临时: battery.main" in res.output
    print("PASS test_test_new_candidate_bad_score_falls_back")


def test_test_import_corrupt_file_reports_and_continues(tmp_path):
    """文件存在但内容坏掉（非法 JSON / 缺字段）→ 同样只提示无法加载，不崩。"""
    broken = tmp_path / "broken.json"
    broken.write_text("{not-json", encoding="utf-8")
    res = _runner.invoke(app, ["test", "--offline"], input=f"i\n{broken}\nq\n")
    assert res.exit_code == 0, res.output
    assert "无法加载" in res.output

    incomplete = tmp_path / "incomplete.json"
    incomplete.write_text(json.dumps({"candidate_id": "x"}), encoding="utf-8")
    res2 = _runner.invoke(app, ["test", "--offline"], input=f"i\n{incomplete}\nq\n")
    assert res2.exit_code == 0, res2.output
    assert "无法加载" in res2.output
    print("PASS test_test_import_corrupt_file_reports_and_continues")


# ---------------------------------------------------------------- 纯助手


def test_make_provider_uses_configured_vendor_and_key(monkeypatch):
    """非离线路径：用配置里的 vendor/key 造 provider，`--vendor` 可覆盖配置。"""
    monkeypatch.setattr(cli, "load_config",
                        lambda root=None: {"vendor_id": "deepseek",
                                           "api_key": "sk-abc", "model": "m"})
    seen: dict = {}

    def _fake_from_config(vendor_id, api_key, timeout=None):
        seen.update(vendor_id=vendor_id, api_key=api_key)
        return "provider-sentinel"

    monkeypatch.setattr("data_dog.providers.client.Provider.from_config",
                        _fake_from_config)
    assert cli._make_provider(False, None, None) == "provider-sentinel"
    assert seen == {"vendor_id": "deepseek", "api_key": "sk-abc"}
    assert cli._make_provider(False, "zhipu", None) == "provider-sentinel"
    assert seen["vendor_id"] == "zhipu"
    print("PASS test_make_provider_uses_configured_vendor_and_key")


def test_fake_provider_parses_dict_and_broken_messages():
    """离线 provider 的候选抽取要兼容 dict / dataclass，坏 JSON 不抛异常。"""
    from data_dog.cli import _FakeProvider
    from data_dog.providers.client import LLMChatMessage

    ok = _FakeProvider._candidate([{"role": "user",
                                    "content": '前导文字 {"component_ref": "battery.main"}'}])
    assert ok["component_ref"] == "battery.main"
    dataclass_msg = LLMChatMessage(role="user", content='{"component_ref": "x"}')
    assert _FakeProvider._candidate([dataclass_msg])["component_ref"] == "x"
    # 没有 JSON / JSON 坏掉：都当作空候选（默认剧本），绝不抛
    assert _FakeProvider._candidate([{"role": "user", "content": "no json here"}]) == {}
    assert _FakeProvider._candidate([{"role": "user", "content": "{broken"}]) == {}
    print("PASS test_fake_provider_parses_dict_and_broken_messages")


def test_ui_module_fails_loudly_when_spec_unavailable(monkeypatch):
    """UI 模块拿不到 spec 时要报明确的 ImportError（而不是神秘 AttributeError）。"""
    monkeypatch.setattr("importlib.util.spec_from_file_location",
                        lambda *a, **k: None)
    with pytest.raises(ImportError, match="无法加载 UI 模块"):
        cli._ui_module("rich_run")
    print("PASS test_ui_module_fails_loudly_when_spec_unavailable")


@pytest.mark.filterwarnings("ignore::RuntimeWarning")   # runpy 重跑本模块的提示
def test_module_is_runnable_as_script(monkeypatch):
    """模块作为脚本跑（`python -m data_dog.cli`）时必须走 Typer 主入口。"""
    import runpy
    import sys

    monkeypatch.setattr(sys, "argv", ["data-dog", "--help"])
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_module("data_dog.cli", run_name="__main__")
    assert exit_info.value.code == 0          # --help 正常退出
    print("PASS test_module_is_runnable_as_script")
