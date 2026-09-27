"""统一的治理错误类型与对外错误分类。"""
from __future__ import annotations

from enum import Enum


class ErrorCode(str, Enum):
    """对外可区分的错误码。"""

    MISSING_CREDENTIAL = "missing_credential"
    INVALID_CREDENTIAL = "invalid_credential"
    INSUFFICIENT_SCOPE = "insufficient_scope"
    CROSS_TENANT_ACCESS = "cross_tenant_access"
    QUOTA_EXCEEDED = "quota_exceeded"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    IDEMPOTENCY_IN_FLIGHT = "idempotency_in_flight"
    NOT_FOUND = "not_found"
    INTERNAL_ERROR = "internal_error"


class GovernanceError(Exception):
    """治理层抛出的统一异常。

    携带错误码、人类可读原因以及可选的可解释上下文
    （如剩余额度、可重试时机）。
    """

    def __init__(
        self,
        code: ErrorCode,
        message: str,
        *,
        status_code: int = 403,
        details: dict | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}

    def to_dict(self) -> dict:
        return {
            "error": {
                "code": self.code.value,
                "message": self.message,
                "details": self.details,
            }
        }
