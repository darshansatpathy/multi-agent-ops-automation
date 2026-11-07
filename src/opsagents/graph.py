"""The supervisor/worker graph, built on LangGraph.

    START → supervisor ─┬→ triage ──┐
                        ├→ shipment ┤
                        ├→ billing ─┤ (may pause for approval)
                        ├→ comms ───┤
                        ├→ escalate ┘
                        └→ finish → END

Every worker visit is a bounded loop of decide → execute. Decisions are stored in the
ledger under (run, worker, visit, step), so when LangGraph re-runs a node after an
approval resume, the same actions replay and the executor returns stored results:
nothing is decided twice and nothing is written twice.
"""

from __future__ import annotations

import operator
import uuid
from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from opsagents.backend import OpsBackend
from opsagents.brains import ROUTES, WORKERS, Brain, History, View
from opsagents.executor import ApprovalRequired, RunContext, ToolExecutor
from opsagents.ledger import LedgerStore, entry_dict
from opsagents.models import Action, Handoff, Ticket, ToolCall, action_from_dict
from opsagents.observability import log_event, span


def merge(a: dict[str, Any] | None, b: dict[str, Any] | None) -> dict[str, Any]:
    return {**(a or {}), **(b or {})}


class RunState(TypedDict, total=False):
    run_id: str
    ticket: dict[str, Any]
    facts: Annotated[dict[str, Any], merge]
    done: Annotated[list[str], operator.add]
    outcomes: Annotated[list[str], operator.add]
    trace: Annotated[list[dict[str, Any]], operator.add]
    visits: Annotated[dict[str, Any], merge]
    next: str
    steps: int
    status: str


@dataclass
class RunResult:
    run_id: str
    status: str
    facts: dict[str, Any]
    trace: list[dict[str, Any]]
    pending_approval: dict[str, Any] | None
    ledger: list[dict[str, Any]] = field(default_factory=list)
    duplicate: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "status": self.status,
            "facts": self.facts,
            "trace": self.trace,
            "pending_approval": self.pending_approval,
            "ledger": self.ledger,
            "duplicate": self.duplicate,
        }


class ApprovalError(ValueError):
    pass


@dataclass
class OpsEngine:
    backend: OpsBackend
    executor: ToolExecutor
    ledger: LedgerStore
    brain: Brain
    checkpointer: BaseCheckpointSaver[Any] = field(default_factory=InMemorySaver)

    def __post_init__(self) -> None:
        self.policy = self.executor.policy
        self.graph = self._build().compile(checkpointer=self.checkpointer)

    # ------------------------------------------------------------------ public API

    def submit(self, ticket: Ticket, run_id: str | None = None) -> RunResult:
        """Start a run. A ticket id seen before returns its existing run: webhooks deliver twice."""
        seen = self.ledger.get_decision("tickets", ticket.ticket_id)
        if seen is not None:
            existing = self.get(str(seen["run_id"]))
            existing.duplicate = True
            log_event("duplicate_ticket", ticket_id=ticket.ticket_id, run_id=existing.run_id)
            return existing
        rid = run_id or f"run-{uuid.uuid4().hex[:16]}"
        self.ledger.put_decision("tickets", ticket.ticket_id, {"run_id": rid})
        initial: RunState = {
            "run_id": rid,
            "ticket": ticket.model_dump(),
            "facts": {},
            "done": [],
            "outcomes": [],
            "trace": [],
            "visits": {},
            "steps": 0,
            "status": "running",
        }
        with span("ops.run", {"ops.ticket": ticket.ticket_id, "ops.brain": self.brain.name}):
            self.graph.invoke(initial, self._config(rid))
        return self.get(rid)

    def resume(self, run_id: str, *, approved: bool, approver: str, note: str = "") -> RunResult:
        current = self.get(run_id)
        if current.pending_approval is None:
            raise ApprovalError(f"run {run_id} is not waiting for an approval")
        if not approver or approver == current.pending_approval.get("customer_id"):
            raise ApprovalError("an approver is required")
        decision = {"approved": bool(approved), "approver": approver, "note": note[:500]}
        log_event(
            "approval",
            run_id=run_id,
            approved=approved,
            approver=approver,
            fingerprint=current.pending_approval.get("fingerprint"),
        )
        self.graph.invoke(Command(resume=decision), self._config(run_id))
        return self.get(run_id)

    def recover(self, run_id: str) -> RunResult:
        """Continue a run from its last checkpoint after the process died mid-step."""
        log_event("recover", run_id=run_id)
        self.graph.invoke(None, self._config(run_id))
        return self.get(run_id)

    def get(self, run_id: str) -> RunResult:
        snapshot = self.graph.get_state(self._config(run_id))
        values: dict[str, Any] = dict(snapshot.values or {})
        if not values:
            raise KeyError(run_id)
        pending = None
        for task in snapshot.tasks:
            for intr in task.interrupts:
                pending = dict(intr.value)
        status = "awaiting_approval" if pending else str(values.get("status", "running"))
        return RunResult(
            run_id,
            status,
            dict(values.get("facts", {})),
            list(values.get("trace", [])),
            pending,
            [entry_dict(e) for e in self.ledger.entries(run_id)],
        )

    # ------------------------------------------------------------------ graph

    @staticmethod
    def _config(run_id: str) -> Any:
        return {"configurable": {"thread_id": run_id}, "recursion_limit": 60}

    def _view(self, state: RunState, tools: tuple[str, ...] = ()) -> View:
        return View(
            Ticket(**state["ticket"]),
            self.backend.as_of.isoformat(),
            dict(state.get("facts", {})),
            tuple(state.get("done", [])),
            tuple(state.get("outcomes", [])),
            tools,
        )

    def _build(self) -> StateGraph[RunState]:
        g: StateGraph[RunState] = StateGraph(RunState)
        g.add_node("supervisor", self._supervisor)
        for worker in WORKERS:
            g.add_node(worker, self._worker_node(worker))
        g.add_node("escalate", self._escalate)
        g.add_node("finish", self._finish)
        g.add_edge(START, "supervisor")
        g.add_conditional_edges("supervisor", lambda s: s["next"], [*WORKERS, "escalate", "finish"])
        for worker in (*WORKERS, "escalate"):
            g.add_edge(worker, "supervisor")
        g.add_edge("finish", END)
        return g

    def _supervisor(self, state: RunState) -> dict[str, Any]:
        steps = int(state.get("steps", 0)) + 1
        facts = state.get("facts", {})
        if steps > self.policy.max_steps:
            nxt, reason = (
                ("finish", "step budget used up") if facts.get("escalation") else ("escalate", "step budget used up")
            )
            if nxt == "escalate":
                return {
                    "steps": steps,
                    "next": nxt,
                    "facts": {
                        "escalate_requested": True,
                        "escalate_queue": "customer_service",
                        "escalate_reason": reason,
                    },
                    "trace": [{"step": "route", "next": nxt, "reason": reason}],
                }
        else:
            key = f"supervisor:{steps}"
            cached = self.ledger.get_decision(state["run_id"], key)
            if cached is not None:
                nxt, reason = str(cached["next"]), str(cached["reason"])
            else:
                nxt, reason = self.brain.route(self._view(state))
                self.ledger.put_decision(state["run_id"], key, {"next": nxt, "reason": reason})
        if nxt not in ROUTES:
            nxt, reason = "escalate", f"invalid route {nxt!r}"
        if nxt in WORKERS and nxt in state.get("done", []):
            nxt, reason = "escalate", f"{nxt} already ran; refusing to loop"
        if nxt == "escalate" and facts.get("escalation"):
            nxt, reason = "finish", "already escalated"
        log_event("route", run_id=state["run_id"], next=nxt, reason=reason)
        return {"steps": steps, "next": nxt, "trace": [{"step": "route", "next": nxt, "reason": reason}]}

    def _worker_node(self, worker: str) -> Any:
        def node(state: RunState) -> dict[str, Any]:
            run_id = state["run_id"]
            visit = int(state.get("visits", {}).get(worker, 0)) + 1
            ctx = RunContext(run_id, Ticket(**state["ticket"]))
            view = self._view(state, tuple(sorted(self.policy.tools_for(worker))))
            history: History = []
            trace: list[dict[str, Any]] = []
            handoff: Handoff | None = None
            with span(f"ops.worker.{worker}", {"ops.run": run_id, "ops.visit": visit}):
                for step in range(self.policy.max_actions_per_visit):
                    action = self._decide(worker, view, history, run_id, f"{worker}:{visit}:{step}")
                    if isinstance(action, Handoff):
                        handoff = action
                        break
                    try:
                        result = self.executor.run(worker, action, ctx)
                    except ApprovalRequired as pending:
                        decision = interrupt({**pending.request, "run_id": run_id})
                        self.executor.grant(
                            pending.request["fingerprint"],
                            approved=bool(decision["approved"]),
                            approver=str(decision["approver"]),
                            note=str(decision.get("note", "")),
                        )
                        trace.append(
                            {
                                "step": "approval",
                                "worker": worker,
                                "approved": bool(decision["approved"]),
                                "approver": str(decision["approver"]),
                            }
                        )
                        result = self.executor.run(worker, action, ctx)
                    observation = result.observation()
                    history.append((action, observation))
                    trace.append(
                        {
                            "step": "tool",
                            "worker": worker,
                            "tool": action.tool,
                            "ok": result.ok,
                            "cached": result.cached,
                            "attempts": result.attempts,
                            **({} if result.ok else {"error": result.error, "error_kind": result.error_kind}),
                        }
                    )
                if handoff is None:
                    handoff = Handoff("escalate", f"{worker} did not finish within its action limit")
            facts = dict(handoff.facts)
            if handoff.outcome == "escalate":
                facts.setdefault("escalate_requested", True)
                facts.setdefault("escalate_queue", "customer_service")
                facts.setdefault("escalate_reason", handoff.summary or f"{worker} asked for a human")
            trace.append({"step": "handoff", "worker": worker, "outcome": handoff.outcome, "summary": handoff.summary})
            return {
                "facts": facts,
                "done": [worker],
                "outcomes": [f"{worker}:{handoff.outcome}"],
                "trace": trace,
                "visits": {worker: visit},
            }

        return node

    def _decide(self, worker: str, view: View, history: History, run_id: str, key: str) -> Action:
        cached = self.ledger.get_decision(run_id, key)
        if cached is not None:
            return action_from_dict(cached)
        action = self.brain.act(worker, view, history)
        self.ledger.put_decision(run_id, key, action.to_dict())
        return action

    def _escalate(self, state: RunState) -> dict[str, Any]:
        facts = state.get("facts", {})
        ticket = Ticket(**state["ticket"])
        queue = str(facts.get("escalate_queue", "customer_service"))
        if queue not in {"customer_service", "billing", "claims", "security"}:
            queue = "customer_service"
        reason = str(facts.get("escalate_reason") or "needs a person")[:500]
        if len(reason) < 5:
            reason = "needs a person"
        call = ToolCall("escalate", {"ticket_id": ticket.ticket_id, "queue": queue, "reason": reason})
        result = self.executor.run("supervisor", call, RunContext(state["run_id"], ticket))
        escalation = result.data if result.ok else {"escalation_id": "pending", "queue": queue}
        return {
            "facts": {"escalation": escalation},
            "done": ["escalate"],
            "outcomes": ["escalate:done"],
            "trace": [
                {
                    "step": "tool",
                    "worker": "supervisor",
                    "tool": "escalate",
                    "ok": result.ok,
                    "cached": result.cached,
                    "attempts": result.attempts,
                }
            ],
        }

    def _finish(self, state: RunState) -> dict[str, Any]:
        facts = state.get("facts", {})
        outcomes = state.get("outcomes", [])
        escalation = facts.get("escalation") or {}
        replied = "comms:done" in outcomes
        if escalation.get("escalation_id") == "pending" or not replied:
            status = "failed"  # a person must pick this up from the logs: nothing reached the customer or queue
        else:
            status = "escalated" if escalation else "resolved"
        log_event("run_finished", run_id=state["run_id"], status=status)
        return {"status": status, "trace": [{"step": "finish", "status": status}]}


def make_engine(
    backend: OpsBackend, executor: ToolExecutor, brain: Brain, checkpointer: BaseCheckpointSaver[Any] | None = None
) -> OpsEngine:
    return OpsEngine(backend, executor, executor.ledger, brain, checkpointer or InMemorySaver())


__all__ = ["ApprovalError", "OpsEngine", "RunResult", "RunState", "ToolCall", "make_engine"]
