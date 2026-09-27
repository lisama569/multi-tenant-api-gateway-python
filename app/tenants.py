"""租户、凭据与权限范围模型。"""
from __future__ import annotations

from dataclasses import dataclass, field

from .errors import ErrorCode, GovernanceError


@dataclass(frozen=True)
class Credential:
    """一把凭据：映射到唯一租户，并携带权限范围集合。"""

    key: str
    tenant_id: str
    scopes: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class Tenant:
    """租户及其治理策略。"""

    tenant_id: str
    name: str
    scopes: frozenset[str] = field(default_factory=frozenset)
    quota_limit: int = 100
    quota_window_seconds: int = 60


class TenantRegistry:
    """凭据 -> 租户 的查询注册表（只读模型）。

    认证、租户归属与权限范围是三种相互独立、错误可区分的判定：
    - 缺少凭据 -> MISSING_CREDENTIAL (401)
    - 凭据无效 -> INVALID_CREDENTIAL (401)
    - 跨租户   -> CROSS_TENANT_ACCESS (403)
    - 权限不足 -> INSUFFICIENT_SCOPE (403)
    """

    def __init__(self, tenants: list[Tenant] | None = None,
                 credentials: list[Credential] | None = None) -> None:
        self._tenants: dict[str, Tenant] = {
            t.tenant_id: t for t in (tenants or [])
        }
        self._credentials: dict[str, Credential] = {
            c.key: c for c in (credentials or [])
        }

    def get_tenant(self, tenant_id: str) -> Tenant | None:
        return self._tenants.get(tenant_id)

    def authenticate(self, credential_key: str | None) -> Credential:
        if not credential_key:
            raise GovernanceError(
                ErrorCode.MISSING_CREDENTIAL,
                "缺少 API 凭据，请通过 X-API-Key 请求头提供。",
                status_code=401,
            )
        credential = self._credentials.get(credential_key)
        if credential is None:
            raise GovernanceError(
                ErrorCode.INVALID_CREDENTIAL,
                "提供的 API 凭据无效或已被吊销。",
                status_code=401,
            )
        return credential

    def authorize_tenant(self, credential: Credential,
                         target_tenant_id: str) -> Tenant:
        tenant = self.get_tenant(target_tenant_id)
        if tenant is None:
            # 对不存在的租户不暴露其存在性：统一按越权处理。
            raise GovernanceError(
                ErrorCode.CROSS_TENANT_ACCESS,
                "无权访问目标租户资源。",
                status_code=403,
                details={"target_tenant_id": target_tenant_id},
            )
        if credential.tenant_id != target_tenant_id:
            raise GovernanceError(
                ErrorCode.CROSS_TENANT_ACCESS,
                "禁止跨租户访问：凭据归属租户与目标租户不一致。",
                status_code=403,
                details={
                    "credential_tenant_id": credential.tenant_id,
                    "target_tenant_id": target_tenant_id,
                },
            )
        return tenant

    def authorize_scope(self, credential: Credential, scope: str) -> None:
        if scope not in credential.scopes:
            raise GovernanceError(
                ErrorCode.INSUFFICIENT_SCOPE,
                f"凭据缺少所需权限范围：{scope}。",
                status_code=403,
                details={"required_scope": scope,
                         "granted_scopes": sorted(credential.scopes)},
            )
