import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from opsagents.backend import Crash, FaultPlan, OpsBackend
from opsagents.brains import History, View
from opsagents.brains.llm import LLMBrain
from opsagents.brains.rule import RuleBrain
from opsagents.executor import ToolExecutor
from opsagents.graph import ApprovalError, OpsEngine, make_engine
from opsagents.ledger import SqlLedger
from opsagents.models import Action, Handoff, Ticket, ToolCall
from opsagents.policy import Policy
from opsagents.providers import ModelReply, ScriptedProvider, ToolCallRequest

Make = Callable[..., Ticket]
Engine = Callable[..., OpsEngine]
CREDIT = "AF-24003 is two days late. We expect the SLA credit."


def test_resolved_run_has_route_writes_and_reply(engine_for: Engine, ticket: Make) -> None:
    engine = engine_for()
    r = engine.submit(ticket(body="Our shipment AF-24001 looks late. When will it arrive?"), run_id="r1")
    assert r.status == "resolved"
    assert [t["next"] for t in r.trace if t["step"] == "route"] == ["triage", "shipment", "comms", "finish"]
    assert {e["tool"] for e in r.ledger if e["kind"] == "write"} == {"rebook_shipment", "send_customer_message"}
    assert engine.backend.messages[0]["ticket_id"] == "T-5001"
    with pytest.raises(KeyError):
        engine.get("no-such-run")


def test_approval_pauses_then_resumes(engine_for: Engine, ticket: Make) -> None:
    engine = engine_for()
    r = engine.submit(ticket("C-1003", CREDIT), run_id="r1")
    assert r.status == "awaiting_approval"
    assert r.pending_approval is not None and "420.00 EUR" in r.pending_approval["summary"]
    assert engine.backend.messages == []  # nothing sent while waiting
    with pytest.raises(ApprovalError):
        engine.resume("r1", approved=True, approver="")
    done = engine.resume("r1", approved=True, approver="u-approver", note="ok")
    assert done.status == "resolved"
    assert any(t["step"] == "approval" and t["approver"] == "u-approver" for t in done.trace)
    assert [c["amount_eur"] for c in engine.backend.credits[1:]] == [420.0]
    with pytest.raises(ApprovalError, match="not waiting"):
        engine.resume("r1", approved=True, approver="u-approver")


def test_denied_approval_issues_nothing(engine_for: Engine, ticket: Make) -> None:
    engine = engine_for()
    engine.submit(ticket("C-1003", CREDIT), run_id="r1")
    done = engine.resume("r1", approved=False, approver="u-approver")
    assert done.status == "resolved" and done.facts["credit"]["status"] == "denied"
    assert len(engine.backend.credits) == 1


def test_resume_replays_decisions_without_new_reads(engine_for: Engine, ticket: Make) -> None:
    engine = engine_for()
    engine.submit(ticket("C-1003", CREDIT), run_id="r1")
    reads_before = sum(1 for e in engine.ledger.entries("r1") if e.kind == "read")
    engine.resume("r1", approved=True, approver="u")
    reads_after = sum(1 for e in engine.ledger.entries("r1") if e.kind == "read")
    assert reads_after == reads_before  # the billing node re-ran from cache


def test_duplicate_ticket_returns_the_first_run(engine_for: Engine, ticket: Make) -> None:
    engine = engine_for()
    t = ticket(body="AF-24001 will miss the promised date. Please apply our SLA credit.")
    first = engine.submit(t, run_id="r1")
    writes = engine.backend.write_count
    again = engine.submit(t)
    assert again.duplicate and again.run_id == "r1" == first.run_id
    assert engine.backend.write_count == writes


def test_recover_after_a_crash_does_not_repeat_the_write(engine_for: Engine, ticket: Make) -> None:
    engine = engine_for(faults=FaultPlan(crash_after="issue_credit"))
    t = ticket(body="AF-24001 will miss the promised date. Please apply our SLA credit.")
    with pytest.raises(Crash):
        engine.submit(t, run_id="r1")
    r = engine.recover("r1")
    assert r.status == "resolved"
    assert [c["amount_eur"] for c in engine.backend.credits[1:]] == [180.0]
    assert engine.backend.write_count == 2  # the credit and the reply, once each


def test_approval_survives_a_process_restart(policy: Policy, ticket: Make, tmp_path: Path) -> None:
    """Pause for approval, drop every in-memory object, rebuild from SQLite, then approve."""
    db = str(tmp_path / "ops.db")
    backend = OpsBackend.from_file()

    def engine() -> tuple[OpsEngine, list[Any]]:
        conn = sqlite3.connect(db, check_same_thread=False)
        ledger = SqlLedger.sqlite(db)
        ex = ToolExecutor(backend, policy, ledger, sleep=lambda _s: None)
        return make_engine(backend, ex, RuleBrain(policy), SqliteSaver(conn)), [conn, ledger]

    first, closers = engine()
    assert first.submit(ticket("C-1003", CREDIT), run_id="r1").status == "awaiting_approval"
    for c in closers:
        c.close()
    second, closers = engine()
    try:
        assert second.get("r1").status == "awaiting_approval"
        assert second.resume("r1", approved=True, approver="u").status == "resolved"
        assert [c["amount_eur"] for c in backend.credits[1:]] == [420.0]
    finally:
        for c in closers:
            c.close()


class LoopingBrain:
    name = "looping"

    def route(self, view: View) -> tuple[str, str]:
        return ("triage" if "triage" not in view.done else "shipment"), "again"

    def act(self, worker: str, view: View, history: History) -> Action:
        return ToolCall("get_customer", {"customer_id": view.ticket.customer_id, "n": len(history)})


def test_misbehaving_brain_is_bounded(engine_for: Engine, ticket: Make) -> None:
    engine = engine_for(brain=LoopingBrain())
    r = engine.submit(ticket(), run_id="r1")
    outcomes = [t for t in r.trace if t["step"] == "handoff"]
    assert all(o["outcome"] == "escalate" for o in outcomes)
    assert "action limit" in outcomes[0]["summary"]
    assert any(t.get("next") == "escalate" for t in r.trace)
    assert r.status == "failed"  # it never replied, and a person picks it up from the escalation
    assert engine.backend.escalations


def test_step_budget(engine_for: Engine, ticket: Make, policy: Policy) -> None:
    class Wanderer(RuleBrain):
        def route(self, view: View) -> tuple[str, str]:
            return "escalate", "keep escalating"

    engine = engine_for(brain=Wanderer(policy))
    r = engine.submit(ticket(), run_id="r1")
    routes = [t for t in r.trace if t["step"] == "route"]
    assert len(routes) <= policy.max_steps + 1
    assert r.status == "failed"


def test_llm_brain_end_to_end(engine_for: Engine, ticket: Make, policy: Policy) -> None:
    def call(name: str, args: str) -> ModelReply:
        return ModelReply(None, (ToolCallRequest(name, args),))

    body = "Hello Nordwind Retail BV,\\n\\nShipment AF-24002 is moving.\\n\\nTicket T-5001"
    script = [
        call("route", '{"next": "triage", "reason": "start"}'),
        call("get_customer", '{"customer_id": "C-1001"}'),
        call("handoff", '{"outcome": "done", "summary": "ok", "facts": {"intent": "delay_inquiry"}}'),
        call("route", '{"next": "comms", "reason": "reply"}'),
        call("send_customer_message", '{"customer_id": "C-1001", "ticket_id": "T-5001", "body": "' + body + '"}'),
        call(
            "send_customer_message",
            '{"customer_id": "C-1001", "ticket_id": "T-5001", '
            '"body": "Hello,\\n\\nWe are looking into it now.\\n\\nTicket T-5001"}',
        ),
        call("handoff", '{"outcome": "done", "summary": "sent"}'),
        call("route", '{"next": "finish", "reason": "done"}'),
    ]
    engine = engine_for(brain=LLMBrain(ScriptedProvider(script), policy))
    r = engine.submit(ticket(), run_id="r1")
    assert r.status == "resolved"
    blocked = [e for e in r.ledger if e["status"] == "rejected"]
    assert len(blocked) == 1 and "not this customer's" in blocked[0]["error"]
    assert len(engine.backend.messages) == 1


def test_handoff_without_facts_still_escalates(engine_for: Engine, ticket: Make) -> None:
    class Shrugs:
        name = "shrugs"

        def route(self, view: View) -> tuple[str, str]:
            if "triage" not in view.done:
                return "triage", "start"
            if view.facts.get("escalation"):
                return ("comms", "tell them") if "comms" not in view.done else ("finish", "done")
            return "escalate", "asked"

        def act(self, worker: str, view: View, history: History) -> Action:
            if worker == "comms":
                if history:
                    return Handoff("done", "sent")
                t = view.ticket
                return ToolCall(
                    "send_customer_message",
                    {
                        "customer_id": t.customer_id,
                        "ticket_id": t.ticket_id,
                        "body": f"We passed this on. Ticket {t.ticket_id}",
                    },
                )
            return Handoff("escalate", "")

    r = engine_for(brain=Shrugs()).submit(ticket(), run_id="r1")
    assert r.status == "escalated"
    assert r.facts["escalate_queue"] == "customer_service"
