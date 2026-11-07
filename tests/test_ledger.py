from collections.abc import Iterator

import pytest

from opsagents.ledger import InMemoryLedger, LedgerEntry, LedgerStore, SqlLedger


@pytest.fixture(params=["memory", "sqlite", "postgres"])
def store(request: pytest.FixtureRequest) -> Iterator[LedgerStore]:
    if request.param == "memory":
        s: LedgerStore = InMemoryLedger()
    elif request.param == "sqlite":
        s = SqlLedger.sqlite(":memory:")
    else:
        s = SqlLedger.postgres(request.getfixturevalue("postgres_url"))
    yield s
    s.close()


def _entry(call_id: str, status: str = "ok", run: str = "r1") -> LedgerEntry:
    return LedgerEntry(
        run,
        call_id,
        "billing",
        "issue_credit",
        "write",
        {"amount_eur": 1.5},
        status,
        result={"credit_id": "CN-1"} if status == "ok" else None,
        attempts=2,
    )


def test_entries_and_lookup(store: LedgerStore) -> None:
    store.append(_entry("a", "error"))
    store.append(_entry("a"))
    store.append(_entry("b", run="r2"))
    found = store.ok_result("a")
    assert found is not None and found.result == {"credit_id": "CN-1"} and found.attempts == 2
    assert store.ok_result("missing") is None
    assert [e.call_id for e in store.entries("r1")] == ["a", "a"]
    assert len(store.entries()) == 3
    seqs = [e.seq for e in store.entries()]
    assert seqs == sorted(seqs)


def test_decisions_and_approvals_are_write_once(store: LedgerStore) -> None:
    store.put_decision("r1", "billing:1:0", {"type": "tool_call", "tool": "x"})
    store.put_decision("r1", "billing:1:0", {"type": "tool_call", "tool": "y"})
    assert store.get_decision("r1", "billing:1:0") == {"type": "tool_call", "tool": "x"}
    assert store.get_decision("r2", "billing:1:0") is None
    store.put_approval("fp", {"approved": True, "approver": "u1"})
    store.put_approval("fp", {"approved": False, "approver": "u2"})
    assert store.get_approval("fp") == {"approved": True, "approver": "u1"}
    assert store.get_approval("other") is None
