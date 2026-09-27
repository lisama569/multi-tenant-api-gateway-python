"""认证与跨租户越权场景：四类拒绝必须可区分，数据严格隔离。"""
from __future__ import annotations

from fastapi.testclient import TestClient

from .conftest import log_request, log_response, print_audit


def test_root_remains_public_and_compatible(client: TestClient) -> None:
    log_request("root", "GET", "/")
    resp = client.get("/")
    log_response(resp)
    assert resp.status_code == 200
    assert resp.json() == {"Hello": "World"}  # 既有行为兼容


def test_missing_credential_is_distinguishable(client, governance) -> None:
    log_request("missing credential", "GET", "/tenants/tenant-a/items")
    resp = client.get("/tenants/tenant-a/items")
    log_response(resp)
    assert resp.status_code == 401
    err = resp.json()["error"]
    assert err["code"] == "missing_credential"
    print_audit(governance, "tenant-a")


def test_invalid_credential_is_distinguishable(client, governance) -> None:
    headers = {"X-API-Key": "ak-does-not-exist"}
    log_request("invalid credential", "GET",
                "/tenants/tenant-a/items", headers)
    resp = client.get("/tenants/tenant-a/items", headers=headers)
    log_response(resp)
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_credential"
    print_audit(governance, "tenant-a")


def test_cross_tenant_read_is_denied(client, governance) -> None:
    # tenant-b 的凭据试图读取 tenant-a 的资源
    headers = {"X-API-Key": "ak-tenant-b"}
    log_request("cross-tenant read", "GET",
                "/tenants/tenant-a/items", headers)
    resp = client.get("/tenants/tenant-a/items", headers=headers)
    log_response(resp)
    assert resp.status_code == 403
    err = resp.json()["error"]
    assert err["code"] == "cross_tenant_access"
    assert err["details"]["credential_tenant_id"] == "tenant-b"
    assert err["details"]["target_tenant_id"] == "tenant-a"
    print_audit(governance, "tenant-b")


def test_cross_tenant_write_is_denied_and_has_no_side_effect(
    client, governance
) -> None:
    headers = {"X-API-Key": "ak-tenant-b", "Idempotency-Key": "attk-1"}
    body = {"name": "steal", "payload": {}}
    log_request("cross-tenant write", "POST",
                "/tenants/tenant-a/items", headers, body)
    resp = client.post("/tenants/tenant-a/items", headers=headers, json=body)
    log_response(resp)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "cross_tenant_access"
    # tenant-a 自己看数据，确认没有被写入任何内容
    own = client.get("/tenants/tenant-a/items",
                     headers={"X-API-Key": "ak-tenant-a"})
    log_response(own)
    assert own.json()["items"] == []
    print_audit(governance, "tenant-b")


def test_insufficient_scope_is_distinguishable(client, governance) -> None:
    # tenant-b 只有 items:read，执行写操作应被精确归类为权限不足
    headers = {"X-API-Key": "ak-tenant-b", "Idempotency-Key": "b-w-1"}
    body = {"name": "x"}
    log_request("insufficient scope", "POST",
                "/tenants/tenant-b/items", headers, body)
    resp = client.post("/tenants/tenant-b/items",
                       headers=headers, json=body)
    log_response(resp)
    assert resp.status_code == 403
    err = resp.json()["error"]
    assert err["code"] == "insufficient_scope"
    assert err["details"]["required_scope"] == "items:write"
    print_audit(governance, "tenant-b")


def test_tenant_data_isolation(client, governance) -> None:
    # tenant-a 写入，tenant-b 无法通过任何路径看到
    client.post("/tenants/tenant-a/items",
                headers={"X-API-Key": "ak-tenant-a",
                         "Idempotency-Key": "a-1"},
                json={"name": "secret"})
    resp_b = client.get("/tenants/tenant-b/items",
                        headers={"X-API-Key": "ak-tenant-b"})
    resp_a = client.get("/tenants/tenant-a/items",
                        headers={"X-API-Key": "ak-tenant-a"})
    log_request("isolation check", "GET", "/tenants/tenant-b/items",
                {"X-API-Key": "ak-tenant-b"})
    log_response(resp_b)
    log_response(resp_a)
    assert resp_b.json()["items"] == []
    assert resp_a.json()["items"][0]["name"] == "secret"
