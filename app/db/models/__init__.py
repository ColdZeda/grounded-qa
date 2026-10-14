"""统一导出所有 ORM 模型。

⚠️ **这个文件的存在意义只有一个：让 `Base.metadata` 保持完整。**
    Alembic 的 autogenerate 只扫描**已经被导入过**的模型类。
    漏导入 = 生成的迁移里**少一张表**，而且**不报错**（等上线才发现表不存在）。
    → **新增模型必须在这里补一行导入。**
"""

from app.db.models.conversation import Conversation, Message, MessageRole
from app.db.models.knowledge import EMBEDDING_DIM, Chunk, Document, DocumentStatus
from app.db.models.tenant import Tenant
from app.db.models.user import User, UserRole

__all__ = [
    "EMBEDDING_DIM",
    "Chunk",
    "Conversation",
    "Document",
    "DocumentStatus",
    "Message",
    "MessageRole",
    "Tenant",
    "User",
    "UserRole",
]
