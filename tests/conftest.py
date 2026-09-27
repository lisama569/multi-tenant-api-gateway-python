"""测试夹具：确定性时钟、种子租户、FastAPI TestClient。

每个测试都通过 log_request 打印请求输入，通过 print_audit 打印
治理判定依据（结论 + 原因 + 关键细节）。
"""
from __future__ import annotations

import json
from itertools import count
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.api.deps import get_governance
from app.governance import Governance
from app.main import app
from app.tenants import Credential, Tenant, TenantRegistry


class FakeClock:
    """可手动推进的确定性时钟。"""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def build_test_registry() -> TenantRegistry:
    tenants = [
        Tenant("tenant-a", "Alpha",
               frozenset({"items:read", "items:write"}),
               quota_limit=3, quota_window_seconds=60),
        Tenant("tenant-b", "Beta",
               frozenset({"items:read"}),
               quota_limit=2, quota_window_seconds=60),
        Tenant("tenant-c", "Gamma",
               frozenset({"items:read", "items:write",
                          "quota:read", "audit:read"}),
               quota_limit=100, quota_window_seconds=60),
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


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def governance(fake_clock: FakeClock) -> Governance:
    gov = Governance(build_test_registry(), clock=fake_clock,
                     audit_sink=lambda r: None)
    app.dependency_overrides[get_governance] = lambda: gov
    yield gov
    app.dependency_overrides.clear()
    gov.reset()


@pytest.fixture
def client(governance: Governance) -> TestClient:
    # 清理进程内演示存储，避免用例之间数据污染。
    from app.api import routes
    with routes._store_lock:
        routes._items.clear()
        routes._id_seq = count(1)
    return TestClient(app, raise_server_exceptions=False)


def log_request(title: str, method: str, path: str,
                headers: dict | None = None, json_body: Any = None) -> None:
    """打印请求输入。"""
    safe_headers = {k: v for k, v in (headers or {}).items()}
    print(f"\n-> [{title}] {method} {path}")
    print(f"   headers={json.dumps(safe_headers, ensure_ascii=False)}")
    if json_body is not None:
        print(f"   body={json.dumps(json_body, ensure_ascii=False, sort_keys=True)}")


def log_response(resp: Any) -> None:
    try:
        body = resp.json()
    except Exception:
        body = resp.text
    print(f"<- status={resp.status_code} "
          f"body={json.dumps(body, ensure_ascii=False, default=str)}")


def print_audit(governance: Governance, tenant_id: str) -> None:
    """打印某租户的治理判定依据。"""
    print(f"   audit[{tenant_id}]:")
    for r in governance.audit.list(tenant_id):
        detail = json.dumps(r.detail, ensure_ascii=False, default=str,
                            sort_keys=True)
        print(f"     #{r.seq} {r.method} {r.path} "
              f"decision={r.decision} reason={r.reason} detail={detail}")
