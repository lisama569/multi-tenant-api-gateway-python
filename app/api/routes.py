"""HTTP 路由：仅做参数解析与序列化，治理全部委托给 Governance。

数据存储为进程内字典，按 tenant_id 物理隔离；治理层保证任何租户
都无法越过路径租户参数读取他人数据。
"""
from __future__ import annotations

import itertools
import threading
from collections import defaultdict
from typing import Any

from fastapi import APIRouter, Depends, Header, Request
from pydantic import BaseModel

from ..governance import Governance
from .deps import get_governance

router = APIRouter()

# 进程内演示存储：{tenant_id: [item...]}，自带锁保证写入一致。
_store_lock = threading.Lock()
_items: dict[str, list[dict[str, Any]]] = defaultdict(list)
_id_seq = itertools.count(1)


def _new_item_id() -> int:
    """集中生成条目 ID，便于测试注入存储故障。"""
    return next(_id_seq)


class ItemIn(BaseModel):
    name: str
    payload: dict[str, Any] = {}


@router.get("/")
def read_root() -> dict:
    # 既有行为保持兼容：公开、无鉴权。
    return {"Hello": "World"}


@router.get("/tenants/{tenant_id}/items")
def list_items(
    tenant_id: str,
    request: Request,
    x_api_key: str | None = Header(default=None),
    gov: Governance = Depends(get_governance),
) -> dict:
    gov.guard_read(x_api_key, "GET", request.url.path,
                   tenant_id, "items:read")
    with _store_lock:
        items = list(_items[tenant_id])
    return {"tenant_id": tenant_id, "items": items}


@router.post("/tenants/{tenant_id}/items")
def create_item(
    tenant_id: str,
    body: ItemIn,
    request: Request,
    x_api_key: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None,
                                         alias="Idempotency-Key"),
    gov: Governance = Depends(get_governance),
) -> dict:
    payload = body.model_dump()
    with gov.guard_write(
        x_api_key, "POST", request.url.path, tenant_id, "items:write",
        idempotency_key, payload,
    ) as decision:
        if decision["replay"]:
            cached = dict(decision["response"])
            cached["replayed"] = True
            return cached

        with _store_lock:
            item = {"id": _new_item_id(), "tenant_id": tenant_id, **payload}
            _items[tenant_id].append(item)
        response = {"tenant_id": tenant_id, "item": item,
                    "idempotency_key": decision["idempotency_key"]}
        decision["response"] = response
        return response


@router.get("/tenants/{tenant_id}/quota")
def get_quota(
    tenant_id: str,
    request: Request,
    x_api_key: str | None = Header(default=None),
    gov: Governance = Depends(get_governance),
) -> dict:
    _, status = gov.quota_status(x_api_key, tenant_id)
    return {
        "tenant_id": status.tenant_id,
        "limit": status.limit,
        "used": status.used,
        "remaining": status.remaining,
        "window_started_at": status.window_started_at,
        "window_resets_at": status.window_resets_at,
    }


@router.get("/tenants/{tenant_id}/audit")
def get_audit(
    tenant_id: str,
    request: Request,
    x_api_key: str | None = Header(default=None),
    gov: Governance = Depends(get_governance),
) -> dict:
    records = gov.audit_trail(x_api_key, tenant_id)
    return {
        "tenant_id": tenant_id,
        "records": [
            {
                "seq": r.seq,
                "timestamp": r.timestamp,
                "tenant_id": r.tenant_id,
                "credential_hint": r.credential_hint,
                "method": r.method,
                "path": r.path,
                "decision": r.decision,
                "reason": r.reason,
                "target_tenant_id": r.target_tenant_id,
                "idempotency_key": r.idempotency_key,
                "detail": r.detail,
            }
            for r in records
        ],
    }
