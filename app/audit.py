"""审计追溯：记录每次治理判定的结论与原因。

审计与接口层、治理引擎通过 ``AuditSink`` 协议解耦，默认实现为线程安全的
内存环形存储并打印结构化日志，便于本地测试观察；生产可替换为持久化实现。
"""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

logger = logging.getLogger("governance.audit")


@dataclass(frozen=True)
class AuditEntry:
    timestamp: str
    tenant_id: str
    action: str
    decision: str          # allow / deny / replay
    reason: str            # 判定依据，例如 scope_missing:items:write
    request_summary: dict[str, Any] = field(default_factory=dict)


class AuditSink(Protocol):
    def record(self, entry: AuditEntry) -> None:
        ...


class InMemoryAuditSink:
    """线程安全的内存审计存储，同时输出结构化 JSON 日志。"""

    def __init__(self, max_entries: int = 1024) -> None:
        self._entries: deque[AuditEntry] = deque(maxlen=max_entries)
        self._lock = threading.Lock()

    def record(self, entry: AuditEntry) -> None:
        with self._lock:
            self._entries.append(entry)
        # 审计日志不持锁输出，避免 IO 阻塞判定
        logger.info(json.dumps(asdict(entry), ensure_ascii=False, sort_keys=True))

    def entries(self) -> list[AuditEntry]:
        with self._lock:
            return list(self._entries)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
