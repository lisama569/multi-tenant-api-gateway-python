"""幂等重放与标识冲突：重复提交无副作用，键复用被明确拒绝。"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from .conftest import log_request, log_response, print_audit

HEADERS = {"X-API-Key": "ak-tenant-c"}  # 大配额，避免配额干扰


def _post(client: TestClient, key: str, name: str):
    headers = {**HEADERS, "Idempotency-Key": key}
    body = {"name": name, "payload": {"v": name}}
    log_request("create item", "POST",
                "/tenants/tenant-c/items", headers, body)
    resp = client.post("/tenants/tenant-c/items", headers=headers, json=body)
    log_response(resp)
    return resp


def test_repeated_submission_has_single_side_effect(
    client: TestClient, governance
) -> None:
    r1 = _post(client, "order-1", "alpha")
    r2 = _post(client, "order-1", "alpha")
    r3 = _post(client, "order-1", "alpha")
    assert r1.status_code == 200
    assert r2.status_code == 200 and r2.json().get("replayed") is True
    assert r3.status_code == 200 and r3.json().get("replayed") is True

    first_id = r1.json()["item"]["id"]
    assert r2.json()["item"]["id"] == first_id
    assert r3.json()["item"]["id"] == first_id

    listing = client.get("/tenants/tenant-c/items", headers=HEADERS)
    log_request("verify side effects", "GET",
                "/tenants/tenant-c/items", HEADERS)
    log_response(listing)
    assert len(listing.json()["items"]) == 1  # 只有一次真实写入
    print_audit(governance, "tenant-c")


def test_same_key_different_payload_is_conflict(
    client: TestClient, governance
) -> None:
    r1 = _post(client, "order-2", "first")
    assert r1.status_code == 200

    conflict = _post(client, "order-2", "different")
    assert conflict.status_code == 409
    err = conflict.json()["error"]
    assert err["code"] == "idempotency_conflict"
    assert err["details"]["idempotency_key"] == "order-2"
    assert (err["details"]["stored_fingerprint"]
            != err["details"]["request_fingerprint"])

    # 冲突后原结果仍可被正常重放
    replay = _post(client, "order-2", "first")
    assert replay.status_code == 200
    assert replay.json()["item"]["id"] == r1.json()["item"]["id"]
    print_audit(governance, "tenant-c")


def test_idempotency_key_is_scoped_per_tenant(
    client: TestClient, governance
) -> None:
    # 同样的键在不同租户下互不影响
    headers_a = {"X-API-Key": "ak-tenant-a", "Idempotency-Key": "shared-k"}
    headers_c = {**HEADERS, "Idempotency-Key": "shared-k"}
    ra = client.post("/tenants/tenant-a/items", headers=headers_a,
                     json={"name": "a"})
    rc = client.post("/tenants/tenant-c/items", headers=headers_c,
                     json={"name": "c"})
    log_request("per-tenant key A", "POST",
                "/tenants/tenant-a/items", headers_a, {"name": "a"})
    log_response(ra)
    log_response(rc)
    assert ra.status_code == 200 and rc.status_code == 200
    assert ra.json()["item"]["tenant_id"] == "tenant-a"
    assert rc.json()["item"]["tenant_id"] == "tenant-c"


def test_concurrent_same_key_collapses_to_one_effect(
    client: TestClient, governance
) -> None:
    # 直接压测治理层（TestClient 跨线程会使用独立 portal，
    # 不适合作为并发一致性的载体）。
    body = {"name": "race", "payload": {}}
    barrier = threading.Barrier(8)
    effects: list[int] = []
    lock = threading.Lock()

    def fire(i: int) -> str:
        barrier.wait()
        with governance.guard_write(
            "ak-tenant-c", "POST", "/tenants/tenant-c/items",
            "tenant-c", "items:write", "race-key", body,
        ) as decision:
            if not decision["replay"]:
                with lock:
                    effects.append(i)
                decision["response"] = {"item": {"id": 1000 + i}}
            return "first" if not decision["replay"] else "replay"

    log_request("concurrent same key x8", "POST",
                "/tenants/tenant-c/items",
                {"Idempotency-Key": "race-key"}, body)
    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(fire, range(8)))
    print("<- outcomes:", outcomes)
    assert outcomes.count("first") == 1
    assert outcomes.count("replay") == 7
    assert effects == [outcomes.index("first")]  # 只有一次副作用
    print_audit(governance, "tenant-c")


def test_failed_first_request_releases_key(client: TestClient,
                                           governance, monkeypatch) -> None:
    # 模拟首个请求业务失败：键应释放，同内容可再次提交成功
    from app.api import routes

    def failing_next_id() -> int:
        raise RuntimeError("storage down")

    monkeypatch.setattr(routes, "_new_item_id", failing_next_id)
    headers = {**HEADERS, "Idempotency-Key": "fail-key"}
    body = {"name": "will-fail"}
    log_request("failing first", "POST",
                "/tenants/tenant-c/items", headers, body)
    resp = client.post("/tenants/tenant-c/items",
                       headers=headers, json=body)
    log_response(resp)
    assert resp.status_code == 500
    monkeypatch.undo()

    retry = client.post("/tenants/tenant-c/items",
                        headers=headers, json=body)
    log_response(retry)
    assert retry.status_code == 200
    assert retry.json().get("replayed") is None or \
        retry.json().get("replayed") is False
    print_audit(governance, "tenant-c")
