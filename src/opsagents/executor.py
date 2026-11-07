"""Deterministic tool execution: the only path from an agent's decision to a side effect.

For every call, in order:
1. the tool exists and is in the calling worker's scope;
2. arguments pass the tool's strict schema, then are canonicalised;
3. object-level authorisation: shipments and invoices must belong to the ticket's customer
   (anything else looks exactly like "not found");
4. a write that already succeeded under the same idempotency key returns the stored result
   (from the ledger, or from the backend when the process died before the ledger saw it);
5. the run's tool budget;
6. policy guards: credit caps and approvals, rebooking rules, message content rules;
7. execution with bounded retries on transient failures;
8. one ledger entry with the outcome.

Agents decide; this module decides whether the decision is allowed to happen.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from pydantic import ValidationError

from opsagents.backend import BackendError, OpsBackend, Transient
from opsagents.ledger import LedgerEntry, LedgerStore
from opsagents.models import Ticket, ToolCall, ToolResult, canonical, digest
from opsagents.policy import Policy
from opsagents.tools import TOOLS

_SHIPMENT_REF = re.compile(r"\bAF-[0-9]{5}\b")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


class ApprovalRequired(Exception):
    """Raised when a write needs a human decision. The graph turns it into an interrupt."""

    def __init__(self, request: dict[str, Any]) -> None:
        super().__init__(request["summary"])
        self.request = request


@dataclass
class RunContext:
    run_id: str
    ticket: Ticket


@dataclass
class ToolExecutor:
    backend: OpsBackend
    policy: Policy
    ledger: LedgerStore
    max_retries: int = 3
    backoff_s: float = 0.02
    sleep: Callable[[float], None] = field(default=time.sleep, repr=False)

    # ------------------------------------------------------------------ public

    def run(self, worker: str, call: ToolCall, ctx: RunContext) -> ToolResult:
        spec = TOOLS.get(call.tool)
        if spec is None:
            return self._reject(ctx, worker, call, "validation", f"unknown tool {call.tool!r}")
        if call.tool not in self.policy.tools_for(worker):
            return self._reject(ctx, worker, call, "permission", f"{worker} may not call {call.tool}")
        try:
            args = spec.args_model.model_validate(call.args)
        except ValidationError as exc:
            first = exc.errors()[0]
            where = ".".join(str(p) for p in first.get("loc", ())) or "arguments"
            return self._reject(ctx, worker, call, "validation", f"{where}: {first.get('msg', 'invalid')}")
        clean = args.model_dump()

        denied = self._authorise(clean, ctx)
        if denied:
            return self._reject(ctx, worker, call, "not_found", denied)

        if spec.kind == "write":
            call_id = digest("write", ctx.ticket.ticket_id, call.tool, canonical(clean))
        else:
            call_id = digest("read", ctx.run_id, call.tool, canonical(clean))
        done = self.ledger.ok_result(call_id)
        if done is not None:
            return ToolResult(True, done.result, cached=True, call_id=call_id, attempts=0)
        if spec.kind == "write":
            # Reconcile with the system of record: a write can be applied there and the process
            # die before the ledger saw it. Its guards passed when it was first sent; re-checking
            # them now would judge the write against the state it created.
            applied = self.backend.applied(call_id)
            if applied is not None:
                return self._record(ctx, worker, call.tool, clean, call_id, "ok", 0, data=applied)

        if self._calls_used(ctx.run_id) >= self.policy.max_tool_calls:
            return self._reject(ctx, worker, call, "budget", "tool-call budget for this ticket is used up")

        if spec.kind == "write":
            problem = self._guard(call.tool, clean, ctx, call_id, worker)
            if problem:
                return self._reject(ctx, worker, call, problem[0], problem[1], call_id=call_id)

        return self._execute(worker, call.tool, spec.handler, args, clean, call_id, ctx)

    def grant(self, fingerprint: str, *, approved: bool, approver: str, note: str = "") -> None:
        self.ledger.put_approval(
            fingerprint, {"approved": approved, "approver": approver, "note": note, "at": time.time()}
        )

    # ------------------------------------------------------------------ steps

    def _authorise(self, args: dict[str, Any], ctx: RunContext) -> str | None:
        customer = ctx.ticket.customer_id
        sid = args.get("shipment_id")
        if sid is not None:
            s = self.backend.shipments.get(sid)
            if s is None or s["customer_id"] != customer:
                return f"shipment {sid} not found for this customer"
        inv_id = args.get("invoice_id")
        if inv_id is not None:
            inv = self.backend.invoices.get(inv_id)
            owner = self.backend.shipments.get(inv["shipment_id"], {}).get("customer_id") if inv else None
            if owner != customer:
                return f"invoice {inv_id} not found for this customer"
        if "customer_id" in args and args["customer_id"] != customer:
            return f"customer {args['customer_id']} not found"
        return None

    def _calls_used(self, run_id: str) -> int:
        return sum(1 for e in self.ledger.entries(run_id) if e.status in {"ok", "error"})

    def _guard(
        self, tool: str, a: dict[str, Any], ctx: RunContext, call_id: str, worker: str
    ) -> tuple[str, str] | None:
        if tool == "issue_credit":
            return self._guard_credit(a, ctx, call_id, worker)
        if tool == "rebook_shipment":
            return self._guard_rebook(a)
        if tool == "send_customer_message":
            return self._guard_message(a, ctx)
        if tool == "escalate" and a["ticket_id"] != ctx.ticket.ticket_id:
            return ("policy", "can only escalate the current ticket")
        return None

    def _guard_credit(self, a: dict[str, Any], ctx: RunContext, call_id: str, worker: str) -> tuple[str, str] | None:
        p = self.policy
        inv = self.backend.invoices[a["invoice_id"]]
        shipment = self.backend.shipments[inv["shipment_id"]]
        amount = float(a["amount_eur"])
        if amount > p.approval_max_eur:
            return ("policy", f"credits above {p.approval_max_eur:.0f} EUR are not issued by agents")
        if p.one_credit_per_invoice_reason and any(
            c["invoice_id"] == inv["id"] and c["reason"] == a["reason"] for c in self.backend.credits
        ):
            return ("policy", f"invoice {inv['id']} already has a {a['reason']} credit")
        if a["reason"] == "sla_breach":
            tier = self.backend.customers[ctx.ticket.customer_id]["tier"]
            cap = p.credit_for(tier, shipment["freight_cost_eur"])
            sla = self.backend.sla(shipment["id"])
            if sla["delay_days"] < 1:
                return ("policy", "shipment is not late; no SLA credit is due")
            if amount > cap:
                return ("policy", f"SLA credit for a {tier} customer is at most {cap:.2f} EUR")
        else:
            twins = [
                i
                for i in self.backend.invoices.values()
                if i["shipment_id"] == inv["shipment_id"] and i["amount_eur"] == inv["amount_eur"]
            ]
            if len(twins) < 2:
                return ("policy", "invoice is not a duplicate")
            if amount > inv["amount_eur"]:
                return ("policy", "credit exceeds the duplicate invoice")
        if amount > p.auto_approve_max_eur:
            decision = self.ledger.get_approval(call_id)
            if decision is None:
                request = {
                    "fingerprint": call_id,
                    "tool": "issue_credit",
                    "args": a,
                    "worker": worker,
                    "ticket_id": ctx.ticket.ticket_id,
                    "customer_id": ctx.ticket.customer_id,
                    "summary": f"Credit {amount:.2f} EUR on {inv['id']} ({a['reason']}) for {ctx.ticket.customer_id}",
                }
                pending = any(
                    e.call_id == call_id and e.status == "approval_required" for e in self.ledger.entries(ctx.run_id)
                )
                if not pending:  # a node re-run after resume must not log the request twice
                    self.ledger.append(
                        LedgerEntry(
                            ctx.run_id,
                            call_id,
                            worker,
                            "issue_credit",
                            "write",
                            a,
                            "approval_required",
                            result=request,
                            attempts=0,
                        )
                    )
                raise ApprovalRequired(request)
            if not decision.get("approved"):
                return ("denied", f"approval denied by {decision.get('approver', 'approver')}")
        return None

    def _guard_rebook(self, a: dict[str, Any]) -> tuple[str, str] | None:
        p = self.policy
        s = self.backend.shipments[a["shipment_id"]]
        sla = self.backend.sla(s["id"])
        if s["status"] != "delayed" or (p.rebook_only_when_at_risk and sla["state"] != "at_risk"):
            return ("policy", "only delayed shipments at risk of missing the SLA are rebooked")
        options = [
            o
            for o in self.backend.find_capacity(s["lane"], a["pickup_date"])
            if o["carrier_id"] == a["carrier_id"] and o["pickup_date"] == a["pickup_date"]
        ]
        if not options:
            return ("policy", "no such capacity")
        o = options[0]
        increase = (o["rate_eur"] - s["freight_cost_eur"]) / s["freight_cost_eur"] * 100
        if increase > p.max_cost_increase_pct:
            return ("policy", f"rebooking costs {increase:.0f}% more; the limit is {p.max_cost_increase_pct:.0f}%")
        if p.must_arrive_earlier and date.fromisoformat(o["eta_date"]) >= date.fromisoformat(s["eta_date"]):
            return ("policy", "rebooking would not arrive earlier than the current ETA")
        return None

    def _guard_message(self, a: dict[str, Any], ctx: RunContext) -> tuple[str, str] | None:
        p = self.policy
        body = a["body"]
        if a["ticket_id"] != ctx.ticket.ticket_id:
            return ("policy", "message must answer the current ticket")
        if p.must_reference_ticket and ctx.ticket.ticket_id not in body:
            return ("policy", f"message must reference {ctx.ticket.ticket_id}")
        if len(body) > p.max_message_chars:
            return ("policy", f"message longer than {p.max_message_chars} characters")
        lowered = body.lower()
        hit = next((t for t in p.forbidden_terms if t in lowered), None)
        if hit:
            return ("policy", f"message contains internal wording ({hit!r})")
        foreign = [
            ref
            for ref in _SHIPMENT_REF.findall(body)
            if self.backend.shipments.get(ref, {}).get("customer_id") != ctx.ticket.customer_id
        ]
        if foreign:
            return ("policy", "message mentions a shipment that is not this customer's")
        if _EMAIL.search(body):
            return ("policy", "message must not contain email addresses")
        others = [c["name"] for cid, c in self.backend.customers.items() if cid != ctx.ticket.customer_id]
        if any(name.lower() in lowered for name in others):
            return ("policy", "message mentions another customer")
        return None

    def _execute(
        self,
        worker: str,
        tool: str,
        handler: Any,
        args: Any,
        clean: dict[str, Any],
        call_id: str,
        ctx: RunContext,
    ) -> ToolResult:
        attempt = 0
        while True:
            attempt += 1
            try:
                self.backend.maybe_fail(call_id, attempt)
                data = handler(self.backend, call_id, args)
            except Transient as exc:
                if attempt <= self.max_retries:
                    self.sleep(self.backoff_s * 2 ** (attempt - 1))
                    continue
                return self._record(ctx, worker, tool, clean, call_id, "error", attempt, err=(str(exc), exc.kind))
            except BackendError as exc:
                return self._record(ctx, worker, tool, clean, call_id, "error", attempt, err=(str(exc), exc.kind))
            return self._record(ctx, worker, tool, clean, call_id, "ok", attempt, data=data)

    def _record(
        self,
        ctx: RunContext,
        worker: str,
        tool: str,
        args: dict[str, Any],
        call_id: str,
        status: str,
        attempts: int,
        data: Any = None,
        err: tuple[str, str] | None = None,
    ) -> ToolResult:
        kind = TOOLS[tool].kind
        self.ledger.append(
            LedgerEntry(
                ctx.run_id,
                call_id,
                worker,
                tool,
                kind,
                args,
                status,
                result=data,
                error=err[0] if err else None,
                error_kind=err[1] if err else None,
                attempts=attempts,
            )
        )
        if err:
            return ToolResult(False, error=err[0], error_kind=err[1], call_id=call_id, attempts=attempts)
        return ToolResult(True, data, call_id=call_id, attempts=attempts)

    def _reject(
        self, ctx: RunContext, worker: str, call: ToolCall, kind: str, message: str, call_id: str = ""
    ) -> ToolResult:
        tool_kind = TOOLS[call.tool].kind if call.tool in TOOLS else "unknown"
        cid = call_id or digest("rejected", ctx.run_id, call.tool, canonical(call.args))
        self.ledger.append(
            LedgerEntry(
                ctx.run_id,
                cid,
                worker,
                call.tool,
                tool_kind,
                dict(call.args),
                "rejected",
                error=message,
                error_kind=kind,
                attempts=0,
            )
        )
        return ToolResult(False, error=message, error_kind=kind, call_id=cid)
