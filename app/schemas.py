"""请求/响应数据模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class ItemCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    data: str = Field(default="", max_length=4000)


class Item(BaseModel):
    id: str
    tenant_id: str
    name: str
    data: str
