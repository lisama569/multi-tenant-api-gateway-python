"""接口层：只做编排，治理逻辑全部委托 GovernanceEngine。

顺序固定为：凭据识别 -> 权限范围 -> 配额/限流 -> 幂等区段 -> 业务副作用，
因此任何拒绝都发生在副作用之前。配额与幂等判定结果以响应头回传，便于
调用方解释；GovernanceError 由统一处理器映射为可区分的对外错误。
"""

from __future__ import annotations

import hashlib
import logging
import threading
import uuid
from typing import Any

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from app.audit import AuditEntry, utc_now_iso
from app.errors import ErrorCode, GovernanceError
from app.governance import GovernanceEngine
from app.schemas import Item, ItemCreate

logger = logging.getLogger("governance.request")

SCOPE_READ = "items:read"
SCOPE_WRITE = "items:write"


class ItemStore:
    """演示用的租户资源存储；按 tenant_id 隔离。"""

    def __init__(self) -> None:
        self._items: dict[str, Item] = {}
        self._lock = threading.Lock()

    def create(self, tenant_id: str, payload: ItemCreate) -> Item:
        item = Item(
            id=uuid.uuid4().hex[:12],
            tenant_id=tenant_id,
            name=payload.name,
            data=payload.data,
        )
        with self._lock:
            self._items[item.id] = item
        return item

    def list_for(self, tenant_id: str) -> list[Item]:
        with self._lock:
            return [i for i in self._items.values() if i.tenant_id == tenant_id]

    def get(self, item_id: str) -> Item | None:
        with self._lock:
            return self._items.get(item_id)


def _canonical_request_hash(payload: ItemCreate) -> str:
    raw = payload.model_dump_json()
    return hashlib.sha256(raw.encode()).hexdigest()


def _quota_headers(remaining: int, reset_at: str, burst_remaining: int) -> dict[str, str]:
    return {
        "X-Quota-Remaining": str(remaining),
        "X-Quota-Reset-At": reset_at,
        "X-RateLimit-Remaining": str(burst_remaining),
    }


def register(app: FastAPI, engine: GovernanceEngine) -> None:
    store = ItemStore()

    @app.exception_handler(GovernanceError)
    async def _on_governance_error(_: Request, exc: GovernanceError) -> JSONResponse:
        headers: dict[str, str] = {}
        retry_after = exc.details.get("retry_after_seconds")
        if retry_after is not None:
            headers["Retry-After"] = str(retry_after)
        logger.info(
            "DENY tenant=%s code=%s reason-msg=%s details=%s",
            exc.tenant_id or "-", exc.code, exc.message, exc.details,
        )
        return JSONResponse(status_code=exc.http_status, content=exc.to_body(),
                            headers=headers)

    @app.exception_handler(ValidationError)
    async def _on_validation_error(_: Request, exc: ValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={"error": {"code": ErrorCode.VALIDATION_ERROR,
                               "message": "请求参数校验失败",
                               "details": exc.errors()}},
        )

    @app.get("/api/items")
    def list_items(
        request: Request,
        x_api_key: str | None = Header(default=None),
    ) -> dict[str, Any]:
        tenant = engine.authenticate(x_api_key)
        engine.authorize_scope(tenant, SCOPE_READ)
        remaining, reset_at, burst_remaining = engine.consume_quota(tenant)
        logger.info("ALLOW tenant=%s GET /api/items quota_remaining=%s",
                    tenant.id, remaining)
        items = store.list_for(tenant.id)
        return JSONResponse(
            content={"items": [i.model_dump() for i in items]},
            headers=_quota_headers(remaining, reset_at, burst_remaining),
        )

    @app.post("/api/items", status_code=201)
    def create_item(
        request: Request,
        payload: ItemCreate,
        x_api_key: str | None = Header(default=None),
        idempotency_key: str | None = Header(default=None),
    ) -> JSONResponse:
        logger.info(
            "INPUT tenant-key-present=%s POST /api/items payload=%s idempotency_key=%s",
            bool(x_api_key), payload.model_dump(), idempotency_key,
        )
        tenant = engine.authenticate(x_api_key)
        engine.authorize_scope(tenant, SCOPE_WRITE)
        remaining, reset_at, burst_remaining = engine.consume_quota(tenant)

        request_hash = _canonical_request_hash(payload)
        replay = engine.begin_idempotent(tenant, idempotency_key, request_hash)
        if replay is not None:
            logger.info("ALLOW tenant=%s POST /api/items IDEMPOTENT-REPLAY key=%s",
                        tenant.id, idempotency_key)
            headers = _quota_headers(remaining, reset_at, burst_remaining)
            headers["X-Idempotent-Replay"] = "true"
            return JSONResponse(status_code=200, content=replay, headers=headers)

        # 幂等占位之后才发生真正的副作用，且只发生一次
        item = store.create(tenant.id, payload)
        body: dict[str, Any] = {"item": item.model_dump()}
        if idempotency_key is not None:
            engine.store_idempotent(tenant, idempotency_key, request_hash, body)

        logger.info("ALLOW tenant=%s POST /api/items CREATED item_id=%s",
                    tenant.id, item.id)
        headers = _quota_headers(remaining, reset_at, burst_remaining)
        return JSONResponse(status_code=201, content=body, headers=headers)

    @app.get("/api/items/{item_id}")
    def get_item(
        request: Request,
        item_id: str,
        x_api_key: str | None = Header(default=None),
    ) -> dict[str, Any]:
        tenant = engine.authenticate(x_api_key)
        engine.authorize_scope(tenant, SCOPE_READ)
        remaining, reset_at, burst_remaining = engine.consume_quota(tenant)

        item = store.get(item_id)
        if item is None:
            engine.audit_sink.record(
                AuditEntry(utc_now_iso(), tenant.id, "GET /api/items/{id}",
                           "deny", ErrorCode.NOT_FOUND, {"resource_id": item_id})
            )
            raise GovernanceError(
                ErrorCode.NOT_FOUND, f"资源 '{item_id}' 不存在",
                tenant_id=tenant.id,
                details={"resource": "item", "resource_id": item_id},
            )
        # 资源存在但属于其他租户 -> 明确归类为跨租户访问
        engine.assert_same_tenant(tenant, item.tenant_id)
        logger.info("ALLOW tenant=%s GET /api/items/%s quota_remaining=%s",
                    tenant.id, item_id, remaining)
        return JSONResponse(
            content={"item": item.model_dump()},
            headers=_quota_headers(remaining, reset_at, burst_remaining),
        )
