"""知识库：解析 / 切分 / 入库。

⚠️ **为什么这层在 `app/` 里，而不是留在 `scripts/kb/ingest.py`**（2026-10-17 重构）：

    入库现在有**两个入口** —— 离线 CLI（灌语料）和 mini worker（处理上传）。
    如果解析/切分/写库的逻辑留在脚本里，worker 只能 `from scripts...` 去 import，
    而 `scripts/` **不在 `app` 包里**（镜像是 `uv sync --no-install-project`），
    所以那条 import 在容器里必然失败 —— 属于"本地跑得好好的，部署就炸"那一类。

    分界线是**纯不纯**：
      · `text.py`  —— 纯函数（字符串进、字符串出），不碰库、不碰网络、不碰磁盘（`chunk_file` 除外）
      · `ingest.py` —— 碰库、碰 embedding API

    这样"切分质量怎么调"和"入库怎么写得幂等"可以分开看、分开测。
"""
