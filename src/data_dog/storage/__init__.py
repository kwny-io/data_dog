"""storage —— 持久化层（SQLAlchemy 2.0）。

设计目标：把项目里**内存态**的核心对象落库，重启不丢（把交付从 L1 推到 L2）。

- **本地/无设施**：默认 SQLite（Python 自带，零安装，文件即库）。
- **生产**：设环境变量 `DD_DATABASE_URL` 切 PostgreSQL，**代码不改**：
      sqlite:///data_dog.db
      postgresql+psycopg://user:pw@host:5432/data_dog

子模块：
  db        引擎 / 会话 / Base / init_db
  models    ORM 表（遥测事件、候选、调查报告、Alert、Outbox）
  repository 仓储层：写入与查询（供 P2/P3 调用）
"""
