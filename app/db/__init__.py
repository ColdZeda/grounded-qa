"""db：表模型、迁移（alembic）、仓储层。

v1.0 要点：
    所有业务表带 ``tenant_id``（多租户隔离）；
    向量表带 ``embedding_model`` / ``embedding_dim`` 元数据，用于拒绝混用向量。
"""
