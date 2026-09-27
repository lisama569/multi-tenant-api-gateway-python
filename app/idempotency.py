"""写操作的幂等处理。"""
from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import Any

from .errors import ErrorCode, GovernanceError

# 进行中请求的最长等待时间（秒）：防止并发同键提交无限挂起。
IN_FLIGHT_WAIT_SECONDS = 5.0


@dataclass
class IdempotencyRecord:
    """一条幂等记录。键空间按租户隔离。"""

    key: str
    tenant_id: str
    fingerprint: str
    completed: bool = False
    response: Any = None
    event: threading.Event | None = None


class IdempotencyStore:
    """幂等键的登记、重放与冲突检测。

    同一 (租户, 键) 的判定规则：
    - 首次请求：登记为“进行中”，begin 返回 (record, True)；
    - 内容指纹相同、已完成：重放缓存响应（不产生重复副作用）；
    - 内容指纹相同、进行中：阻塞等待首个请求完成后重放；
    - 内容指纹不同：无论进行中或已完成，一律明确拒绝（键被复用）。

    所有判定在同一把锁下完成，保证并发下状态一致。
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._records: dict[tuple[str, str], IdempotencyRecord] = {}

    def begin(self, tenant_id: str, key: str,
              fingerprint: str) -> tuple[IdempotencyRecord, bool]:
        """登记一次请求，返回 (记录, 是否首次)。

        非首次但内容一致且已完成时，record.completed 为 True，
        调用方应直接重放 record.response。
        """
        with self._lock:
            storage_key = (tenant_id, key)
            record = self._records.get(storage_key)
            if record is None:
                record = IdempotencyRecord(
                    key=key, tenant_id=tenant_id, fingerprint=fingerprint,
                    event=threading.Event(),
                )
                self._records[storage_key] = record
                return record, True

            if record.fingerprint != fingerprint:
                raise GovernanceError(
                    ErrorCode.IDEMPOTENCY_CONFLICT,
                    "幂等标识已被用于不同的请求内容，禁止复用。",
                    status_code=409,
                    details={
                        "idempotency_key": key,
                        "stored_fingerprint": record.fingerprint,
                        "request_fingerprint": fingerprint,
                    },
                )

            if record.completed:
                return record, False

            # 相同内容、首个请求仍在进行：在锁外等待其完成。
            event = record.event
        if not event.wait(IN_FLIGHT_WAIT_SECONDS):
            raise GovernanceError(
                ErrorCode.IDEMPOTENCY_IN_FLIGHT,
                "相同幂等标识的请求正在处理中，请稍后重试。",
                status_code=409,
                details={"idempotency_key": key,
                         "retry_after_seconds": 0.1},
            )
        with self._lock:
            record = self._records[(tenant_id, key)]
            if not record.completed:
                # 首个请求失败并释放了键：本次成为新的首次请求。
                record = IdempotencyRecord(
                    key=key, tenant_id=tenant_id, fingerprint=fingerprint,
                    event=threading.Event(),
                )
                self._records[(tenant_id, key)] = record
                return record, True
            return record, False

    def complete(self, tenant_id: str, key: str, response: Any) -> None:
        """将进行中的请求标记为完成并缓存响应。"""
        with self._lock:
            record = self._records[(tenant_id, key)]
            record.completed = True
            record.response = response
            event = record.event
            record.event = None
        if event is not None:
            event.set()

    def fail(self, tenant_id: str, key: str) -> None:
        """进行中的请求失败：删除记录并唤醒等待者，允许同键重试。"""
        with self._lock:
            record = self._records.pop((tenant_id, key), None)
            event = record.event if record is not None else None
        if event is not None:
            event.set()

    def reset(self) -> None:
        with self._lock:
            self._records.clear()
