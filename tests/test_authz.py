"""跨租户越权、凭据缺失/无效、权限不足：错误必须被正确归类且原因可区分。"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# 既有行为兼容
# ---------------------------------------------------------------------------

def test_root_keeps_legacy_behavior(client):
    r = client.get("/")
    print("输入: GET / （无凭据）")
    assert r.status_code == 200
    assert r.json() == {"Hello": "World"}


# ---------------------------------------------------------------------------
# 凭据识别
# ---------------------------------------------------------------------------

def test_missing_credentials_is_distinct(client, show_audit):
    r = client.get("/api/items")
    print("输入: GET /api/items, 无 X-API-Key -> 判定:", r.json())
    assert r.status_code == 401
    body = r.json()["error"]
    assert body["code"] == "missing_credentials"
    show_audit("缺少凭据")


def test_invalid_credentials_is_distinct(client, show_audit):
    r = client.get("/api/items", headers={"X-API-Key": "key-does-not-exist"})
    print("输入: GET /api/items, X-API-Key=key-does-not-exist -> 判定:", r.json())
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_credentials"
    show_audit("无效凭据")


def test_empty_api_key_treated_as_missing(client):
    r = client.get("/api/items", headers={"X-API-Key": ""})
    print("输入: GET /api/items, X-API-Key='' -> 判定:", r.json())
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "missing_credentials"


# ---------------------------------------------------------------------------
# 权限范围
# ---------------------------------------------------------------------------

def test_readonly_tenant_cannot_write(client, show_audit):
    payload = {"name": "attempt", "data": "x"}
    r = client.post(
        "/api/items", headers={"X-API-Key": "key-ro-789"}, json=payload
    )
    print(f"输入: POST /api/items key=key-ro-789 payload={payload} -> 判定:",
          r.status_code, r.json())
    assert r.status_code == 403
    body = r.json()["error"]
    assert body["code"] == "insufficient_scope"
    assert body["details"]["required_scope"] == "items:write"
    # 只读租户仍可正常读
    r2 = client.get("/api/items", headers={"X-API-Key": "key-ro-789"})
    assert r2.status_code == 200
    show_audit("权限不足")


# ---------------------------------------------------------------------------
# 跨租户边界
# ---------------------------------------------------------------------------

def _create_item(client, key: str, name: str, idem: str | None = None) -> str:
    headers = {"X-API-Key": key}
    if idem is not None:
        headers["Idempotency-Key"] = idem
    r = client.post("/api/items", headers=headers, json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["item"]["id"]


def test_cross_tenant_get_denied_and_classified(client, show_audit):
    item_id = _create_item(client, "key-a-123", "owned-by-a", idem="seed-a")
    r = client.get(f"/api/items/{item_id}", headers={"X-API-Key": "key-b-456"})
    print(f"输入: tenant-b 读取 tenant-a 的资源 {item_id} -> 判定:",
          r.status_code, r.json())
    assert r.status_code == 403
    body = r.json()["error"]
    assert body["code"] == "cross_tenant_access"
    assert body["details"]["resource_tenant_id"] == "tenant-a"
    assert body["details"]["expected_tenant_id"] == "tenant-b"
    show_audit("跨租户读越权")


def test_list_is_scoped_per_tenant(client):
    _create_item(client, "key-a-123", "a-secret", idem="seed-a2")
    r = client.get("/api/items", headers={"X-API-Key": "key-b-456"})
    print("输入: tenant-b 列表 -> 判定:", r.status_code, r.json())
    assert r.status_code == 200
    assert r.json()["items"] == []  # 看不到 A 的数据

    r2 = client.get("/api/items", headers={"X-API-Key": "key-a-123"})
    names = [i["name"] for i in r2.json()["items"]]
    assert "a-secret" in names
    assert all(i["tenant_id"] == "tenant-a" for i in r2.json()["items"])


def test_owner_can_read_own_resource(client):
    item_id = _create_item(client, "key-a-123", "mine", idem="seed-a3")
    r = client.get(f"/api/items/{item_id}", headers={"X-API-Key": "key-a-123"})
    print(f"输入: tenant-a 读取自己的资源 {item_id} -> 判定:", r.status_code)
    assert r.status_code == 200
    assert r.json()["item"]["id"] == item_id


def test_unknown_resource_is_not_found_not_cross_tenant(client):
    r = client.get("/api/items/nope-123", headers={"X-API-Key": "key-a-123"})
    print("输入: GET /api/items/nope-123 -> 判定:", r.status_code, r.json())
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


def test_all_denial_codes_are_distinguishable(client):
    results = [
        client.get("/api/items"),
        client.get("/api/items", headers={"X-API-Key": "bad"}),
        client.post("/api/items", headers={"X-API-Key": "key-ro-789"},
                    json={"name": "x"}),
    ]
    codes = [r.json()["error"]["code"] for r in results]
    print("输入: 三类拒绝请求 -> 错误码序列:", codes)
    assert codes == ["missing_credentials", "invalid_credentials",
                     "insufficient_scope"]
