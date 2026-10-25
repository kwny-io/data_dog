"""把 `docs/data_plane.md` 里被标记的接入示例抽出来，打印成一行 JSON。

单一真源：文档 → 工具（`tools/verify_compose_web.sh`）→ 守卫测试
（`tests/test_doc_ingest_example.py`）。三处都读同一段文字，谁都不许再手写一份样例。
"""

from __future__ import annotations

import json
import pathlib
import re
import sys

START = "<!-- dd-doc-sample: ingest-event -->"
END = "<!-- /dd-doc-sample: ingest-event -->"


def extract(doc: pathlib.Path) -> dict:
    text = doc.read_text(encoding="utf-8")
    if START not in text or END not in text:
        raise SystemExit(f"{doc} 缺少采样标记 {START} / {END}")
    block = text.split(START, 1)[1].split(END, 1)[0]
    m = re.search(r"```json\n(.*?)```", block, re.S)
    if not m:
        raise SystemExit("标记之间不是 ```json 代码块")
    return json.loads(m.group(1))


def main(argv: list[str]) -> int:
    doc = pathlib.Path(argv[1] if len(argv) > 1 else "docs/data_plane.md")
    print(json.dumps(extract(doc), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
