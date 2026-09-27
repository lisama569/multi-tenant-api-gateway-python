"""按租户维度的固定窗口配额与限流。"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Callable

from .errors import ErrorCode, GovernanceError
from .tenants import TenantRegistry


@dataclass(frozen=True)
class QuotaStatus:
    """某次配额判定后的快照，用于对外解释。"""

    tenant_id: str
    limit: int
    used: int
    remaining: int
    window_started_at: float
    window_resets_at: float


class _Window:
    __slots__ = ("started_at", "used")

    def __init__(self, started_at: float) -> None:
        self.started_at = started_at
        self.used = 0


class QuotaEnforcer:
    """固定窗口配额计数器。

    语义：
    - 每个租户拥有独立窗口 [started_at, started_at + window_seconds)；
    - 窗口内被接受的请求计数达到 limit 后，新请求被拒绝，直到窗口滚动；
    - “检查 + 计数 + 快照”在同一把锁内原子完成，
      因此突发并发下不会出现计数漂移或超额放行；
    - 窗口边界判定以注入时钟为准，测试中可精确推进时间。
    """

    def __init__(self, registry: TenantRegistry,
                 clock: Callable[[], float]) -> None:
        self._registry = registry
        self._clock = clock
        self._lock = threading.Lock()
        self._windows: dict[str, _Window] = {}

    def _snapshot_locked(self, tenant_id: str, limit: int,
                         window_seconds: int) -> QuotaStatus:
        window = self._windows[tenant_id]
        return QuotaStatus(
            tenant_id=tenant_id,
            limit=limit,
            used=window.used,
            remaining=max(0, limit - window.used),
            window_started_at=window.started_at,
            window_resets_at=window.started_at + window_seconds,
        )

    def _roll_window_locked(self, tenant_id: str,
                            window_seconds: int) -> _Window:
        now = self._clock()
        window = self._windows.get(tenant_id)
        if window is None or now >= window.started_at + window_seconds:
            window = _Window(now)
            self._windows[tenant_id] = window
        return window

    def _reject(self, snapshot: QuotaStatus) -> GovernanceError:
        retry_after = max(0.0, snapshot.window_resets_at - self._clock())
        return GovernanceError(
            ErrorCode.QUOTA_EXCEEDED,
            "租户在当前配额窗口内的请求额度已用尽。",
            status_code=429,
            details={
                "tenant_id": snapshot.tenant_id,
                "limit": snapshot.limit,
                "used": snapshot.used,
                "remaining": snapshot.remaining,
                "window_resets_at": snapshot.window_resets_at,
                "retry_after_seconds": retry_after,
            },
        )

    def consume(self, tenant_id: str) -> QuotaStatus:
        """原子地消耗 1 个配额；额度不足时抛出 QUOTA_EXCEEDED。"""
        tenant = self._registry.get_tenant(tenant_id)
        if tenant is None:  # 防御性：正常流程已在授权阶段拦截
            raise GovernanceError(
                ErrorCode.CROSS_TENANT_ACCESS, "未知租户。", status_code=403
            )
        with self._lock:
            window = self._roll_window_locked(
                tenant_id, tenant.quota_window_seconds
            )
            snapshot = self._snapshot_locked(
                tenant_id, tenant.quota_limit, tenant.quota_window_seconds
            )
            if window.used >= tenant.quota_limit:
                raise self._reject(snapshot)
            window.used += 1
            return self._snapshot_locked(
                tenant_id, tenant.quota_limit, tenant.quota_window_seconds
            )

    def status(self, tenant_id: str) -> QuotaStatus:
        """读取当前窗口快照，不消耗配额（不存在窗口时初始化空窗口）。"""
        tenant = self._registry.get_tenant(tenant_id)
        if tenant is None:
            raise GovernanceError(
                ErrorCode.CROSS_TENANT_ACCESS, "未知租户。", status_code=403
            )
        with self._lock:
            self._roll_window_locked(
                tenant_id, tenant.quota_window_seconds
            )
            return self._snapshot_locked(
                tenant_id, tenant.quota_limit, tenant.quota_window_seconds
            )

    def reset(self) -> None:
        """清空全部计数（测试辅助）。"""
        with self._lock:
            self._windows.clear()
