"""项目元数据一致性：让"声明过的"与"真实的"不可能悄悄漂移。

为什么值得单独一组测试
----------------------
这个仓库踩过同一类坑三次，每次都不是逻辑 bug，而是**声明与事实脱节**：

1. `prometheus-client` 在 `pyproject.toml` 声明了、`requirements.txt` 里没有 →
   按 requirements 安装的人，`python -m data_dog.service` 直接 ImportError
   （本地 venv 恰好装过，所以"测不出来"）。
2. `signing.py` 与 `docs/p4/02_control_plane.md` 都让用户
   `pip install data_dog[signing]`，但 pyproject 里**根本没有这个 extra**。
3. README 用精确数字做承诺（源码/测试多少文件多少行、多少个 make 命令），
   加几个文件就漂一位，而**没有任何机制会提醒**。

这类漂移靠人肉记性守不住，所以把"声明 == 事实"变成断言：
数字由脚本重算（`scripts/readme_counts.py`）、依赖清单由脚本生成
（`scripts/gen_requirements.py`），本文件只负责"比对并失败关闭"。
"""
from __future__ import annotations

import importlib.util
import pathlib
import re

try:                              # Python 3.10 无 tomllib（3.11 才进标准库）
    import tomllib
except ModuleNotFoundError:       # pragma: no cover - 仅 3.10 走这里
    import tomli as tomllib  # dev extra 里按 python_version < '3.11' 装了兜底

ROOT = pathlib.Path(__file__).resolve().parent.parent


def _load_script(name: str):
    """`scripts/*.py` 不是包，用 spec 直接加载（比 subprocess 快且无 stdio 依赖）。"""
    path = ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"_dd_{name}", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_tests_never_create_schema_on_real_pg_with_create_all():
    """测试不得用 `init_db()`（`create_all`）打真 PostgreSQL。

    为什么：`init_db()` 建表但**不写 `alembic_version`**，于是库里出现"表都在、
    版本表为空"的楔子状态；同一个 session 里后续任何 `upgrade_head()` 都会
    `DuplicateTable`。2026-10-15 实锤：干净库上 `pytest -m postgres` 是
    `22 passed + 1 failed + 14 errors`（14 个 error 全是 state_store 的 PG fixture
    在 `upgrade_head` 时撞已存在的表），改成 `migrations.upgrade_head()` 后 37 passed。

    真库一律走 `migrations.upgrade_head()`——与生产启动路径 `service/app.py` 同源。
    """
    offenders: list[str] = []
    _self = pathlib.Path(__file__).name          # 本文件里的匹配模式会自匹配，跳过它
    for f in sorted((ROOT / "tests").glob("*.py")):
        if f.name == _self:
            continue
        for i, line in enumerate(f.read_text(encoding="utf-8").splitlines(), 1):
            if line.lstrip().startswith("#"):        # 注释不算
                continue
            if "init_db(" in line and ("_PG_URL" in line or "DD_TEST_PG_URL" in line):
                offenders.append(f"{f.name}:{i}: {line.strip()}")
    assert not offenders, (
        "测试里用 init_db() 打真 PG：create_all 不写 alembic_version，"
        "随后 upgrade_head() 会 DuplicateTable。请改用 migrations.upgrade_head()。违规点："
        + " | ".join(offenders)
    )


def test_readme_scale_numbers_match_reality():
    """README 里的规模数字必须与磁盘一致（不一致就跑 scripts/readme_counts.py）。"""
    rc = _load_script("readme_counts")
    want, changes = rc.build()
    have = rc.README.read_text(encoding="utf-8")
    assert want == have, (
        "README 规模数字已漂移：\n  " + "\n  ".join(changes)
        + "\n修法：python scripts/readme_counts.py")
    print("PASS test_readme_scale_numbers_match_reality")


def test_pre_commit_runs_readme_counts_check():
    """README 数字的核对必须挂在 pre-commit 上（否则只有"人记得"时才跑）。

    这条真的漏过：数字脚本早就存在，但**没有任何自动闸门调用它**——本轮新增测试文件时
    README 就漂了一次，靠人肉才发现。而且钩子里这一步必须**无条件跑**：漂移最常见的形态是
    "只新增了文件、README 忘了改"，那种提交里根本没有 .py 变更，会被"无暂存 .py 就退出"跳过。
    """
    hook = (ROOT / "scripts" / "git-hooks" / "pre-commit").read_text(encoding="utf-8")
    assert "readme_counts.py --check" in hook, "pre-commit 没有核对 README 规模数字"
    assert hook.index("readme_counts.py --check") > hook.index("ruff check"), \
        "核对步骤必须在 ruff 之后单独成步"

    step = next(line for line in hook.splitlines()
                if "readme_counts.py --check" in line and line.strip().startswith('"$PY"'))
    assert not step.startswith((" ", "\t")), (
        "核对步骤被缩进了——说明它被圈在 if [ -n '$files' ] 里，"
        "没有 .py 变更的提交会直接跳过它")
    print("PASS test_pre_commit_runs_readme_counts_check")


def test_requirements_txt_is_generated_from_pyproject():
    """requirements.txt 必须等于由 pyproject 生成的结果（两份手抄清单必然漂移）。"""
    gr = _load_script("gen_requirements")
    assert gr.generate() == gr.REQUIREMENTS.read_text(encoding="utf-8"), (
        "requirements.txt 与 pyproject.toml 漂移；修法：python scripts/gen_requirements.py")
    print("PASS test_requirements_txt_is_generated_from_pyproject")


def test_every_referenced_extra_exists():
    """文档/代码/构建文件里让用户安装的每个 extra，都必须在 pyproject 里真实存在。

    扫的是 `.[a,b]` 与 `data_dog[a,b]` 两种写法（含 Dockerfile / compose / CI / Makefile，
    因为 extra 写错最直接的后果就是镜像构建或 CI 安装失败）。
    """
    extras = set(_pyproject()["project"]["optional-dependencies"])
    pattern = re.compile(r"(?:data_dog|(?<![\w\]])\.)\[([a-z0-9_]+(?:,[a-z0-9_]+)*)\]")

    targets = [ROOT / "README.md", ROOT / "Dockerfile", ROOT / "Makefile",
               ROOT / "requirements.txt", ROOT / "pyproject.toml"]
    targets += sorted((ROOT / "docs").rglob("*.md"))
    targets += sorted((ROOT / "src").rglob("*.py"))
    targets += sorted(ROOT.glob("compose*.yaml"))
    targets += sorted((ROOT / ".github" / "workflows").glob("*.yml"))

    found: set[str] = set()
    for path in targets:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for group in pattern.findall(text):
            found.update(group.split(","))

    missing = sorted(found - extras)
    assert not missing, (
        f"这些 extra 被引用但 pyproject 里不存在：{missing}；"
        f"pyproject 现有 extras：{sorted(extras)}")
    # 这条曾经真的缺失过（signing.py 让用户装 data_dog[signing]，extra 却不存在）
    assert "signing" in extras
    print("PASS test_every_referenced_extra_exists")


def test_license_declared_and_present():
    """pyproject 声明 MIT，就必须真有 LICENSE 文件（别只写在元数据里）。"""
    project = _pyproject()["project"]
    declared = str(project.get("license", {}).get("text", "")).upper()
    assert declared == "MIT", f"pyproject 里 license 声明变了：{declared!r}"
    license_file = ROOT / "LICENSE"
    assert license_file.exists(), "缺 LICENSE 文件（pyproject 已声明 MIT）"
    head = license_file.read_text(encoding="utf-8")[:200]
    assert "MIT License" in head, "LICENSE 文件内容不是 MIT 正文"
    print("PASS test_license_declared_and_present")


def test_entry_points_are_importable():
    """[project.scripts] 里写的 `模块:属性` 必须真的能解析到（改了包结构而忘改入口会漏）。"""
    scripts = _pyproject()["project"].get("scripts", {})
    assert scripts, "pyproject 没有 [project.scripts] 入口"
    for name, target in scripts.items():
        module_name, _, attr = target.partition(":")
        assert attr, f"入口 {name} 的写法不是 `模块:属性`：{target}"
        mod = importlib.import_module(module_name)
        assert hasattr(mod, attr), f"入口 {name} 指向 {target}，但模块里没有 {attr}"
    print("PASS test_entry_points_are_importable")


def test_schemas_match_readme_claim():
    """README 说"5 份正式事件 JSON Schema"，就必须真有 5 份、且各有正反例与校验脚本。"""
    schemas = sorted((ROOT / "schemas").glob("*.json"))
    assert len(schemas) == 5, f"README 声称 5 份 Schema，实际 {len(schemas)} 份"
    for path in schemas:
        for kind in ("valid", "invalid"):
            example = ROOT / "schemas" / "examples" / f"{path.stem}.{kind}.json"
            assert example.exists(), f"{path.name} 缺 {kind} 示例：{example.name}"
    assert (ROOT / "schemas" / "tools" / "validate_examples.py").exists()
    print("PASS test_schemas_match_readme_claim")


def test_python_version_consistent_across_manifests():
    """pyproject / CI / Dockerfile 声明的 Python 版本必须自洽（别一处 3.10 一处 3.12）。"""
    requires = _pyproject()["project"]["requires-python"]          # 例如 ">=3.10"
    m = re.match(r">=(\d+)\.(\d+)", requires)
    assert m, f"requires-python 写法不认识：{requires!r}"
    floor = (int(m.group(1)), int(m.group(2)))

    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    dm = re.search(r"^FROM python:(\d+)\.(\d+)", dockerfile, re.M)
    assert dm, "Dockerfile 里找不到 `FROM python:X.Y`"
    assert (int(dm.group(1)), int(dm.group(2))) >= floor, "Dockerfile 的基础镜像低于 requires-python"

    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    versions = {tuple(int(x) for x in v.split(".")) for v in re.findall(r'python-version:\s*"(\d+\.\d+)"', ci)}
    assert versions, "CI 里找不到 python-version"
    for v in versions:
        assert v >= floor, f"CI 用的 Python {v} 低于 requires-python {requires}"
    print("PASS test_python_version_consistent_across_manifests")


def _load_migration(name_part: str):
    """按文件名片段加载一个 alembic 迁移模块（只读常量，不执行 upgrade）。"""
    paths = sorted((ROOT / "alembic" / "versions").glob(f"*{name_part}*"))
    assert paths, f"找不到迁移文件：*{name_part}*"
    spec = importlib.util.spec_from_file_location("_dd_mig", paths[0])
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_json_columns_are_jsonb_on_postgres():
    """models.py 说 PostgreSQL 上是 JSONB，那就**必须真的是** JSONB（声明 == 事实）。

    这条曾经真的错过：baseline 迁移用 sa.JSON()，在 PG 上落成 json——不能建 GIN 索引、
    不去重、每次取值都要重新解析，而 docstring 一直写着 JSONB。
    """
    from sqlalchemy.dialects import postgresql
    from sqlalchemy.types import JSON as SA_JSON

    from data_dog.storage.models import Base

    bad = []
    for table in Base.metadata.tables.values():
        for col in table.c:
            if isinstance(col.type, SA_JSON):
                impl = col.type.dialect_impl(postgresql.dialect())
                if not isinstance(impl, postgresql.JSONB):
                    bad.append(f"{table.name}.{col.name} -> {type(impl).__name__}")
    assert not bad, "这些 JSON 列在 PostgreSQL 上不是 JSONB：" + ", ".join(bad)
    print("PASS test_json_columns_are_jsonb_on_postgres")


def test_jsonb_migration_covers_every_json_column():
    """迁移的 JSON 列清单必须与模型元数据**逐条一致**。

    为什么值得一条测试：清单写死然后与模型漂移，正是本仓反复吃过的亏——新增一个 JSON 列
    会悄悄是 json（而不是 jsonb），而且没人会注意到。
    """
    from sqlalchemy.types import JSON as SA_JSON

    from data_dog.storage.models import Base

    mig = _load_migration("jsonb_and_indexes")
    have = {(t.name, c.name) for t in Base.metadata.tables.values()
            for c in t.c if isinstance(c.type, SA_JSON)}
    listed = set(mig._JSON_COLUMNS)
    assert have == listed, (
        f"迁移漏了：{sorted(have - listed)}；迁移多了：{sorted(listed - have)}"
        + chr(10) + "修法：改 alembic/versions/20261013_0900_jsonb_and_indexes.py 的 _JSON_COLUMNS")
    print("PASS test_jsonb_migration_covers_every_json_column")


def test_composite_indexes_declared_in_models_and_migration():
    """租户维度的复合索引：模型声明与迁移**同名同表**（否则 ORM 与库会各说各话）。"""
    from data_dog.storage.models import Base

    mig = _load_migration("jsonb_and_indexes")
    have = {ix.name for t in Base.metadata.tables.values() for ix in t.indexes}
    listed = {name for name, _table, _cols in mig._COMPOSITE}
    assert listed <= have, f"这些复合索引只在迁移里、没在模型里：{sorted(listed - have)}"
    assert len(listed) >= 4, "租户复合索引少得可疑（至少要覆盖日志/候选/报告/告警）"
    print("PASS test_composite_indexes_declared_in_models_and_migration")
