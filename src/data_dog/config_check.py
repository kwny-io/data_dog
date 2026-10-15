"""配置体检（`data-dog config check`）：把"配错了但没人知道"提前变成红/黄灯。

为什么需要它
-----------
这个仓库有 140+ 个 `DD_*` 环境变量。读取点确实唯一（`settings.py`），但**没有任何启动期
校验**，于是三类错误都不报错：

1. **拼错键名**（如 `DD_STAT_BACKEND`）：值被完全忽略，程序拿默认值跑——多副本下 `memory`
   后端会让限流与候选合并各算各的，属于"看起来在跑、其实是错的"；
2. **数值写错**（如 `DD_INVESTIGATE_TIMEOUT_S=90s`）：`_get_int/_get_float` 捕获
   `ValueError` 后**静默回落默认值**；
3. **危险组合**：鉴权全关、异步调查 + 进程内状态、启动即自动迁移（多副本并发迁移）。

本模块只做**能用环境变量判定**的事：不连数据库/Redis、不验密钥真假——那些由 `/readyz`
与真实请求回答（见 `docs/reliability.md`）。判定为错误的项让 `config check` 以非零码退出，
适合放进部署前的 CI / 迁移 Job。
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from . import settings

_SRC_ROOT = Path(__file__).resolve().parent.parent        # src/
_SELF = Path(__file__).name
_KEY_LITERAL = re.compile(r"(DD_[A-Z0-9_]+)")
_TYPED = re.compile(r'_get_(int|float)\(\s*"(DD_[A-Z0-9_]+)"')
_RETENTION_DAYS = re.compile(r"^DD_RETENTION_[A-Z0-9_]*_DAYS$")
_BACKENDS = {"memory", "postgres", "redis"}
_TRUTHY_OFF = {"0", "false", "no", "off"}


def known_keys() -> set[str]:
    """仓库里真实存在的 `DD_*` 键：从 `src/**/*.py` 的**字符串字面量**提取。

    为什么用源码当真源而不是维护一张清单：清单一定会漂（新增一个设置忘登记一次），
    而源码里没有字面量就意味着**没有任何代码读它**——那正是"未知键"的定义。
    跳过本文件自身，避免把体检规则里的键名算进"已知"。
    """
    keys: set[str] = set()
    for path in sorted(_SRC_ROOT.rglob("*.py")):
        if path.name == _SELF:
            continue
        try:
            keys |= set(_KEY_LITERAL.findall(path.read_text(encoding="utf-8")))
        except OSError:                                   # pragma: no cover
            continue
    return keys


def typed_keys() -> dict[str, str]:
    """`{DD_键: "int"|"float"}`：settings.py 用哪个读取器读它（用于取值校验）。"""
    src = (Path(__file__).resolve().parent / "settings.py").read_text(encoding="utf-8")
    return {key: kind for kind, key in _TYPED.findall(src)}


def _truthy(env: dict[str, str], key: str, default: bool = False) -> bool:
    """与 `settings._get_bool` 同语义（显式 falsy 串判假，其余非空判真）。"""
    raw = env.get(key, "").strip()
    if not raw:
        return default
    return raw.lower() not in _TRUTHY_OFF


def check(environ: dict[str, str] | None = None) -> dict[str, Any]:
    """跑一遍体检。返回 `{ok, errors, warnings, checked_keys}`（**无副作用**）。"""
    env = dict(os.environ) if environ is None else dict(environ)
    known = known_keys()
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []

    def err(key: str, message: str, hint: str = "") -> None:
        errors.append({"key": key, "message": message, "hint": hint})

    def warn(key: str, message: str, hint: str = "") -> None:
        warnings.append({"key": key, "message": message, "hint": hint})

    # 1) 未知键 —— 静默忽略是这里最贵的失败模式
    for key in sorted(k for k in env if k.startswith("DD_")):
        if key not in known:
            err(key, "未知配置键：没有任何代码读取它，值会被完全忽略（程序照默认值跑）",
                "删掉它；确实要新增就改 src/data_dog/settings.py 并真的读它")

    # 2) 数值键取值非法 —— settings 会静默回落默认值
    for key, kind in sorted(typed_keys().items()):
        raw = env.get(key, "").strip()
        if not raw:
            continue
        try:
            int(raw) if kind == "int" else float(raw)
        except ValueError:
            want = "整数" if kind == "int" else "数字"
            err(key, f"不是合法{want}（当前 {raw!r}）——settings 会**静默**改用默认值",
                "修正取值，或删掉这一项让默认值生效")

    # 3) 结构性配置：JSON 形状 / 枚举（应用侧本来就会失败关闭，这里提前说清是哪一项）
    #    这里**只看传入的 env**，不调 settings 的读取器——它们读的是 os.environ，
    #    在测试/嵌入式调用里会与传入的 env 不一致（曾经因此漏报 DD_TENANT_KEYS 的坏 JSON）。
    import json as _json

    for key in ("DD_TENANT_KEYS", "DD_DEVICE_KEYS"):
        raw = env.get(key, "").strip()
        if not raw:
            continue
        try:
            obj = _json.loads(raw)
        except ValueError as e:
            err(key, f"不是合法 JSON：{e}", "JSON 对象结构见 docs/delivery.md")
            continue
        if not isinstance(obj, dict) or not obj:
            err(key, "必须是非空 JSON 对象（{id: 密钥}）", "见 docs/delivery.md")

    signing = env.get("DD_DEVICE_SIGNING", "").strip().lower()
    if signing and signing not in ("off", "optional", "required"):
        err("DD_DEVICE_SIGNING", f"取值非法：{signing!r}", "可用 off / optional / required")

    if environ is None:
        # 真实环境（environ=None）里再走一遍 settings 的**深度**校验：
        # 密钥环的轮换字段、重复 key id、ISO 时间等细节只有读取器知道。
        for key, loader in (("DD_TENANT_KEYS", settings.tenant_keyring),
                            ("DD_DEVICE_KEYS", settings.device_keys),
                            ("DD_DEVICE_SIGNING", settings.device_signing)):
            if not os.environ.get(key, "").strip():
                continue
            try:
                loader()
            except ValueError as e:
                err(key, f"配置非法：{e}", "取值要求见 docs/delivery.md")

    for key in ("DD_STATE_BACKEND", "DD_RATE_BACKEND"):
        raw = env.get(key, "").strip().lower()
        if raw and raw not in _BACKENDS:
            err(key, f"未知后端 {raw!r}", "可用 memory / postgres / redis（空 = 自动）")

    # 4) 危险组合（只用环境变量就能判定）
    if not env.get("DD_API_KEY", "").strip() and \
            not env.get("DD_TENANT_KEYS", "").strip():
        warn("DD_API_KEY", "鉴权完全没配：端口可达 = 任何人可读写（数据面与运维面都裸奔）",
             "配 DD_API_KEY（全局）或 DD_TENANT_KEYS（租户绑定）")

    if (env.get("DD_RAWLOG_BASE_DIR", "").strip()
            and env.get("DD_RAWLOG_BASE_URL", "").strip()):
        err("DD_RAWLOG_BASE_DIR",
            "与 DD_RAWLOG_BASE_URL **同时配置**：rawlog 正文存放位置必须二选一",
            "只留一个（本地受控目录 或 对象存储前缀）；同时配会让读取直接失败关闭")

    async_on = _truthy(env, "DD_INVESTIGATE_ASYNC")
    backend = env.get("DD_STATE_BACKEND", "").strip().lower()
    if async_on and backend in ("", "memory"):
        warn("DD_STATE_BACKEND",
             "异步调查 + memory 状态后端：队列与提交幂等只在**进程内**成立，多副本会各收各的",
             "多副本必须 DD_STATE_BACKEND=postgres（或 redis）")

    if _truthy(env, "DD_AUTO_MIGRATE"):
        warn("DD_AUTO_MIGRATE",
             "启动即自动迁移：多副本会**并发跑迁移**、互相竞争",
             "多副本置 0，迁移交给一次性 Job/命令（见 helm 的 migrate Job）")

    if _truthy(env, "DD_NOTIFY_ENABLED") and _truthy(env, "DD_NOTIFY_ALLOW_PRIVATE"):
        warn("DD_NOTIFY_ALLOW_PRIVATE",
             "通知目标允许内网/回环：SSRF 门已打开（默认失败关闭）",
             "只在受信内网置 1")

    for key in sorted(k for k in env if _RETENTION_DAYS.match(k)):
        try:
            n = int(env[key].strip())
        except ValueError:
            continue                                      # 数值非法已在第 2 步报过
        if n < 0:
            warn(key, f"保留天数写成负数（{n}）——settings 用 max(0, ...) 静默吞掉",
                 "0 的语义是**永久保留**，确认这是你要的")
    audit_days = env.get("DD_RETENTION_AUDIT_DAYS", "").strip()
    if audit_days and audit_days != "0":
        warn("DD_RETENTION_AUDIT_DAYS",
             "审计清理已开启（默认 0 = 永久）：销毁审计记录本身是合规风险",
             "确认保留期满足 1 年承诺，且删除留痕（retention.purge）仍在")

    return {"ok": not errors, "errors": errors, "warnings": warnings,
            "checked_keys": len(known)}


def render(report: dict[str, Any]) -> str:
    """人类可读输出（CLI 用）。"""
    errs, warns = report["errors"], report["warnings"]
    lines = [f"===== 配置体检（已知键 {report['checked_keys']} 个）====="]
    if not errs and not warns:
        lines.append("  [OK] 未发现问题")
    for item in errs:
        lines.append(f"  [错误] [{item['key']}] {item['message']}")
        if item["hint"]:
            lines.append(f"         -> {item['hint']}")
    for item in warns:
        lines.append(f"  [警告] [{item['key']}] {item['message']}")
        if item["hint"]:
            lines.append(f"         -> {item['hint']}")
    lines.append(f"  合计：{len(errs)} 错误 / {len(warns)} 警告")
    return chr(10).join(lines)


def main(argv: list[str] | None = None) -> int:
    """`python -m data_dog.config_check [--json] [--strict]`。

    `--strict` 把警告也算失败（部署门禁用）。
    """
    import argparse
    import json as _json

    from .core.console import ensure_utf8_console

    ensure_utf8_console()
    parser = argparse.ArgumentParser(description="data_dog 配置体检")
    parser.add_argument("--json", action="store_true", help="输出 JSON（供流水线采集）")
    parser.add_argument("--strict", action="store_true", help="警告也算失败（非零退出）")
    args = parser.parse_args(argv)

    report = check()
    if args.json:
        print(_json.dumps(report, ensure_ascii=False))
    else:
        print(render(report))
    if report["errors"] or (args.strict and report["warnings"]):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
