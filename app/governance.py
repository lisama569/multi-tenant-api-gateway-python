"""治理门面：把认证、授权、配额、幂等、审计编排为统一入口。

治理逻辑与接口层解耦：API 只依赖本门面，租户策略（窗口大小、
权限范围、凭据）可在不改动路由的前提下独立演进。
"""
from __future__ import annotations

import hashlib
import json
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from .audit import AuditLog, AuditRecord
from .errors import GovernanceError
from .idempotency import IdempotencyStore
from .quota import QuotaEnforcer, QuotaStatus
from .tenants import Credential, Tenant, TenantRegistry


@dataclass
class GovernanceContext:
    """一次请求的治理上下文。"""

    tenant: Tenant
    credential: Credential
    method: str
    path: str
    target_tenant_id: str


def _credential_hint(key: str | None) -> str | None:
    """只保留凭据前后片段，避免审计中泄露完整密钥。"""
    if not key:
        return None
    if len(key) <= 8:
        return key[:2] + "***"
    return f"{key[:4]}...{key[-4:]}"


def fingerprint_payload(method: str, path: str, payload: Any) -> str:
    """对请求内容计算稳定指纹，用于幂等冲突检测。"""
    canonical = json.dumps(
        {"method": method, "path": path, "payload": payload},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class Governance:
    def __init__(self, registry: TenantRegistry,
                 clock: Callable[[], float] | None = None,
                 audit_sink: Callable[[AuditRecord], None] | None = None,
                 audit_log: AuditLog | None = None,
                 quota: QuotaEnforcer | None = None,
                 idempotency: IdempotencyStore | None = None) -> None:
        self._clock = clock if clock is not None else time.monotonic
        self.registry = registry
        self.audit = audit_log or AuditLog(sink=audit_sink,
                                           clock=self._clock)
        self.quota = quota or QuotaEnforcer(registry, self._clock)
        self.idempotency = idempotency or IdempotencyStore()

    # ---- 内部：认证 + 租户边界 + 权限范围 -----------------------
    def _authenticate_authorize(
        self, credential_key: str | None, method: str, path: str,
        target_tenant_id: str, scope: str,
    ) -> GovernanceContext:
        hint = _credential_hint(credential_key)
        credential: Credential | None = None
        try:
            credential = self.registry.authenticate(credential_key)
            tenant = self.registry.authorize_tenant(
                credential, target_tenant_id
            )
            self.registry.authorize_scope(credential, scope)
        except GovernanceError as exc:
            self.audit.record(
                tenant_id=credential.tenant_id if credential else None,
                credential_hint=hint,
                method=method, path=path,
                target_tenant_id=target_tenant_id,
                decision="deny", reason=exc.code.value,
                detail={"message": exc.message, **exc.details},
            )
            raise
        return GovernanceContext(
            tenant=tenant, credential=credential, method=method, path=path,
            target_tenant_id=target_tenant_id,
        )

    # ---- 读路径 -------------------------------------------------
    def guard_read(self, credential_key: str | None, method: str, path: str,
                   target_tenant_id: str, scope: str) -> GovernanceContext:
        ctx = self._authenticate_authorize(
            credential_key, method, path, target_tenant_id, scope
        )
        try:
            status = self.quota.consume(target_tenant_id)
        except GovernanceError as exc:
            self.audit.record(
                tenant_id=target_tenant_id,
                credential_hint=_credential_hint(credential_key),
                method=method, path=path,
                target_tenant_id=target_tenant_id,
                decision="deny", reason=exc.code.value,
                detail=exc.details,
            )
            raise
        self.audit.record(
            tenant_id=target_tenant_id,
            credential_hint=_credential_hint(credential_key),
            method=method, path=path, target_tenant_id=target_tenant_id,
            decision="allow", reason="quota_consumed",
            detail={"remaining": status.remaining,
                    "window_resets_at": status.window_resets_at},
        )
        return ctx

    # ---- 写路径 -------------------------------------------------
    @contextmanager
    def guard_write(
        self, credential_key: str | None, method: str, path: str,
        target_tenant_id: str, scope: str,
        idempotency_key: str | None, payload: Any,
    ) -> Iterator[dict]:
        ctx = self._authenticate_authorize(
            credential_key, method, path, target_tenant_id, scope
        )
        hint = _credential_hint(credential_key)

        if not idempotency_key:
            # 未携带幂等键：照常消耗配额执行业务，不做幂等登记。
            status = self._consume_or_deny(
                target_tenant_id, method, path, hint, None
            )
            self.audit.record(
                tenant_id=target_tenant_id, credential_hint=hint,
                method=method, path=path, target_tenant_id=target_tenant_id,
                decision="allow", reason="quota_consumed",
                detail={"remaining": status.remaining},
            )
            decision = {"replay": False, "idempotency_key": None,
                        "response": None}
            try:
                yield decision
            except Exception:
                raise
            return

        fp = fingerprint_payload(method, path, payload)
        try:
            record, is_first = self.idempotency.begin(
                target_tenant_id, idempotency_key, fp
            )
        except GovernanceError as exc:
            self.audit.record(
                tenant_id=target_tenant_id, credential_hint=hint,
                method=method, path=path, target_tenant_id=target_tenant_id,
                idempotency_key=idempotency_key,
                decision="deny", reason=exc.code.value,
                detail=exc.details,
            )
            raise

        if not is_first:
            # 重放不消耗配额、不产生副作用。
            self.audit.record(
                tenant_id=target_tenant_id, credential_hint=hint,
                method=method, path=path, target_tenant_id=target_tenant_id,
                idempotency_key=idempotency_key,
                decision="replay", reason="idempotent_replay",
                detail={"fingerprint": fp},
            )
            yield {"replay": True, "idempotency_key": idempotency_key,
                   "response": record.response}
            return

        # 首次提交：消耗配额并执行业务；异常时释放幂等键。
        status = self._consume_or_deny(
            target_tenant_id, method, path, hint, idempotency_key
        )
        self.audit.record(
            tenant_id=target_tenant_id, credential_hint=hint,
            method=method, path=path, target_tenant_id=target_tenant_id,
            idempotency_key=idempotency_key,
            decision="allow", reason="quota_consumed",
            detail={"remaining": status.remaining, "fingerprint": fp},
        )
        decision = {"replay": False, "idempotency_key": idempotency_key,
                    "response": None}
        try:
            yield decision
        except BaseException:
            self.idempotency.fail(target_tenant_id, idempotency_key)
            raise
        self.idempotency.complete(
            target_tenant_id, idempotency_key, decision["response"]
        )

    def _consume_or_deny(
        self, tenant_id: str, method: str, path: str, hint: str | None,
        idempotency_key: str | None,
    ) -> QuotaStatus:
        try:
            return self.quota.consume(tenant_id)
        except GovernanceError as exc:
            self.audit.record(
                tenant_id=tenant_id, credential_hint=hint,
                method=method, path=path, target_tenant_id=tenant_id,
                idempotency_key=idempotency_key,
                decision="deny", reason=exc.code.value,
                detail=exc.details,
            )
            raise

    # ---- 自检类接口 --------------------------------------------
    def quota_status(self, credential_key: str | None, tenant_id: str,
                     ) -> tuple[GovernanceContext, QuotaStatus]:
        ctx = self._authenticate_authorize(
            credential_key, "GET", f"/tenants/{tenant_id}/quota",
            tenant_id, "quota:read"
        )
        return ctx, self.quota.status(tenant_id)

    def audit_trail(self, credential_key: str | None,
                    tenant_id: str) -> list[AuditRecord]:
        self._authenticate_authorize(
            credential_key, "GET", f"/tenants/{tenant_id}/audit",
            tenant_id, "audit:read"
        )
        return self.audit.list(tenant_id)

    def reset(self) -> None:
        self.quota.reset()
        self.idempotency.reset()
        self.audit.reset()
