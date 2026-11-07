from __future__ import annotations

import os
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from opsagents.backend import FaultPlan, OpsBackend
from opsagents.brains.rule import RuleBrain
from opsagents.executor import RunContext, ToolExecutor
from opsagents.graph import OpsEngine, make_engine
from opsagents.ledger import InMemoryLedger
from opsagents.models import Ticket
from opsagents.policy import Policy, load_policy


@pytest.fixture(scope="session")
def policy() -> Policy:
    return load_policy()


@pytest.fixture
def backend() -> OpsBackend:
    return OpsBackend.from_file()


@pytest.fixture
def ledger() -> InMemoryLedger:
    return InMemoryLedger()


@pytest.fixture
def executor(backend: OpsBackend, policy: Policy, ledger: InMemoryLedger) -> ToolExecutor:
    return ToolExecutor(backend, policy, ledger, sleep=lambda _s: None)


@pytest.fixture
def ticket() -> Callable[..., Ticket]:
    def _make(
        customer_id: str = "C-1001",
        body: str = "Where is AF-24001?",
        ticket_id: str = "T-5001",
        subject: str = "Question",
    ) -> Ticket:
        return Ticket(ticket_id=ticket_id, customer_id=customer_id, subject=subject, body=body)

    return _make


@pytest.fixture
def ctx(ticket: Callable[..., Ticket]) -> Callable[..., RunContext]:
    def _make(customer_id: str = "C-1001", ticket_id: str = "T-5001", run_id: str = "run-1") -> RunContext:
        return RunContext(run_id, ticket(customer_id=customer_id, ticket_id=ticket_id))

    return _make


@pytest.fixture
def engine_for(policy: Policy) -> Callable[..., OpsEngine]:
    def _make(brain: Any = None, faults: FaultPlan | None = None) -> OpsEngine:
        backend = OpsBackend.from_file(faults=faults)
        ex = ToolExecutor(backend, policy, InMemoryLedger(), sleep=lambda _s: None)
        return make_engine(backend, ex, brain or RuleBrain(policy))

    return _make


@pytest.fixture
def postgres_url() -> Iterator[str]:
    url = os.environ.get("OPS_TEST_DATABASE_URL")
    if not url:
        pytest.skip("set OPS_TEST_DATABASE_URL to run PostgreSQL tests")
    import psycopg

    with psycopg.connect(url, autocommit=True) as conn:
        for table in (
            "ops_ledger",
            "ops_decisions",
            "ops_approvals",
            "checkpoints",
            "checkpoint_blobs",
            "checkpoint_writes",
            "checkpoint_migrations",
        ):
            conn.execute(f"DROP TABLE IF EXISTS {table} CASCADE")  # type: ignore[arg-type]
    yield url
