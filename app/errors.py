"""可区分的对外错误分类。

每种治理失败都有稳定的机器可读 ``code``、固定的 HTTP 状态码与人类可读
``message``，配额类错误额外携带额度与重试时机，便于调用方区分处理。
"""

from __future__ import annotations

from typing import Any


class ErrorCode:
    # 凭据与授权
    MISSING_CREDENTIALS = "missing_credentials"
    INVALID_CREDENTIALS = "invalid_credentials"
    INSUFFICIENT_SCOPE = "insufficient_scope"
    CROSS_TENANT_ACCESS = "cross_tenant_access"
    # 配额与限流
    QUOTA_EXCEEDED = "quota_exceeded"
    RATE_LIMITED = "rate_limited"
    # 幂等
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    REPLAY_IN_PROGRESS = "replay_in_progress"
    # 资源 / 请求
    NOT_FOUND = "not_found"
    VALIDATION_ERROR = "validation_error"


# 错误码 -> HTTP 状态码
HTTP_STATUS: dict[str, int] = {
    ErrorCode.MISSING_CREDENTIALS: 401,
    ErrorCode.INVALID_CREDENTIALS: 401,
    ErrorCode.INSUFFICIENT_SCOPE: 403,
    ErrorCode.CROSS_TENANT_ACCESS: 403,
    ErrorCode.QUOTA_EXCEEDED: 429,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.IDEMPOTENCY_CONFLICT: 409,
    ErrorCode.REPLAY_IN_PROGRESS: 409,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.VALIDATION_ERROR: 422,
}

# 错误码 -> 是否在 Retry-After / 详情中给出可重试时机
RETRYABLE: frozenset[str] = frozenset(
    {ErrorCode.QUOTA_EXCEEDED, ErrorCode.RATE_LIMITED, ErrorCode.REPLAY_IN_PROGRESS}
)


class GovernanceError(Exception):
    """治理层抛出的、可直接映射为对外响应的错误。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        tenant_id: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.tenant_id = tenant_id
        self.details: dict[str, Any] = details or {}

    @property
    def http_status(self) -> int:
        return HTTP_STATUS[self.code]

    @property
    def retryable(self) -> bool:
        return self.code in RETRYABLE

    def to_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "error": {
                "code": self.code,
                "message": self.message,
            }
        }
        if self.tenant_id is not None:
            body["error"]["tenant_id"] = self.tenant_id
        if self.details:
            body["error"]["details"] = self.details
        return body
