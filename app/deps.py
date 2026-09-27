"""依赖装配：把治理引擎、审计存储等单例提供给接口层。"""

from __future__ import annotations

from app.audit import AuditSink, InMemoryAuditSink
from app.governance import GovernanceEngine
from app.tenants import build_default_registry


def build_engine() -> GovernanceEngine:
    return GovernanceEngine(build_default_registry(), InMemoryAuditSink())


def get_audit_sink(engine: GovernanceEngine) -> AuditSink:
    return engine.audit_sink  # type: ignore[attr-defined]
