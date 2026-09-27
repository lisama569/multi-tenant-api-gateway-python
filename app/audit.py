"""审计记录：完整反映租户、判定结论与原因。"""
from __future__ import annotations

import json
import sys
import threading
from dataclasses import asdict, dataclass, field
from typing import Any, Callable


@dataclass(frozen=True)
class AuditRecord:
    seq: int
    timestamp: float
    tenant_id: str | None
    credential_hint: str | None
    method: str
    path: str
    decision: str          # allow / deny / replay
    reason: str
    target_tenant_id: str | None = None
    idempotency_key: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


def _default_sink(record: AuditRecord) -> None:
    """默认输出：每行一条 JSON，便于本地验证与日志采集。"""
    print("AUDIT " + json.dumps(asdict(record), ensure_ascii=False,
                                default=str), file=sys.stderr)


class AuditLog:
    """线程安全的内存审计日志，并向可注入 sink 输出。"""

    def __init__(self, sink: Callable[[AuditRecord], None] | None = None,
                 clock: Callable[[], float] | None = None) -> None:
        self._lock = threading.Lock()
        self._records: list[AuditRecord] = []
        self._sink = sink if sink is not None else _default_sink
        self._clock = clock if clock is not None else (lambda: 0.0)

    def record(self, **kwargs: Any) -> AuditRecord:
        with self._lock:
            entry = AuditRecord(
                seq=len(self._records) + 1,
                timestamp=self._clock(),
                tenant_id=kwargs.get("tenant_id"),
                credential_hint=kwargs.get("credential_hint"),
                method=kwargs.get("method", ""),
                path=kwargs.get("path", ""),
                decision=kwargs.get("decision", ""),
                reason=kwargs.get("reason", ""),
                target_tenant_id=kwargs.get("target_tenant_id"),
                idempotency_key=kwargs.get("idempotency_key"),
                detail=dict(kwargs.get("detail") or {}),
            )
            self._records.append(entry)
        # sink 在锁外执行，避免日志输出阻塞其他请求。
        self._sink(entry)
        return entry

    def list(self, tenant_id: str | None = None) -> list[AuditRecord]:
        """返回审计记录；指定租户时仅返回与其相关的记录。

        - 该租户自身发起的所有判定始终可见；
        - 针对该租户的越权/拒绝尝试也对其可见（便于发现探测行为）；
        - 成功访问必然租户一致，不会泄露其他租户的正常流量。
        """
        with self._lock:
            if tenant_id is None:
                return list(self._records)
            return [
                r for r in self._records
                if r.tenant_id == tenant_id
                or (r.target_tenant_id == tenant_id
                    and r.decision == "deny")
            ]

    def reset(self) -> None:
        with self._lock:
            self._records.clear()
