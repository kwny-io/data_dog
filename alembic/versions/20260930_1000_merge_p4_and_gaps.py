"""merge P4 控制面（a2b3c4d5e6f7）与 G 系列缺口（d7e8f9a0b1c2）

两条并行开发链各自从共同基点 `b1c2d3e4f5a6`（G1）分叉：
- 左链（本分支）：G3 状态快照 → G7 变更记录，head 为 `d7e8f9a0b1c2`；
- 右链（P4 分支）：P4-2 算子注册表，head 为 `a2b3c4d5e6f7`。

合并两分支后 alembic 出现**双头**，`upgrade head` 会因歧义失败。本 merge 版本
把两头并为一头（无 schema 变更——两链各自新增独立表，无字段级冲突），使后续
迁移链恢复单头。

纯 reconcile：upgrade/downgrade 均为 no-op。
"""
from __future__ import annotations

from collections.abc import Sequence

revision: str = 'e8f9a0b1c2d3'
down_revision: str | Sequence[str] | None = ('a2b3c4d5e6f7', 'd7e8f9a0b1c2')
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
