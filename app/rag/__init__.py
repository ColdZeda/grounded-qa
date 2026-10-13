"""rag：解析 → 切分 → 嵌入 → 检索 → 精排 → 生成 → 拒答。

深度锚所在。v1.0 规划（骨架阶段先占位）：
    loaders.py     文档解析（Markdown / HTML）
    splitter.py    切分（走配置里的 chunk_size / chunk_overlap）
    embedder.py    embedding（走 API；带 embedding_model / embedding_dim 元数据锁校验）
    retriever.py   混合检索：全文 + 向量 → RRF 融合
    reranker.py    精排（走 API）
    generator.py   生成 + 引用溯源 + 低置信拒答
"""
