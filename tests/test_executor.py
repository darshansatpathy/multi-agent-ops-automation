from collections.abc import Callable

import pytest

from opsagents.backend import FaultPlan, OpsBackend
from opsagents.executor import ApprovalRequired, RunContext, ToolExecutor
from opsagents.ledger import InMemoryLedger
from opsagents.models import ToolCall
from opsagents.policy import Policy

Ctx = Callable[..., RunContext]


def _credit(amount: float, invoice: str = "INV-8001", reason: str = "sla_breach") -> ToolCall:
    return ToolCall("issue_credit", {"invoice_id": invoice, "amount_eur": amount, "reason": reason})


def _message(body: str, customer: str = "C-1001", ticket: str = "T-5001") -> ToolCall:
    return ToolCall("send_customer_message", {"customer_id": customer, "ticket_id": ticket, "body": body})


def test_unknown_tool_and_scope(executor: ToolExecutor, ctx: Ctx) -> None:
    r = executor.run("triage", ToolCall("delete_invoice", {}), ctx())
    assert (r.ok, r.error_kind) == (False, "validation")
    r = executor.run("comms", _credit(10), ctx())
    assert r.error_kind == "permission"
    assert executor.ledger.entries()[-1].status == "rejected"


@pytest.mark.parametrize(
    "args",
    [
        {"shipment_id": "AF-24001", "extra": 1},  # unknown field
        {"shipment_id": "af-24001"},  # pattern
        {"shipment_id": 24001},  # strict: no coercion
        {},
    ],
)
def test_strict_argument_validation(executor: ToolExecutor, ctx: Ctx, args: dict[str, object]) -> None:
    r = executor.run("shipment", ToolCall("get_shipment", args), ctx())
    assert r.error_kind == "validation"


def test_string_amount_is_not_coerced(executor: ToolExecutor, ctx: Ctx) -> None:
    r = executor.run(
        "billing",
        ToolCall("issue_credit", {"invoice_id": "INV-8001", "amount_eur": "180", "reason": "sla_breach"}),
        ctx(),
    )
    assert r.error_kind == "validation"


def test_object_level_authorisation_looks_like_not_found(executor: ToolExecutor, ctx: Ctx) -> None:
    mine = executor.run("shipment", ToolCall("get_shipment", {"shipment_id": "AF-24001"}), ctx())
    theirs = executor.run("shipment", ToolCall("get_shipment", {"shipment_id": "AF-24002"}), ctx())
    missing = executor.run("shipment", ToolCall("get_shipment", {"shipment_id": "AF-99999"}), ctx())
    assert mine.ok
    assert theirs.error_kind == missing.error_kind == "not_found"
    assert executor.run("billing", _credit(10, "INV-8003"), ctx()).error_kind == "not_found"
    other = ToolCall("get_customer", {"customer_id": "C-1002"})
    assert executor.run("triage", other, ctx()).error_kind == "not_found"


def test_reads_are_cached_within_a_run(executor: ToolExecutor, ctx: Ctx) -> None:
    call = ToolCall("check_sla", {"shipment_id": "AF-24001"})
    first = executor.run("shipment", call, ctx())
    again = executor.run("billing", call, ctx())
    other_run = executor.run("shipment", call, ctx(run_id="run-2"))
    assert not first.cached and again.cached and not other_run.cached
    assert again.data == first.data


def test_writes_are_idempotent_per_ticket(executor: ToolExecutor, ctx: Ctx, backend: OpsBackend) -> None:
    first = executor.run("billing", _credit(180), ctx())
    assert first.ok
    replay = executor.run("billing", _credit(180), ctx(run_id="run-2"))  # same ticket, new run
    assert replay.cached and replay.data == first.data
    assert len(backend.credits) == 2  # the prior credit plus one
    # A different ticket asking for the same credit is stopped by policy, not by the cache.
    blocked = executor.run("billing", _credit(180), ctx(ticket_id="T-5002"))
    assert blocked.error_kind == "policy"
    assert "already has a sla_breach credit" in str(blocked.error)


def test_credit_policy(executor: ToolExecutor, ctx: Ctx) -> None:
    assert "agents" in str(executor.run("billing", _credit(2500, "INV-8010"), ctx("C-1003")).error)
    assert "at most 180.00" in str(executor.run("billing", _credit(200), ctx()).error)
    partial = executor.run("billing", _credit(10, "INV-8008-A", "duplicate_invoice"), ctx("C-1002"))
    assert partial.ok  # INV-8008-A has a twin, and 10 EUR is below the auto limit
    not_dup = executor.run("billing", _credit(10, "INV-8013", "duplicate_invoice"), ctx("C-1002"))
    assert "not a duplicate" in str(not_dup.error)
    too_much = executor.run("billing", _credit(200, "INV-8012-B", "duplicate_invoice"), ctx("C-1006"))
    assert "exceeds the duplicate" in str(too_much.error)


def test_sla_credit_requires_a_delay(policy: Policy, ctx: Ctx) -> None:
    backend = OpsBackend.from_file()
    backend.invoices["INV-8002"] = {"id": "INV-8002", "shipment_id": "AF-24002", "amount_eur": 1700, "status": "open"}
    ex = ToolExecutor(backend, policy, InMemoryLedger())
    r = ex.run("billing", _credit(50, "INV-8002"), ctx("C-1002"))
    assert "not late" in str(r.error)


def test_approval_flow(executor: ToolExecutor, ctx: Ctx) -> None:
    call = _credit(420, "INV-8003")
    with pytest.raises(ApprovalRequired) as pending:
        executor.run("billing", call, ctx("C-1003"))
    fp = pending.value.request["fingerprint"]
    with pytest.raises(ApprovalRequired):
        executor.run("billing", call, ctx("C-1003"))
    assert sum(e.status == "approval_required" for e in executor.ledger.entries()) == 1  # logged once
    executor.grant(fp, approved=True, approver="u-1")
    assert executor.run("billing", call, ctx("C-1003")).ok


def test_denied_approval(executor: ToolExecutor, ctx: Ctx) -> None:
    call = _credit(420, "INV-8003")
    with pytest.raises(ApprovalRequired) as pending:
        executor.run("billing", call, ctx("C-1003"))
    executor.grant(pending.value.request["fingerprint"], approved=False, approver="u-1")
    r = executor.run("billing", call, ctx("C-1003"))
    assert r.error_kind == "denied"


def test_rebook_policy(executor: ToolExecutor, ctx: Ctx) -> None:
    def rebook(sid: str, carrier: str, day: str, customer: str = "C-1001") -> str:
        r = executor.run(
            "shipment",
            ToolCall("rebook_shipment", {"shipment_id": sid, "carrier_id": carrier, "pickup_date": day}),
            ctx(customer),
        )
        return "ok" if r.ok else str(r.error)

    assert "costs 33% more" in rebook("AF-24001", "CR-06", "2026-11-03")
    assert "only delayed" in rebook("AF-24007", "CR-03", "2026-11-02")
    assert "no such capacity" in rebook("AF-24001", "CR-01", "2026-11-02")
    assert "not arrive earlier" in rebook("AF-24013", "CR-05", "2026-11-03", "C-1002")
    assert rebook("AF-24001", "CR-02", "2026-11-02") == "ok"


@pytest.mark.parametrize(
    ("body", "fragment"),
    [
        ("Your shipment is fine and on its way to you.", "must reference"),
        ("Ticket T-5001: the internal note says it is fine.", "internal wording"),
        ("Ticket T-5001: AF-24002 is also on the road today.", "not this customer's"),
        ("Ticket T-5001: write to help@aurora.example for more.", "email"),
        ("Ticket T-5001: Hanse Electronics GmbH had the same issue.", "another customer"),
        ("Ticket T-5001 " + "x" * 1300, "longer than"),
    ],
)
def test_message_rules(executor: ToolExecutor, ctx: Ctx, body: str, fragment: str) -> None:
    r = executor.run("comms", _message(body), ctx())
    assert fragment in str(r.error)


def test_message_must_go_to_the_ticket(executor: ToolExecutor, ctx: Ctx) -> None:
    assert (
        executor.run("comms", _message("Ticket T-5001 all good here.", ticket="T-5009"), ctx()).error_kind == "policy"
    )
    assert executor.run("comms", _message("Ticket T-5001 all good here.", customer="C-1002"), ctx()).error_kind == (
        "not_found"
    )
    assert executor.run("comms", _message("Ticket T-5001: AF-24001 is on its way."), ctx()).ok


def test_escalate_only_current_ticket(executor: ToolExecutor, ctx: Ctx) -> None:
    call = ToolCall("escalate", {"ticket_id": "T-0009", "queue": "billing", "reason": "please look"})
    assert executor.run("supervisor", call, ctx()).error_kind == "policy"


def test_retries_absorb_transient_failures(policy: Policy, ctx: Ctx) -> None:
    sleeps: list[float] = []
    backend = OpsBackend.from_file(faults=FaultPlan(rate=1.0, seed=1, max_failures_per_call=2))
    ex = ToolExecutor(backend, policy, InMemoryLedger(), sleep=sleeps.append)
    r = ex.run("shipment", ToolCall("get_shipment", {"shipment_id": "AF-24001"}), ctx())
    assert r.ok and r.attempts == 3
    assert sleeps == [0.02, 0.04]


def test_retries_are_bounded(policy: Policy, ctx: Ctx) -> None:
    backend = OpsBackend.from_file(faults=FaultPlan(rate=1.0, seed=1, max_failures_per_call=99))
    ex = ToolExecutor(backend, policy, InMemoryLedger(), sleep=lambda _s: None)
    r = ex.run("shipment", ToolCall("get_shipment", {"shipment_id": "AF-24001"}), ctx())
    assert (r.ok, r.error_kind, r.attempts) == (False, "transient", 4)


def test_tool_budget(policy: Policy, ctx: Ctx) -> None:
    ex = ToolExecutor(OpsBackend.from_file(), policy, InMemoryLedger())
    for day in range(policy.max_tool_calls):
        assert ex.run(
            "shipment",
            ToolCall("find_capacity", {"lane": "NLRTM-DEHAM", "earliest_pickup": f"2026-11-{day + 1:02d}"}),
            ctx(),
        ).ok
    r = ex.run("shipment", ToolCall("check_sla", {"shipment_id": "AF-24001"}), ctx())
    assert r.error_kind == "budget"


def test_reconciles_a_write_applied_before_a_crash(policy: Policy, ctx: Ctx) -> None:
    backend = OpsBackend.from_file()
    ledger = InMemoryLedger()
    ex = ToolExecutor(backend, policy, ledger)
    call = ToolCall("rebook_shipment", {"shipment_id": "AF-24001", "carrier_id": "CR-02", "pickup_date": "2026-11-02"})
    from opsagents.models import canonical, digest

    key = digest("write", "T-5001", "rebook_shipment", canonical(call.args))
    backend.rebook(key, "AF-24001", "CR-02", "2026-11-02")  # applied, but the ledger never heard
    r = ex.run("shipment", call, ctx())
    assert r.ok and r.attempts == 0
    assert backend.write_count == 1
    assert ledger.ok_result(key) is not None
