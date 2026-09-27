"""默认租户目录。

目录本身是纯数据查询，治理策略（配额、限流、幂等）位于 GovernanceEngine，
未来可在不改动接口层的情况下替换为数据库/配置中心实现并做按租户策略演进。
"""

from __future__ import annotations

from app.governance import Tenant


class StaticTenantRegistry:
    def __init__(self, tenants: list[Tenant]) -> None:
        self._by_key: dict[str, Tenant] = {t.api_key: t for t in tenants}
        self._by_id: dict[str, Tenant] = {t.id: t for t in tenants}

    def get_by_api_key(self, api_key: str) -> Tenant | None:
        return self._by_key.get(api_key)

    def get_by_id(self, tenant_id: str) -> Tenant | None:
        return self._by_id.get(tenant_id)


def build_default_registry() -> StaticTenantRegistry:
    tenants = [
        Tenant(
            id="tenant-a",
            api_key="key-a-123",
            scopes=frozenset({"items:read", "items:write"}),
            quota_limit=5,
            window_seconds=60,
            burst_limit=5,
        ),
        Tenant(
            id="tenant-b",
            api_key="key-b-456",
            scopes=frozenset({"items:read", "items:write"}),
            quota_limit=3,
            window_seconds=60,
            burst_limit=3,
        ),
        # 只读租户：用于覆盖权限不足场景
        Tenant(
            id="tenant-ro",
            api_key="key-ro-789",
            scopes=frozenset({"items:read"}),
            quota_limit=10,
            window_seconds=60,
            burst_limit=10,
        ),
    ]
    return StaticTenantRegistry(tenants)
