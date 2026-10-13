"""workers：异步任务。

⚠️ 这里是**自研 mini worker**（Redis ``BLPOP``，约 40 行），**不是 Celery**。
    单机低频批处理场景，重型框架不划算 —— 看到没有 Celery 不要以为是漏了。
    （AGENTS.md §7.1 约定 7）

职责：文档解析 / 切分 / 向量化入库（串行执行 + 容器 mem_limit 兜底）。
"""
