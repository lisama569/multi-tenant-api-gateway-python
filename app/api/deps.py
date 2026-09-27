"""共享依赖：种子租户、凭据与单例治理组件。

测试可通过 app.dependency_overrides[get_governance] 注入
带确定性时钟的 Governance 实例。
"""
from __future__ import annotations

import time
from typing import Callable

from ..governance import Governance
from ..tenants import Credential, Tenant, TenantRegistry


def get_clock() -> Callable[[], float]:
    return time.monotonic


def build_registry() -> TenantRegistry:
    """种子数据：

    - tenant-a：读写权限，每窗口 3 个请求；
    - tenant-b：只读权限；
    - tenant-c：带审计/配额自检权限。
    """
    tenants = [
        Tenant("tenant-a", "Alpha",
               frozenset({"items:read", "items:write"}),
               quota_limit=3, quota_window_seconds=60),
        Tenant("tenant-b", "Beta",
               frozenset({"items:read"}),
               quota_limit=5, quota_window_seconds=60),
        Tenant("tenant-c", "Gamma",
               frozenset({"items:read", "items:write",
                          "quota:read", "audit:read"}),
               quota_limit=10, quota_window_seconds=60),
    ]
    credentials = [
        Credential("ak-tenant-a", "tenant-a",
                   frozenset({"items:read", "items:write"})),
        Credential("ak-tenant-b", "tenant-b",
                   frozenset({"items:read"})),
        Credential("ak-tenant-c", "tenant-c",
                   frozenset({"items:read", "items:write",
                              "quota:read", "audit:read"})),
    ]
    return TenantRegistry(tenants, credentials)


_default_governance: Governance | None = None


def get_governance() -> Governance:
    global _default_governance
    if _default_governance is None:
        _default_governance = Governance(
            build_registry(), clock=get_clock()
        )
    return _default_governance
