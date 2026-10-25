"""文档与 .gitignore 的同源守卫：数据集不入库这件事必须写在文档里。

为什么钉这个：`/data/` 被 `.gitignore` 排除，全新克隆后**没有数据集**，文档 §6 那条
评估命令会直接 FileNotFoundError。这正是「文档与代码不同源」的典型形态——命令写得
对，前提却是假的。三个断言各自对应一个会真实发生的漂移：

1. 若哪天有人把 `/data/` 从 .gitignore 移除并真把数据提交了，那第 2 条断言（文档
   必须声明"不在仓库"）就该失效——所以它俩绑在一起。
2. 文档必须给出**生成命令**，否则读者只有一条跑不通的路。
3. 文档必须说明运行代价（小时级），否则会有人在演示前十分钟开跑。
"""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "mujoco_dataset.md"
GITIGNORE = ROOT / ".gitignore"


def test_data_dir_still_ignored() -> None:
    assert "/data/" in GITIGNORE.read_text(encoding="utf-8").splitlines(), (
        "/data/ 不再被 .gitignore 排除——如果数据集真的入库了，请回来更新本测试与"
        "docs/mujoco_dataset.md §6 的『先决条件』一节（那条命令现在能直接跑通）"
    )


def test_doc_states_dataset_not_in_repo_and_how_to_generate() -> None:
    text = DOC.read_text(encoding="utf-8")
    assert "不在仓库里" in text, "文档必须明说数据集不在仓库里，否则读者会以为命令能直接跑"
    assert "data_dog.mujoco_sim.gen_dataset" in text, "文档必须给出生成数据集的命令"
    assert "FileNotFoundError" in text, "文档必须写出跑不通时的具体报错，便于读者自查"
    assert "小时级" in text, "文档必须说明 n=2000 的耗时量级，避免有人在演示前开跑"
