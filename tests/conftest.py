"""测试夹具：可控时钟的治理引擎 + 打印请求输入与判定依据的测试日志。"""

from __future__ import annotations

import logging
from dataclasses import asdict

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.audit import InMemoryAuditSink
from app.governance import GovernanceEngine
from app.routes import register
from app.tenants import build_default_registry


class FakeClock:
    """以 POSIX 秒计的可控时钟，默认从 1_000_000 起，避免 epoch 边界干扰。"""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def audit_sink() -> InMemoryAuditSink:
    return InMemoryAuditSink()


@pytest.fixture
def engine(clock: FakeClock, audit_sink: InMemoryAuditSink) -> GovernanceEngine:
    return GovernanceEngine(
        build_default_registry(), audit_sink, time_provider=clock
    )


@pytest.fixture
def app(engine: GovernanceEngine) -> FastAPI:
    application = FastAPI()
    register(application, engine)

    @application.get("/")
    def read_root():
        return {"Hello": "World"}

    return application


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def _enable_request_logging(caplog: pytest.LogCaptureFixture):
    """打开治理日志：请求输入、判定结论与原因都会出现在测试输出中。"""
    caplog.set_level(logging.INFO, logger="governance.request")
    caplog.set_level(logging.INFO, logger="governance.audit")
    yield


@pytest.fixture
def show_audit(audit_sink: InMemoryAuditSink):
    """返回一个在断言点打印完整审计轨迹的辅助函数。"""
    def _show(label: str) -> None:
        print(f"\n[审计轨迹] {label}")
        for entry in audit_sink.entries():
            print("  ", asdict(entry))
    return _show


def pytest_configure(config: pytest.Config) -> None:
    """让治理日志（请求输入、判定结论与原因）进入 pytest 捕获并在失败时显示。"""
    for name in ("governance.request", "governance.audit"):
        logging.getLogger(name).setLevel(logging.INFO)
