"""幂等重放与标识冲突：重复提交不产生重复副作用，冲突必须被明确拒绝。"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routes import register

KEY_A = {"X-API-Key": "key-a-123"}
KEY_B = {"X-API-Key": "key-b-456"}
PAYLOAD = {"name": "order-1", "data": "payload-v1"}


def _create(client, key="key-a-123", idem="idem-1", payload=None):
    headers = {"X-API-Key": key}
    if idem is not None:
        headers["Idempotency-Key"] = idem
    return client.post("/api/items", headers=headers, json=payload or PAYLOAD)


def test_replay_returns_same_result_without_new_side_effect(client, show_audit):
    r1 = _create(client)
    r2 = _create(client)
    r3 = _create(client)
    print("输入: 相同键+相同内容提交 3 次 ->",
          [r.status_code for r in (r1, r2, r3)])
    assert r1.status_code == 201
    assert r2.status_code == 200 and r2.headers["x-idempotent-replay"] == "true"
    assert r3.status_code == 200
    first_id = r1.json()["item"]["id"]
    assert r2.json()["item"]["id"] == first_id
    assert r3.json()["item"]["id"] == first_id

    listing = client.get("/api/items", headers=KEY_A).json()["items"]
    # 只有一个真实资源：无重复副作用
    assert len(listing) == 1
    print("判定依据: 真实副作用数量 =", len(listing), "（重放不写库）")
    show_audit("幂等重放")


def test_same_key_different_payload_is_conflict(client, show_audit):
    r1 = _create(client, idem="idem-2", payload={"name": "first"})
    assert r1.status_code == 201
    r2 = _create(client, idem="idem-2", payload={"name": "DIFFERENT-PAYLOAD"})
    print("输入: 复用幂等键但内容不同 ->", r2.status_code, r2.json())
    assert r2.status_code == 409
    body = r2.json()["error"]
    assert body["code"] == "idempotency_conflict"
    assert body["details"]["idempotency_key"] == "idem-2"

    listing = client.get("/api/items", headers=KEY_A).json()["items"]
    assert len(listing) == 1  # 冲突请求未产生副作用
    show_audit("标识冲突")


def test_requests_without_idempotency_key_are_not_deduped(client):
    r1 = _create(client, idem=None, payload={"name": "n1"})
    r2 = _create(client, idem=None, payload={"name": "n1"})
    print("输入: 无幂等键两次相同提交 ->",
          r1.status_code, r2.status_code)
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["item"]["id"] != r2.json()["item"]["id"]


def test_idempotency_key_scoped_per_tenant(client):
    r1 = _create(client, key="key-a-123", idem="shared-key",
                 payload={"name": "a-thing"})
    r2 = _create(client, key="key-b-456", idem="shared-key",
                 payload={"name": "a-thing"})
    print("输入: A/B 两租户使用相同幂等键 ->",
          r1.status_code, r2.status_code)
    assert r1.status_code == 201
    assert r2.status_code == 201  # 不互相视为重放/冲突
    assert r1.json()["item"]["id"] != r2.json()["item"]["id"]

    # 各自重放仍命中自己的记录
    r3 = _create(client, key="key-a-123", idem="shared-key",
                 payload={"name": "a-thing"})
    assert r3.status_code == 200
    assert r3.json()["item"]["id"] == r1.json()["item"]["id"]


def test_concurrent_duplicate_submissions_create_only_one(client, clock,
                                                          monkeypatch):
    """并发重放：同一键同时提交，只有一个产生副作用，其余收到明确结论。"""
    results: list[int] = []
    bodies: list[dict] = []
    lock = threading.Lock()

    def submit(_):
        r = _create(client)
        with lock:
            results.append(r.status_code)
            bodies.append(r.json())

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(submit, range(8)))

    print("输入: 8 个并发相同键提交 -> 状态:", sorted(results))
    created = [b for b in bodies if b.get("item", {}).get("id")]
    unique_ids = {b["item"]["id"] for b in created}
    # 只有一个真实副作用
    assert results.count(201) == 1
    assert len(unique_ids) == 1
    # 其余请求必须属于可解释的三类结论之一：
    #   200 重放完成 / 409 首请求处理中 / 429 先触发突发限流（同样无副作用）
    accounted = (results.count(200) + results.count(409)
                 + results.count(429))
    assert accounted == 7, results

    # 并发突发已耗尽窗口/突发额度，推进时钟过窗口后再核对副作用数量
    clock.advance(61)
    listing = client.get("/api/items", headers=KEY_A).json()["items"]
    assert len(listing) == 1


def test_replay_in_progress_is_distinct_from_conflict(engine):
    """引擎层：进行中重放与内容冲突给出不同 code。"""
    from app.errors import GovernanceError

    tenant = engine.authenticate("key-a-123")
    assert engine.begin_idempotent(tenant, "k", "h") is None
    try:
        engine.begin_idempotent(tenant, "k", "h")
    except GovernanceError as e:
        assert e.code == "replay_in_progress"
        print("进行中重放 ->", e.code, e.http_status)
    try:
        engine.begin_idempotent(tenant, "k", "other-hash")
    except GovernanceError as e:
        assert e.code == "idempotency_conflict"
        print("内容冲突 ->", e.code, e.http_status)
