"""租户治理核心：识别鉴权、配额限流、幂等。

该模块不依赖任何 Web 框架（与接口层解耦），所有“检查—判定—变更”都在同一
把锁内完成，因此窗口边界与并发突发下不会出现计数漂移或超额放行。时间源可
注入，便于对窗口边界做确定性测试；租户策略通过 Tenant 数据驱动演进。
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Protocol

from app.audit import AuditEntry, AuditSink, utc_now_iso
from app.errors import ErrorCode, GovernanceError

# 突发限流的滑动窗口长度（秒）：每租户每秒最多 burst_limit 个请求
BURST_WINDOW_SECONDS = 1.0


# ---------------------------------------------------------------------------
# 租户模型
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Tenant:
    id: str
    api_key: str
    scopes: frozenset[str]
    quota_limit: int        # 每个固定窗口的配额上限
    window_seconds: int     # 配额固定窗口长度
    burst_limit: int        # 每秒突发上限


class TenantRegistry(Protocol):
    def get_by_api_key(self, api_key: str) -> Tenant | None:
        ...

    def get_by_id(self, tenant_id: str) -> Tenant | None:
        ...


# ---------------------------------------------------------------------------
# 内部状态
# ---------------------------------------------------------------------------

@dataclass
class _QuotaState:
    window_start: float = 0.0
    count: int = 0
    burst_hits: list[float] = field(default_factory=list)


@dataclass
class IdempotencyRecord:
    request_hash: str
    response: dict[str, Any]
    created_at: float
    in_progress: bool


@dataclass
class Admission:
    tenant: Tenant
    quota_remaining: int
    reset_at: str
    burst_remaining: int
    replay: dict[str, Any] | None = None


TimeProvider = Callable[[], float]


def _iso_from_epoch(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# 治理引擎
# ---------------------------------------------------------------------------

class GovernanceEngine:
    def __init__(
        self,
        registry: TenantRegistry,
        audit_sink: AuditSink,
        *,
        time_provider: TimeProvider | None = None,
    ) -> None:
        self.registry = registry
        self.audit_sink = audit_sink
        self._now = time_provider or time.time
        self._lock = threading.RLock()
        self._quota: dict[str, _QuotaState] = {}
        self._idem: dict[tuple[str, str], IdempotencyRecord] = {}

    # -- 审计辅助 -----------------------------------------------------------

    def _audit(self, tenant_id: str, action: str, decision: str, reason: str,
               summary: dict[str, Any] | None = None) -> None:
        self.audit_sink.record(
            AuditEntry(
                timestamp=utc_now_iso(),
                tenant_id=tenant_id,
                action=action,
                decision=decision,
                reason=reason,
                request_summary=summary or {},
            )
        )

    def _deny(self, code: str, message: str, *, tenant_id: str, action: str,
              reason: str, summary: dict[str, Any] | None = None,
              details: dict[str, Any] | None = None) -> None:
        self._audit(tenant_id, action, "deny", reason, summary)
        raise GovernanceError(code, message, tenant_id=tenant_id or None,
                              details=details)

    # -- 凭据识别 -----------------------------------------------------------

    def authenticate(self, api_key: str | None) -> Tenant:
        if not api_key:
            self._deny(
                ErrorCode.MISSING_CREDENTIALS,
                "缺少 API 凭据：请通过 X-API-Key 请求头提供",
                tenant_id="",
                action="authenticate",
                reason=ErrorCode.MISSING_CREDENTIALS,
            )
        tenant = self.registry.get_by_api_key(api_key)
        if tenant is None:
            self._deny(
                ErrorCode.INVALID_CREDENTIALS,
                "凭据无效或已被吊销",
                tenant_id="",
                action="authenticate",
                reason=ErrorCode.INVALID_CREDENTIALS,
                summary={"api_key_fingerprint": hashlib_fingerprint(api_key)},
            )
        self._audit(tenant.id, "authenticate", "allow", "authenticated",
                    {"tenant_id": tenant.id})
        return tenant

    # -- 权限范围 -----------------------------------------------------------

    def authorize_scope(self, tenant: Tenant, required_scope: str) -> None:
        if required_scope in tenant.scopes:
            self._audit(tenant.id, f"authorize_scope:{required_scope}", "allow",
                        "scope_granted")
            return
        self._deny(
            ErrorCode.INSUFFICIENT_SCOPE,
            f"权限范围不足，需要 scope '{required_scope}'",
            tenant_id=tenant.id,
            action=f"authorize_scope:{required_scope}",
            reason=f"scope_missing:{required_scope}",
            summary={"held_scopes": sorted(tenant.scopes)},
            details={"required_scope": required_scope,
                     "held_scopes": sorted(tenant.scopes)},
        )

    # -- 租户边界 -----------------------------------------------------------

    def assert_same_tenant(self, tenant: Tenant, resource_tenant_id: str) -> None:
        if resource_tenant_id == tenant.id:
            self._audit(tenant.id, "tenant_boundary", "allow", "same_tenant",
                        {"resource_tenant_id": resource_tenant_id})
            return
        self._deny(
            ErrorCode.CROSS_TENANT_ACCESS,
            "禁止访问或操作其他租户的资源",
            tenant_id=tenant.id,
            action="tenant_boundary",
            reason=ErrorCode.CROSS_TENANT_ACCESS,
            summary={"resource_tenant_id": resource_tenant_id},
            details={"resource_tenant_id": resource_tenant_id,
                     "expected_tenant_id": tenant.id},
        )

    # -- 配额与突发限流 -----------------------------------------------------

    def _state_locked(self, tenant: Tenant) -> _QuotaState:
        state = self._quota.get(tenant.id)
        if state is None:
            state = _QuotaState()
            self._quota[tenant.id] = state
        return state

    def consume_quota(self, tenant: Tenant) -> tuple[int, str, int]:
        """原子地消耗一个配额。

        返回 ``(配额剩余, 窗口重置时间ISO, 突发剩余)``；配额/突发不足时在
        变更计数之前拒绝，因此被拒请求不消耗任何额度。
        """
        with self._lock:
            now = self._now()
            state = self._state_locked(tenant)

            # 固定窗口：过期则整体重置（窗口起点对齐到首个请求）
            if now >= state.window_start + tenant.window_seconds:
                state.window_start = now
                state.count = 0

            reset_epoch = state.window_start + tenant.window_seconds
            reset_at = _iso_from_epoch(reset_epoch)

            # 1) 配额判定（先于突发，错误优先级可解释）
            if state.count >= tenant.quota_limit:
                retry_after = max(1, math.ceil(reset_epoch - now))
                self._deny(
                    ErrorCode.QUOTA_EXCEEDED,
                    f"租户配额已用尽：每 {tenant.window_seconds}s 限 "
                    f"{tenant.quota_limit} 次",
                    tenant_id=tenant.id,
                    action="consume_quota",
                    reason=ErrorCode.QUOTA_EXCEEDED,
                    summary={"used": state.count, "limit": tenant.quota_limit},
                    details={
                        "quota_used": state.count,
                        "quota_limit": tenant.quota_limit,
                        "quota_remaining": 0,
                        "window_seconds": tenant.window_seconds,
                        "reset_at": reset_at,
                        "retry_after_seconds": retry_after,
                    },
                )

            # 2) 突发判定：滑动 1 秒窗口，先清理过期命中
            state.burst_hits = [t for t in state.burst_hits
                                if now - t < BURST_WINDOW_SECONDS]
            if len(state.burst_hits) >= tenant.burst_limit:
                retry_after = max(
                    1, math.ceil(state.burst_hits[0] + BURST_WINDOW_SECONDS - now)
                )
                self._deny(
                    ErrorCode.RATE_LIMITED,
                    f"请求过于频繁：每秒突发上限 {tenant.burst_limit} 次",
                    tenant_id=tenant.id,
                    action="consume_quota",
                    reason=ErrorCode.RATE_LIMITED,
                    summary={"burst_in_window": len(state.burst_hits),
                             "burst_limit": tenant.burst_limit},
                    details={
                        "burst_limit": tenant.burst_limit,
                        "burst_window_seconds": BURST_WINDOW_SECONDS,
                        "retry_after_seconds": retry_after,
                    },
                )

            # 检查全部通过后才变更计数
            state.count += 1
            state.burst_hits.append(now)
            remaining = tenant.quota_limit - state.count
            burst_remaining = tenant.burst_limit - len(state.burst_hits)
            self._audit(
                tenant.id, "consume_quota", "allow", "quota_consumed",
                {"quota_remaining": remaining, "burst_remaining": burst_remaining,
                 "window_start": _iso_from_epoch(state.window_start),
                 "reset_at": reset_at},
            )
            return remaining, reset_at, burst_remaining

    # -- 幂等 ---------------------------------------------------------------

    def begin_idempotent(
        self,
        tenant: Tenant,
        idem_key: str | None,
        request_hash: str,
    ) -> dict[str, Any] | None:
        """开启幂等区段。

        - 无幂等键：返回 None，正常执行（不登记）。
        - 首次见到该 (租户, 键)：原子占位，返回 None，调用方完成后须
          调用 :meth:`store_idempotent` 保存响应。
        - 键已存在且请求哈希不同：抛 IDEMPOTENCY_CONFLICT。
        - 键相同但首个请求仍在处理：抛 REPLAY_IN_PROGRESS（并发重放）。
        - 键相同且已完成：返回已保存响应（重放，无重复副作用）。

        幂等键按租户命名空间隔离，跨租户不可见、不可互相冲突。
        """
        if idem_key is None:
            return None
        with self._lock:
            now = self._now()
            key = (tenant.id, idem_key)
            record = self._idem.get(key)
            if record is None:
                self._idem[key] = IdempotencyRecord(
                    request_hash=request_hash, response={},
                    created_at=now, in_progress=True,
                )
                self._audit(tenant.id, "idempotency.begin", "allow",
                            "idempotency_first", {"idempotency_key": idem_key})
                return None
            if record.request_hash != request_hash:
                self._deny(
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    "幂等标识已被用于不同的请求内容",
                    tenant_id=tenant.id,
                    action="idempotency.begin",
                    reason=ErrorCode.IDEMPOTENCY_CONFLICT,
                    summary={"idempotency_key": idem_key},
                    details={"idempotency_key": idem_key,
                             "original_at": _iso_from_epoch(record.created_at)},
                )
            if record.in_progress:
                self._deny(
                    ErrorCode.REPLAY_IN_PROGRESS,
                    "相同幂等标识的请求仍在处理中，请勿并发重放",
                    tenant_id=tenant.id,
                    action="idempotency.begin",
                    reason=ErrorCode.REPLAY_IN_PROGRESS,
                    summary={"idempotency_key": idem_key},
                    details={"idempotency_key": idem_key,
                             "retry_after_seconds": 1},
                )
            self._audit(tenant.id, "idempotency.begin", "replay",
                        "idempotency_replay", {"idempotency_key": idem_key})
            return dict(record.response)

    def store_idempotent(self, tenant: Tenant, idem_key: str,
                         request_hash: str, response: dict[str, Any]) -> None:
        with self._lock:
            record = self._idem.get((tenant.id, idem_key))
            if record is not None and record.request_hash == request_hash:
                record.response = dict(response)
                record.in_progress = False
                self._audit(tenant.id, "idempotency.store", "allow",
                            "idempotency_stored", {"idempotency_key": idem_key})


def hashlib_fingerprint(api_key: str) -> str:
    """仅记录凭据指纹，避免审计中出现完整密钥。"""
    import hashlib

    return hashlib.sha256(api_key.encode()).hexdigest()[:12]
