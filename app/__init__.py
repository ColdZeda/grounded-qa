"""grounded-qa 后端。

目录职责（详见 AGENTS.md §1.2）：

    api/        HTTP 路由 / SSE 流式
    core/       配置、鉴权、多租户、日志、计量
    rag/        解析 → 切分 → 嵌入 → 检索 → 精排 → 生成 → 拒答
    channels/   渠道适配层（web / feishu）
    db/         表模型、迁移、仓储层
    workers/    异步任务（自研 mini worker，Redis BLPOP）
"""

__version__ = "0.1.0"
