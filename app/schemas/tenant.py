"""租户相关的出参。"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict


class TenantOut(BaseModel):
    """租户信息（管理端可见）。"""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    slug: str
    name: str
    is_active: bool
