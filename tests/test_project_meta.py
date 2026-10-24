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


def test_sandbox_never_sets_rlimit_nproc():
    """沙箱 worker 不得用 `RLIMIT_NPROC` 限制线程数。

    为什么：Linux 的 NPROC 配额统计同一 real UID 下的**全部 task，含线程**。
    `worker_main` 设完 limit 紧接着就 `res_q.put(("ready", ...))`，而 `mp.Queue`
    首次 put 会 lazy 建 feeder 线程 → `RuntimeError: can't start new thread`。
    root 带 `CAP_SYS_RESOURCE` 会绕过该检查，于是本地（root 容器）全绿、
    GitHub runner（普通用户 `runner`）必红——2026-10-23 实测同镜像同代码仅换用户：
    `docker run --rm --user 65534 … pytest tests/test_sandbox.py` → 6 failed, 10 passed。

    禁 fork 请用 `_apply_seccomp()`（已 deny fork/vfork/clone/clone3），
    NPROC 做不到「只禁进程、放行线程」。
    """
    src = (ROOT / "src" / "data_dog" / "operator_factory" / "sandbox_exec.py").read_text(
        encoding="utf-8"
    )
    offenders = [
        f"{i}: {line.strip()}"
        for i, line in enumerate(src.splitlines(), 1)
        if "RLIMIT_NPROC" in line and not line.lstrip().startswith("#")
    ]
    assert not offenders, (
        "sandbox_exec.py 用 RLIMIT_NPROC 限线程：Linux 上 NPROC 也统计线程，"
        "NPROC=0 会让 mp.Queue 的 feeder 线程起不来（root 绕过检查、CI 普通用户必红）。"
        "违规点： " + " | ".join(offenders)
    )


def _git_tracked_files() -> list[str]:
    """返回被跟踪的文件路径（UTF-8，不转义）。

    为何不用 `git ls-files` 的默认输出：Windows 上 `core.quotepath=true` 会把非 ASCII
    文件名转成 C 风格转义（`"\\347\\247\\201\\347..."`），于是 `私人笔记.md` 变成一串
    数字——关键词匹配全部落空。2026-10-23 变异测试实测：中文名私人文件被踩进暂存区，
    守卫仍 PASS（假守卫）。这里用 `-c core.quotepath=false -z` + 显式 UTF-8 解码，
    并在此处一次性把坑堵住，避免每个调用点重复踩。
    """
    import subprocess

    raw = subprocess.run(
        ["git", "-c", "core.quotepath=false", "ls-files", "-z"],
        cwd=ROOT, capture_output=True, check=True,
    ).stdout
    return [p for p in raw.decode("utf-8", "surrogateescape").split("\0") if p]


def test_no_private_paths_are_tracked():
    """仓库是 public 的：私人/临时文件一旦 `git add -A` 就会被全世界看见。

    2026-10-23 用户明确要求（原话）：“你公开我的仓库的时候 私人文档不要上传”。
    这条守卫把该要求变成断言，而不是靠记性：
    任何命中私人路径模式的**被跟踪**文件，都让测试红。

    拦截时机在 `git add` 之后、`git commit` 之前（本机跑全量门禁时），
    以及 pre-push 钩子里。容器拷贝模式下没有 `.git`，此时跳过（容器里也提交不了）。
    """
    if not (ROOT / ".git").exists():
        print("SKIP test_no_private_paths_are_tracked（无 .git：容器拷贝模式）")
        return

    tracked = _git_tracked_files()

    # 目录前缀：会话/任务日志、构建产物、诊断草稿、本机草稿、依赖目录
    banned_prefixes = (
        ".pi/", "artifacts/", "diag/", "data/", ".venv/", "node_modules/",
        ".claude/", ".cursor/", "tmp/",
    )
    # 后缀：Windows 包装脚本、日志、会话导出、密钥材料
    banned_suffixes = (".bat", ".log", ".jsonl", ".pem", ".key", ".p12", ".kdbx")
    # 文件名关键词：私人笔记（中英）
    banned_keywords = ("笔记", "备忘", "私人", "个人", "草稿", "personal", "private",
                       "diary", "journal", "scratch")
    # 允许的例外（示例文件本身就是给公众看的）
    # `.bat` 默认被禁（Windows 本地临时脚本的典型形态）；这里的例外是**故意提交的产品入口**：
    # `make ci-offline` 在 Windows 上会把 `bash` 解析成 WSL 的 bash，而 WSL 看不到 `D:\`，
    # 于是验证台“没跑”却看起来像跑了。这个 .bat 只是用 Git 自带的 bash 转发给 .sh，
    # 是可复现性的一部分，不是个人草稿。
    allowed = {
        ".env.example",
        "scripts/verify_ci_offline.bat",
    }

    bad: list[str] = []
    for path in tracked:
        low = path.lower()
        name = low.rsplit("/", 1)[-1]
        if path in allowed:
            continue
        if any(low.startswith(p) for p in banned_prefixes):
            bad.append(f"{path}（禁止的目录前缀）")
        elif low.endswith(banned_suffixes):
            bad.append(f"{path}（禁止的文件后缀）")
        elif name.startswith(".env") and name != ".env.example":
            bad.append(f"{path}（真实环境变量文件）")
        elif any(k in low for k in banned_keywords):
            bad.append(f"{path}（私人/草稿关键词）")

    assert not bad, (
        "仓库已公开，这些私人或临时文件不该被跟踪：\n  "
        + "\n  ".join(bad)
        + "\n修法：git rm --cached <文件>，并把它加进 .gitignore"
    )
    print("PASS test_no_private_paths_are_tracked")


def test_no_competition_materials_are_tracked():
    """参赛/对外宣讲材料不进代码仓。

    2026-10-25 用户明确要求：“参赛材料不要写到提交的feature里”。
    这些东西的本体（PDF / 演示稿 / 路演页）属于交付材料，不属于工程资产：
    它们会和代码一起被版本化、被用 diff 看，却没有一个可回滚的失败模式。
    它们的正确位置是桌面（当前的 `data_dog-参赛材料.pdf/.html`、
    `data_dog-明日PDF素材.md`、`data_dog-答辩七问.md` 都在桌面上）。

    但“用过的 pdf”不能一刀切：`vendor_docs/` 和 `third_party/` 下的第三方
    手册/许可证原文是**证据**（依从性/许可证审计要用），必须能进仓。
    """
    if not (ROOT / ".git").exists():
        print("SKIP test_no_competition_materials_are_tracked（无 .git：容器拷贝模式）")
        return

    tracked = _git_tracked_files()

    # 名字里出现即拦：中英文两种写法都算
    banned_keywords = (
        "参赛", "competition", "hackathon", "路演", "pitch", "路縯",
        "商业计划书", "计划书", "答辩", "演示视频", "宣讲",
    )
    # 演示/文稿类后缀（代码仓里不应出现二进制文稿）
    banned_suffixes = (".pptx", ".ppt", ".docx", ".doc", ".key", ".mp4", ".m4v", ".mov")
    # PDF 默认禁，但证据类目录放行
    pdf_evidence_prefixes = ("third_party/", "vendor_docs/")

    bad: list[str] = []
    for path in tracked:
        low = path.lower()
        if low.endswith(".pdf") and not low.startswith(pdf_evidence_prefixes):
            bad.append(f"{path}（PDF 非证据类目录，属于对外材料）")
        elif low.endswith(banned_suffixes):
            bad.append(f"{path}（演示/文稿二进制后缀）")
        elif any(k in low for k in banned_keywords):
            bad.append(f"{path}（参赛/宣讲关键词）")

    assert not bad, (
        "参赛/对外材料不该进 feature 分支（留在桌面）：\n  "
        + "\n  ".join(bad)
        + "\n修法：git rm --cached <文件>；证据类 PDF 放 third_party/ 或 vendor_docs/ 才允许跟踪"
    )
    print("PASS test_no_competition_materials_are_tracked")
