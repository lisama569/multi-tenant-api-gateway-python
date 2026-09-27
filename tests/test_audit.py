"""审计追溯：记录完整反映租户、判定结论与原因，且按租户隔离。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import log_request, log_response, print_audit


def test_audit_records_allow_and_replay_decisions(
    client: TestClient, governance
) -> None:
    client.post("/tenants/tenant-c/items",
                headers={"X-API-Key": "ak-tenant-c",
                         "Idempotency-Key": "aud-1"},
                json={"name": "one"})
    client.post("/tenants/tenant-c/items",
                headers={"X-API-Key": "ak-tenant-c",
                         "Idempotency-Key": "aud-1"},
                json={"name": "one"})
    log_request("audit trail", "GET",
                "/tenants/tenant-c/audit", {"X-API-Key": "ak-tenant-c"})
    resp = client.get("/tenants/tenant-c/audit",
                      headers={"X-API-Key": "ak-tenant-c"})
    log_response(resp)
    assert resp.status_code == 200
    records = resp.json()["records"]
    reasons = [(r["decision"], r["reason"]) for r in records]
    assert ("allow", "quota_consumed") in reasons
    assert ("replay", "idempotent_replay") in reasons
    first = records[0]
    assert first["tenant_id"] == "tenant-c"
    assert first["method"] == "POST"
    assert first["target_tenant_id"] == "tenant-c"
    assert first["idempotency_key"] == "aud-1"
    print_audit(governance, "tenant-c")


def test_audit_records_denial_reasons_with_distinguishable_codes(
    client: TestClient, governance
) -> None:
    client.get("/tenants/tenant-c/items")  # 缺凭据
    client.get("/tenants/tenant-c/items",
               headers={"X-API-Key": "ak-bogus"})  # 无效凭据
    client.get("/tenants/tenant-c/items",
               headers={"X-API-Key": "ak-tenant-b"})  # 跨租户
    client.post("/tenants/tenant-c/items",
                headers={"X-API-Key": "ak-tenant-b"},
                json={"name": "x"})  # 注意：路径是 c，凭据是 b => 跨租户
    resp = client.get("/tenants/tenant-c/audit",
                      headers={"X-API-Key": "ak-tenant-c"})
    records = resp.json()["records"]
    deny_reasons = [r["reason"] for r in records
                    if r["decision"] == "deny"]
    print("<- deny reasons seen:", deny_reasons)
    assert "missing_credential" in deny_reasons
    assert "invalid_credential" in deny_reasons
    assert "cross_tenant_access" in deny_reasons
    print_audit(governance, "tenant-c")


def test_audit_trail_is_tenant_scoped(client: TestClient, governance) -> None:
    # tenant-a 的动作
    client.post("/tenants/tenant-a/items",
                headers={"X-API-Key": "ak-tenant-a",
                         "Idempotency-Key": "a-secret"},
                json={"name": "a-data"})
    # tenant-b 越权探测 tenant-a，被拒
    client.get("/tenants/tenant-a/items",
               headers={"X-API-Key": "ak-tenant-b"})

    trail_a = client.get("/tenants/tenant-c/audit",
                         headers={"X-API-Key": "ak-tenant-c"})
    # 用 tenant-c 视角看不到 a/b 的记录
    c_records = trail_a.json()["records"]
    assert all(r["tenant_id"] != "tenant-a" for r in c_records)
    print(f"<- tenant-c visible records: {len(c_records)}")
    print_audit(governance, "tenant-a")


def test_audit_endpoint_requires_audit_scope(client: TestClient) -> None:
    # tenant-a 没有 audit:read
    log_request("audit without scope", "GET",
                "/tenants/tenant-a/audit", {"X-API-Key": "ak-tenant-a"})
    resp = client.get("/tenants/tenant-a/audit",
                      headers={"X-API-Key": "ak-tenant-a"})
    log_response(resp)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "insufficient_scope"
