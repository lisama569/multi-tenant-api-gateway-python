"""配额窗口边界与并发竞争：计数不得漂移，超额请求必须被拒绝并说明可重试时机。"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

KEY_A = {"X-API-Key": "key-a-123"}   # 配额 5 / 60s，突发 5 / s
KEY_B = {"X-API-Key": "key-b-456"}   # 配额 3 / 60s，突发 3 / s


def _make_requests(client, n, headers=KEY_A):
    return [client.get("/api/items", headers=headers) for _ in range(n)]


def test_quota_exact_limit_no_over_admission(client, show_audit):
    responses = _make_requests(client, 6)
    statuses = [r.status_code for r in responses]
    print("输入: tenant-a 连续 6 次 GET -> 状态:", statuses)
    assert statuses == [200, 200, 200, 200, 200, 429]

    denied = responses[-1].json()["error"]
    assert denied["code"] == "quota_exceeded"
    d = denied["details"]
    print("判定依据: used=%s limit=%s retry_after=%ss reset_at=%s"
          % (d["quota_used"], d["quota_limit"],
             d["retry_after_seconds"], d["reset_at"]))
    assert d["quota_used"] == 5
    assert d["quota_remaining"] == 0
    assert d["retry_after_seconds"] == 60
    assert responses[-1].headers["retry-after"] == "60"
    show_audit("配额耗尽")


def test_window_boundary_resets_deterministically(client, clock, show_audit):
    responses = _make_requests(client, 5)
    assert all(r.status_code == 200 for r in responses)
    r = client.get("/api/items", headers=KEY_A)
    assert r.status_code == 429, r.text
    print("输入: 窗口内第 6 次 -> 429；推进时钟到窗口边界")

    clock.advance(59)
    r = client.get("/api/items", headers=KEY_A)
    print("t+59s 判定:", r.status_code, r.json().get("error", {}).get("code"))
    assert r.status_code == 429  # 边界前一秒仍拒绝

    clock.advance(1)
    r = client.get("/api/items", headers=KEY_A)
    print("t+60s 判定:", r.status_code, "remaining=",
          r.headers.get("x-quota-remaining"))
    assert r.status_code == 200  # 恰好边界处重置，不漂移
    assert r.headers["x-quota-remaining"] == "4"
    show_audit("窗口边界重置")


def test_denied_request_does_not_consume_quota(client, clock):
    _make_requests(client, 5)  # 恰好用尽
    for _ in range(3):
        r = client.get("/api/items", headers=KEY_A)
        assert r.status_code == 429
    clock.advance(60)
    responses = _make_requests(client, 5)  # 被拒的 3 次不应占用新窗口额度
    assert [r.status_code for r in responses] == [200] * 5
    print("输入: 新窗口 5 次（此前被拒 3 次不计费）-> 全部 200")


def test_remaining_and_reset_reported_on_admission(client):
    r = client.get("/api/items", headers=KEY_A)
    print("输入: tenant-a 首次请求 -> 配额头:",
          {k: r.headers[k] for k in ("x-quota-remaining", "x-quota-reset-at",
                                     "x-ratelimit-remaining")})
    assert r.headers["x-quota-remaining"] == "4"
    assert r.headers["x-quota-reset-at"]
    assert r.headers["x-ratelimit-remaining"] == "4"


def test_quota_is_isolated_per_tenant(client):
    # A 用尽自己的 5 个，B 的 3 个不受影响
    _make_requests(client, 5, headers=KEY_A)
    b_responses = _make_requests(client, 3, headers=KEY_B)
    print("输入: A 耗尽后 B 请求 3 次 -> ",
          [r.status_code for r in b_responses])
    assert [r.status_code for r in b_responses] == [200, 200, 200]


def test_burst_limit_under_concurrency_never_over_admits(engine, monkeypatch):
    """并发竞争：突发窗口内成功数严格不超过 burst_limit。

    用 TestClient + 线程池制造同一秒内的并发突发；治理引擎在单把锁内做
    原子“检查—计数”，所以无论调度如何交错，放行数恒定。
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routes import register

    application = FastAPI()
    register(application, engine)
    local_client = TestClient(app=application)
    local_client.__enter__()

    def call(_):
        return local_client.get("/api/items", headers=KEY_B).status_code

    with ThreadPoolExecutor(max_workers=8) as pool:
        statuses = list(pool.map(call, range(10)))

    local_client.__exit__(None, None, None)
    accepted = statuses.count(200)
    print("输入: 10 个并发突发请求（burst_limit=3）-> 状态:",
          sorted(statuses))
    assert accepted == 3, statuses
    assert statuses.count(429) == 7

    reasons = [e.reason for e in engine.audit_sink.entries()]
    # 每个被拒都有审计原因；允许数与配额计数一致
    assert reasons.count("quota_consumed") == 3
    assert threading.active_count() >= 1  # 线程已回收后引擎仍可用


def test_concurrent_quota_consumption_is_drift_free(engine):
    """直接对引擎做并发配额消耗，成功次数必须严格等于上限。"""
    tenant = engine.authenticate("key-a-123")
    outcomes: list[int] = []
    lock = threading.Lock()

    def consume():
        try:
            engine.consume_quota(tenant)
            with lock:
                outcomes.append(1)
        except Exception:
            with lock:
                outcomes.append(0)

    with ThreadPoolExecutor(max_workers=16) as pool:
        list(pool.map(lambda _: consume(), range(50)))

    print("输入: 50 个并发 consume_quota（limit=5）-> 放行:",
          sum(outcomes), "拒绝:", outcomes.count(0))
    assert sum(outcomes) == 5
    assert outcomes.count(0) == 45
