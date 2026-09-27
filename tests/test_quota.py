"""配额窗口边界与并发竞争：不得计数漂移、不得超额放行。"""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from .conftest import log_request, log_response, print_audit

HEADERS_A = {"X-API-Key": "ak-tenant-a"}  # 窗口上限 3


def test_exact_limit_then_reject(client: TestClient, governance) -> None:
    statuses = []
    for i in range(3):
        log_request(f"within-limit-{i}", "GET",
                    "/tenants/tenant-a/items", HEADERS_A)
        resp = client.get("/tenants/tenant-a/items", headers=HEADERS_A)
        log_response(resp)
        assert resp.status_code == 200
        statuses.append(resp.status_code)

    log_request("over-limit", "GET", "/tenants/tenant-a/items", HEADERS_A)
    denied = client.get("/tenants/tenant-a/items", headers=HEADERS_A)
    log_response(denied)
    assert denied.status_code == 429
    err = denied.json()["error"]
    assert err["code"] == "quota_exceeded"
    assert err["details"]["remaining"] == 0
    assert err["details"]["used"] == 3
    assert err["details"]["limit"] == 3
    assert err["details"]["retry_after_seconds"] == 60.0
    print_audit(governance, "tenant-a")


def test_window_boundary_resets_quota(client, governance, fake_clock) -> None:
    # 打满 3 个
    for _ in range(3):
        client.get("/tenants/tenant-a/items", headers=HEADERS_A)
    denied = client.get("/tenants/tenant-a/items", headers=HEADERS_A)
    assert denied.status_code == 429

    # 差 1 秒到边界：仍拒绝
    fake_clock.advance(59)
    log_request("just-before-boundary", "GET",
                "/tenants/tenant-a/items", HEADERS_A)
    still_denied = client.get("/tenants/tenant-a/items", headers=HEADERS_A)
    log_response(still_denied)
    assert still_denied.status_code == 429

    # 恰好到窗口边界：计数重置，请求被放行
    fake_clock.advance(1)
    log_request("at-boundary", "GET",
                "/tenants/tenant-a/items", HEADERS_A)
    allowed = client.get("/tenants/tenant-a/items", headers=HEADERS_A)
    log_response(allowed)
    assert allowed.status_code == 200

    # 新窗口又只剩 2 个额度
    client.get("/tenants/tenant-a/items", headers=HEADERS_A)
    third = client.get("/tenants/tenant-a/items", headers=HEADERS_A)
    fourth = client.get("/tenants/tenant-a/items", headers=HEADERS_A)
    assert third.status_code == 200
    assert fourth.status_code == 429
    print_audit(governance, "tenant-a")


def test_concurrent_burst_never_over_admits(client, governance) -> None:
    # 20 个并发请求抢 tenant-b 的 2 个名额（窗口 60s）
    headers = {"X-API-Key": "ak-tenant-b"}
    results: list[int] = []
    lock = threading.Lock()

    def fire() -> None:
        resp = client.get("/tenants/tenant-b/items", headers=headers)
        with lock:
            results.append(resp.status_code)

    log_request("burst-20", "GET x20", "/tenants/tenant-b/items", headers)
    with ThreadPoolExecutor(max_workers=10) as pool:
        list(pool.map(lambda _: fire(), range(20)))

    allowed = sum(1 for s in results if s == 200)
    rejected = sum(1 for s in results if s == 429)
    print(f"<- allowed={allowed} rejected={rejected} "
          f"status_mix={sorted(results)}")
    assert allowed == 2
    assert rejected == 18
    print_audit(governance, "tenant-b")


def test_tenants_have_independent_windows(client, governance) -> None:
    # tenant-a 打满不影响 tenant-c
    for _ in range(3):
        client.get("/tenants/tenant-a/items", headers=HEADERS_A)
    assert client.get("/tenants/tenant-a/items",
                      headers=HEADERS_A).status_code == 429
    resp_c = client.get("/tenants/tenant-c/items",
                        headers={"X-API-Key": "ak-tenant-c"})
    log_request("independent window", "GET",
                "/tenants/tenant-c/items", {"X-API-Key": "ak-tenant-c"})
    log_response(resp_c)
    assert resp_c.status_code == 200


def test_quota_endpoint_explains_remaining(client, governance,
                                           fake_clock) -> None:
    # tenant-c 持有 quota:read，可查看自身配额快照（查看本身不消耗配额）
    headers_c = {"X-API-Key": "ak-tenant-c"}
    resp = client.get("/tenants/tenant-c/quota", headers=headers_c)
    log_request("quota view", "GET", "/tenants/tenant-c/quota", headers_c)
    log_response(resp)
    assert resp.status_code == 200
    body = resp.json()
    assert body["used"] == 0
    assert body["remaining"] == 100
    assert body["window_resets_at"] == fake_clock.now + 60
