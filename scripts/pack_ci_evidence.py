#!/usr/bin/env python
"""把一次离线 CI 验证的产物打成可归档的证据包。

设计原则（为什么这么写）：

1. **不许手写结论**。本脚本只读 `rc.tsv`，任何一条腿 rc≠0 就**拒绝出包**并以非 0 退出。
   理由：`docs/ci-offline.md` 里曾经写着"9/9 通过"而实际从没跑过——证据包如果允许
   "人写一句绿的结论"再打包，那它只是把假话装订得更漂亮。
2. **不依赖外部工具**。用 stdlib `tarfile` / `hashlib`，没有 tar/zip/7z 也能跑（Windows 原生亦可）。
3. **包内必含自证材料**：git 提交 + 是否有未提交改动 + 镜像 ID + 每条腿的 rc 与耗时 +
   每个日志文件的 sha256。这样别人不需要信任我们，只需要校验。
4. **可复现**：包内附 `RERUN.md`，写明重建镜像与重跑的命令，让人能自己从零复现。

用法：
    python scripts/pack_ci_evidence.py                       # 取 artifacts/ci-offline 下最新一轮
    python scripts/pack_ci_evidence.py <run-dir>             # 指定某一轮
    python scripts/pack_ci_evidence.py <run-dir> --image dd-ci-python:3.12 --image dd-ci-node:20
"""

from __future__ import annotations

import argparse
import hashlib
import io
import re
import subprocess
import sys
import tarfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _all_legs() -> list[str]:
    """从 `scripts/verify_ci_offline.sh` 读 ALL_LEGS。

    清单**只有一个真源**：脚本本身。写死两份必然漂移，而漂移的后果是
    “证据包以为覆盖了 9 条腿，实际只跑了 8 条”——比红一条腿更难发现。
    """
    text = (REPO / "scripts" / "verify_ci_offline.sh").read_text("utf-8")
    m = re.search(r'^ALL_LEGS="([^"]+)"', text, re.MULTILINE)
    return m.group(1).split() if m else []


ALL_LEGS = _all_legs()

# Windows 控制台默认 GBK，中文会变乱码（实测：`拒绝出包` 打印成 `�ܾ�������`）。
# 交付脚本的输出要能直接贴进工单，所以强制 UTF-8。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    except (AttributeError, ValueError):
        pass


def _run(cmd: list[str]) -> str:
    try:
        out = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=30, check=False)
        return (out.stdout or out.stderr).strip()
    except (OSError, subprocess.SubprocessError) as exc:  # 工具不存在/超时都不该让打包失败
        return f"<unavailable: {exc}>"


def _latest_run(base: Path) -> Path | None:
    runs = sorted(p for p in base.iterdir() if p.is_dir() and (p / "rc.tsv").exists())
    return runs[-1] if runs else None


def parse_rc(path: Path) -> list[tuple[str, int, str]]:
    """解析 `leg<TAB>rc<TAB>秒数`。格式不认识就直接抛错——宁可炸，也不要静默少一条腿。"""
    rows: list[tuple[str, int, str]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 3:
            raise SystemExit(f"rc.tsv 行格式不对: {line!r}")
        leg, rc, secs = parts[0], parts[1], parts[2]
        try:
            rows.append((leg, int(rc), secs))
        except ValueError as exc:
            raise SystemExit(f"rc 不是整数: {line!r}") from exc
    if not rows:
        raise SystemExit("rc.tsv 为空——没有腿跑过，不能出证据包")
    return rows


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _images_from_run_manifest(run: Path) -> list[str]:
    """从本轮 `manifest.txt` 里回收镜像名。

    2026-10-24 实测：验证脚本已经把每个镜像的 `-> sha256` 写进 manifest 了，
    但打包时还要人手再传一遍 `--image`；忘了传，证据包里就只剩“（未提供）”，
    等于把**已经采到的证据丢掉**。所以缺省从 manifest 回退。
    """
    mf = run / "manifest.txt"
    if not mf.exists():
        return []
    names: list[str] = []
    for line in mf.read_text(encoding="utf-8", errors="replace").splitlines():
        if "->" not in line or line.lstrip().startswith("#"):
            continue
        name = line.split("->", 1)[0].strip()
        # 镜像名里不含空格；带空格的（如 `docker server 29.4.0`）不是镜像行
        if name and " " not in name and name not in names:
            names.append(name)
    return names


def build_manifest(
    run: Path,
    rows: list[tuple[str, int, str]],
    images: list[str],
    missing: list[str] | None = None,
) -> str:
    missing = missing or []
    commit = _run(["git", "rev-parse", "HEAD"])
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
    dirty = _run(["git", "status", "--porcelain"])
    image_ids = [f"- `{img}` → {_run(['docker', 'image', 'inspect', '--format', '{{.Id}}', img])}" for img in images]
    worst = max(rc for _, rc, _ in rows)
    verdict = "PASS" if worst == 0 and not missing else ("PARTIAL" if worst == 0 else "FAIL")

    lines = [
        f"# 离线 CI 证据 · {run.name}",
        "",
        f"- 结论（自动判定，非人手写）：**{verdict}**（最差腿 rc={worst}）",
    ]
    if missing:
        lines.append(
            f"- 覆盖度：**本轮只跑了 {len(rows)}/{len(rows) + len(missing)} 条腿**，"
            f"缺 → `{'`、`'.join(missing)}`。缺的腿没有证据，不要当成通过。"
        )
    lines += [
        f"- 分支 / 提交：`{branch}` @ `{commit}`",
        f"- 工作区：{'有未提交改动（见下）——证据对应的是工作区状态，不是该提交' if dirty else '干净（证据与上述提交严格对应）'}",
        f"- 打包时间：{time.strftime('%Y-%m-%dT%H:%M:%S%z')}",
        f"- 产物目录：`{run}`",
        "",
        "## 每条腿",
        "",
        "| 腿 | rc | 耗时 | 日志 |",
        "| --- | --- | --- | --- |",
    ]
    for leg, rc, secs in rows:
        log = run / f"{leg}.log"
        mark = "✅" if rc == 0 else "❌"
        lines.append(f"| {leg} | {rc} {mark} | {secs}s | {'`' + log.name + '`' if log.exists() else '（无）'} |")

    lines += ["", "## 镜像", ""]
    lines += image_ids or ["- （未提供，可用 `--image` 补充）"]
    lines += ["", "## 日志校验和（sha256）", "", "| 文件 | 字节 | sha256 |", "| --- | --- | --- |"]
    for f in sorted(run.glob("*")):
        if f.is_file():
            lines.append(f"| `{f.name}` | {f.stat().st_size} | `{sha256_of(f)}` |")

    if dirty:
        lines += ["", "## 未提交改动", "", "```", dirty, "```"]
    return "\n".join(lines) + "\n"


RERUN = """# 怎么自己复现这一包

不需要我们的机器，也不需要 GitHub Actions 网络（只有拉基础镜像要网）。

```bash
# 1) 重建验证镜像（一次即可）
docker build -f scripts/ci_offline/Dockerfile.python -t dd-ci-python:3.12 scripts/ci_offline
docker build -f scripts/ci_offline/Dockerfile.node   -t dd-ci-node:20   scripts/ci_offline

# 2) 按 CI 原命令跑默认 4 条腿（lint / test / helm / web）
bash scripts/verify_ci_offline.sh

# 3) 对照 package 里的 rc.tsv / SUMMARY.md / 各腿 .log
```

Windows/git-bash 上必须设 `MSYS_NO_PATHCONV=1`，否则 `-w /work` 会被改写成
`C:/Program Files/Git/work`，docker 直接 rc=125（看起来像"全挂"，其实一条腿都没跑）。
脚本已内置处理；手敲 docker 请照抄。

重腿（postgres / redis / supply-chain / web-e2e / web-e2e-real-backend）需显式点名，默认不跑。
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="打包离线 CI 证据")
    ap.add_argument("run", nargs="?", help="某一轮产物目录（默认取 artifacts/ci-offline 下最新）")
    ap.add_argument("--image", action="append", default=[], help="验证用的镜像名，可给多次")
    ap.add_argument("--out", default="artifacts/ci-evidence", help="证据包输出目录")
    args = ap.parse_args(argv)

    base = REPO / "artifacts" / "ci-offline"
    run = Path(args.run).resolve() if args.run else _latest_run(base)
    if run is None or not (run / "rc.tsv").exists():
        print(f"没找到含 rc.tsv 的产物目录（找过 {base}）", file=sys.stderr)
        return 2

    rows: dict[str, tuple[int, str]] = {}
    for leg, rc, secs in parse_rc(run / "rc.tsv"):
        rows[leg] = (rc, secs)
    print(f"轮次：{run.name}")
    for leg, (rc, secs) in rows.items():
        print(f"  {leg:<14} rc={rc} {secs}s")

    bad = [leg for leg, (rc, _) in rows.items() if rc != 0]
    if bad:
        print(f"\n拒绝出包：{len(bad)} 条腿未通过 → {', '.join(bad)}", file=sys.stderr)
        print("证据包只在全绿时生成。红着打包会让'有证据'变成'有包装过的假话'（见 docs/ci-offline.md）。", file=sys.stderr)
        return 1

    # 少一条腿比红一条腿更危险：只跑 4 条默认腿时，"全绿"很容易被读成"9 条都绿"。
    # 所以这里把缺的腿写进结论行，且不允许它显示为 PASS。
    missing = [leg for leg in ALL_LEGS if leg not in rows]
    if missing:
        print(
            f"\n注意：本轮只跑了 {len(rows)}/{len(ALL_LEGS)} 条腿，缺 → {', '.join(missing)}",
            file=sys.stderr,
        )
        print("包里会把结论标成 PARTIAL，不会写成 PASS。", file=sys.stderr)

    manifest = build_manifest(
        run,
        [(leg, rc, secs) for leg, (rc, secs) in rows.items()],
        args.image or _images_from_run_manifest(run),
        missing=missing,
    )
    out_dir = REPO / args.out
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    archive = out_dir / f"ci-evidence-{stamp}.tar.gz"

    # 内存里组包：先写 manifest / RERUN，再追加原始产物，避免在仓库里留下临时文件。
    with tarfile.open(archive, "w:gz") as tf:
        for name, text in (("EVIDENCE.md", manifest), ("RERUN.md", RERUN)):
            data = text.encode("utf-8")
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mtime = int(time.time())
            tf.addfile(info, io.BytesIO(data))
        for f in sorted(run.iterdir()):
            if f.is_file():
                tf.add(f, arcname=f"{run.name}/{f.name}")

    shown = archive.relative_to(REPO) if archive.is_relative_to(REPO) else archive
    print(f"\n已生成 {shown}（{archive.stat().st_size} 字节）")
    # 这里曾经无条件印 “PASS”，与实际覆盖度无关 —— 同一种“自己给自己下结论”的毛病。
    if missing:
        print(f"包内结论：PARTIAL（跑了 {len(rows)}/{len(rows) + len(missing)} 条腿，缺 {', '.join(missing)}）")
    else:
        print(f"包内结论：PASS（{len(rows)} 条腿全部 rc=0）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
