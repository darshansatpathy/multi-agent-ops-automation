"""The rule brain: deterministic decisions that follow the operating policy.

It is the offline default and the baseline the evaluation holds a model to. Each worker
is a small program written as a generator: it yields tool calls, receives their
observations, and returns a handoff. Re-running the program with the same observations
yields the same next action, which is what makes replay after a resume exact.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Generator
from datetime import date
from typing import Any

from opsagents.brains import History, View
from opsagents.models import Action, Handoff, ToolCall
from opsagents.policy import Policy

Program = Generator[ToolCall, dict[str, Any], Handoff]

_SHIPMENT = re.compile(r"\bAF-[0-9]{5}\b")
_INJECTION = re.compile(
    r"ignore (all |any )?(previous|prior|above) (instructions|rules)|you are now|system prompt|"
    r"(as an? )?(ai|assistant|agent),? you (must|should|are authori[sz]ed)|override (the )?policy|"
    r"developer mode|disregard (your|the) (rules|policy|instructions)",
    re.I,
)
_INTENTS: list[tuple[str, re.Pattern[str]]] = [
    (
        "out_of_scope",
        re.compile(
            r"change (the )?(delivery )?address|new (quote|booking)|quotation|"
            r"cancel (my|the) (account|contract)|job|vacanc",
            re.I,
        ),
    ),
    ("damaged_goods", re.compile(r"damag|crushed|broken|dented|water ?logged", re.I)),
    (
        "invoice_dispute",
        re.compile(
            r"invoice|charged twice|double[- ]charg|billed twice|duplicate|overcharg|"
            r"surcharge",
            re.I,
        ),
    ),
    ("sla_credit", re.compile(r"credit|compensat|refund|sla|penalt", re.I)),
    ("delay_inquiry", re.compile(r"where is|delay|late|eta|status|when will|arriv|track", re.I)),
]

PLANS: dict[str, tuple[str, ...]] = {
    "delay_inquiry": ("shipment", "comms"),
    "sla_credit": ("shipment", "billing", "comms"),
    "damaged_goods": ("shipment", "comms"),
    "invoice_dispute": ("billing", "comms"),
}


def classify(text: str) -> str:
    for intent, pattern in _INTENTS:
        if pattern.search(text):
            return intent
    return "unknown"


def _escalate(queue: str, reason: str, **facts: Any) -> Handoff:
    return Handoff(
        "escalate", reason, {"escalate_requested": True, "escalate_queue": queue, "escalate_reason": reason, **facts}
    )


def _call(tool: str, **args: Any) -> ToolCall:
    return ToolCall(tool, args)


class RuleBrain:
    name = "rule"

    def __init__(self, policy: Policy) -> None:
        self.policy = policy
        self._programs: dict[str, Callable[[View], Program]] = {
            "triage": self._triage,
            "shipment": self._shipment,
            "billing": self._billing,
            "comms": self._comms,
        }

    # ------------------------------------------------------------------ supervisor

    def route(self, view: View) -> tuple[str, str]:
        f, done = view.facts, view.done
        if "triage" not in done:
            return "triage", "every ticket starts with triage"
        if f.get("escalate_requested") and not f.get("escalation"):
            return "escalate", str(f.get("escalate_reason", "worker asked for a human"))
        if f.get("escalation"):
            return ("comms", "tell the customer it was escalated") if "comms" not in done else ("finish", "done")
        for worker in PLANS.get(str(f.get("intent")), ()):
            if worker not in done:
                return worker, f"next step for {f.get('intent')}"
        return ("comms", "reply to the customer") if "comms" not in done else ("finish", "done")

    # ------------------------------------------------------------------ workers

    def act(self, worker: str, view: View, history: History) -> Action:
        program = self._programs[worker](view)
        try:
            action: Action = next(program)
            for _, observation in history:
                action = program.send(observation)
        except StopIteration as stop:
            return stop.value  # type: ignore[no-any-return]
        return action

    def _triage(self, v: View) -> Program:
        t = v.ticket
        text = f"{t.subject}\n{t.body}"
        customer = yield _call("get_customer", customer_id=t.customer_id)
        name = customer["data"]["name"] if customer["ok"] else ""
        tier = customer["data"]["tier"] if customer["ok"] else "standard"
        base = {"customer_name": name, "tier": tier}
        if _INJECTION.search(text):
            return _escalate(
                "security",
                "ticket contains instructions aimed at the agent",
                intent="suspicious",
                suspicious=True,
                **base,
            )
        intent = classify(text)
        if intent in {"unknown", "out_of_scope"}:
            return _escalate("customer_service", f"request type is {intent.replace('_', ' ')}", intent=intent, **base)
        refs = sorted(set(_SHIPMENT.findall(text)))
        if not refs:
            return _escalate("customer_service", "no shipment reference in the ticket", intent=intent, **base)
        if len(refs) > 1:
            return _escalate("customer_service", "ticket mentions several shipments", intent=intent, **base)
        shipment = yield _call("get_shipment", shipment_id=refs[0])
        if not shipment["ok"]:
            return _escalate(
                "customer_service", "shipment reference not found for this customer", intent=intent, **base
            )
        s = shipment["data"]
        return Handoff(
            "done",
            f"{intent} about {s['id']}",
            {"intent": intent, "shipment_id": s["id"], "lane": s["lane"], "shipment_status": s["status"], **base},
        )

    def _shipment(self, v: View) -> Program:
        p, f = self.policy, v.facts
        sid = str(f["shipment_id"])
        shipment = yield _call("get_shipment", shipment_id=sid)
        s = shipment["data"]
        sla_obs = yield _call("check_sla", shipment_id=sid)
        sla = sla_obs["data"]
        facts: dict[str, Any] = {"sla": sla}
        if f["intent"] == "damaged_goods":
            severity = "high" if s["cargo_value_eur"] >= 50_000 else "medium"
            inc = yield _call(
                "create_incident",
                kind="damage_claim",
                shipment_id=sid,
                severity=severity,
                summary=f"Customer reports damaged goods on {sid}",
            )
            if not inc["ok"]:
                return _escalate("claims", "could not open a damage claim", **facts)
            return Handoff("done", f"claim {inc['data']['incident_id']} opened", {**facts, "incident": inc["data"]})
        if f["intent"] != "delay_inquiry":
            return Handoff("done", f"SLA state {sla['state']}", facts)
        if s["status"] == "held_customs":
            return Handoff("done", "held at customs", {**facts, "customs_hold": True})
        if s["status"] != "delayed" or sla["state"] != "at_risk":
            return Handoff("done", f"no action needed ({sla['state']})", facts)
        options = yield _call("find_capacity", lane=s["lane"], earliest_pickup=v.as_of)
        chosen = None
        for o in options["data"] if options["ok"] else []:
            increase = (o["rate_eur"] - s["freight_cost_eur"]) / s["freight_cost_eur"] * 100
            if o["eta_date"] < s["eta_date"] and increase <= p.max_cost_increase_pct:
                chosen = o
                break
        if chosen is None:
            return Handoff("done", "no acceptable alternative capacity", {**facts, "rebooked": None})
        rb = yield _call(
            "rebook_shipment", shipment_id=sid, carrier_id=chosen["carrier_id"], pickup_date=chosen["pickup_date"]
        )
        if not rb["ok"]:
            return Handoff("done", f"rebooking failed: {rb['error']}", {**facts, "rebooked": None})
        return Handoff("done", f"rebooked to {chosen['carrier_id']}", {**facts, "rebooked": rb["data"]})

    def _billing(self, v: View) -> Program:
        p, f = self.policy, v.facts
        sid = str(f["shipment_id"])
        invoices = yield _call("get_invoices", shipment_id=sid)
        invs: list[dict[str, Any]] = invoices["data"] if invoices["ok"] else []
        if f["intent"] == "invoice_dispute":
            dup: dict[str, Any] | None = None
            original: dict[str, Any] = {}
            for inv in invs:
                twins = [i for i in invs if i["amount_eur"] == inv["amount_eur"]]
                if len(twins) > 1 and inv["id"] == twins[-1]["id"]:
                    dup, original = inv, twins[0]
            if dup is None:
                return _escalate("billing", "invoice dispute needs a person to review the charges")
            if any(c["reason"] == "duplicate_invoice" for c in dup["credits"]):
                return Handoff(
                    "done",
                    "duplicate already credited",
                    {"credit": {"status": "already_credited", "invoice_id": dup["id"]}},
                )
            return (
                yield from self._credit(
                    dup["id"], float(dup["amount_eur"]), "duplicate_invoice", {"duplicate_of": original["id"]}
                )
            )
        sla = f.get("sla") or {}
        if int(sla.get("delay_days", 0)) < 1:
            return Handoff(
                "done", "no credit due (not late)", {"credit": {"status": "not_eligible", "why": "not_late"}}
            )
        if not invs:
            return _escalate("billing", "no invoice found for this shipment")
        primary = invs[0]
        if any(c["reason"] == "sla_breach" for c in primary["credits"]):
            return Handoff(
                "done", "already credited", {"credit": {"status": "already_credited", "invoice_id": primary["id"]}}
            )
        amount = p.credit_for(str(f.get("tier", "standard")), float(primary["amount_eur"]))
        if amount <= 0:
            return Handoff(
                "done",
                "no credit due (not in agreement)",
                {"credit": {"status": "not_eligible", "why": "not_in_agreement", "invoice_id": primary["id"]}},
            )
        if amount > p.approval_max_eur:
            return _escalate(
                "billing",
                f"credit of {amount:.2f} EUR is above the agent limit",
                credit={"status": "escalated", "amount_eur": amount, "invoice_id": primary["id"]},
            )
        return (yield from self._credit(primary["id"], amount, "sla_breach", {}))

    @staticmethod
    def _credit(invoice_id: str, amount: float, reason: str, extra: dict[str, Any]) -> Program:
        res = yield _call("issue_credit", invoice_id=invoice_id, amount_eur=amount, reason=reason)
        if res["ok"]:
            return Handoff(
                "done",
                f"credit {res['data']['credit_id']} issued",
                {
                    "credit": {
                        "status": "issued",
                        "amount_eur": amount,
                        "invoice_id": invoice_id,
                        "credit_id": res["data"]["credit_id"],
                        **extra,
                    }
                },
            )
        if res.get("error_kind") == "denied":
            return Handoff(
                "done",
                "approver declined the credit",
                {"credit": {"status": "denied", "amount_eur": amount, "invoice_id": invoice_id}},
            )
        return _escalate(
            "billing",
            f"credit could not be issued: {res['error']}",
            credit={"status": "failed", "amount_eur": amount, "invoice_id": invoice_id},
        )

    def _comms(self, v: View) -> Program:
        t = v.ticket
        res = yield _call(
            "send_customer_message", customer_id=t.customer_id, ticket_id=t.ticket_id, body=compose_message(v)
        )
        if not res["ok"]:
            return _escalate("customer_service", f"reply was blocked: {res['error']}")
        return Handoff("done", "reply sent", {"message_id": res["data"]["message_id"]})


_QUEUE_LABEL = {
    "customer_service": "customer service",
    "billing": "billing",
    "claims": "claims",
    "security": "customer service",
}


def _days(n: int) -> str:
    return f"{n} day" if n == 1 else f"{n} days"


def _day(iso: str) -> str:
    y, m, d = (int(x) for x in iso.split("-"))
    months = [
        "January",
        "February",
        "March",
        "April",
        "May",
        "June",
        "July",
        "August",
        "September",
        "October",
        "November",
        "December",
    ]
    return f"{d} {months[m - 1]} {y}"


def compose_message(v: View) -> str:
    """The customer reply, built only from facts the workers established."""
    f, t = v.facts, v.ticket
    greeting = f"Hello {f['customer_name']}," if f.get("customer_name") else "Hello,"
    lines: list[str] = []
    if f.get("escalation"):
        esc = f["escalation"]
        team = _QUEUE_LABEL.get(str(esc.get("queue")), "customer service")
        lines.append(
            f"Thank you for your message. We have passed it to our {team} team, who will reply "
            f"within one working day. Your reference is {esc.get('escalation_id')}."
        )
    else:
        sid, sla, intent = f.get("shipment_id"), f.get("sla") or {}, f.get("intent")
        if intent == "damaged_goods" and f.get("incident"):
            lines.append(
                f"We are sorry the goods on shipment {sid} arrived damaged. We have opened a claim "
                f"(reference {f['incident']['incident_id']}), and our claims team will contact you "
                "within two working days."
            )
        elif intent == "delay_inquiry":
            if f.get("rebooked"):
                rb = f["rebooked"]
                late = (date.fromisoformat(rb["new_eta"]) - date.fromisoformat(sla["promised_date"])).days
                timing = (
                    "on the promised date"
                    if late <= 0
                    else f"{_days(late)} after the promised date, earlier than before"
                )
                lines.append(
                    f"Shipment {sid} was running late, so we have moved it to an earlier departure. "
                    f"We now expect delivery on {_day(rb['new_eta'])}, {timing}."
                )
            elif f.get("customs_hold"):
                lines.append(
                    f"Shipment {sid} is waiting for customs clearance. We expect delivery on "
                    f"{_day(sla['eta_date'])} once it is released."
                )
            elif sla.get("delivered"):
                lines.append(f"Shipment {sid} was delivered on {_day(sla['eta_date'])}.")
            elif sla.get("state") == "at_risk":
                lines.append(
                    f"Shipment {sid} is running late. We now expect delivery on {_day(sla['eta_date'])}, "
                    f"{_days(sla['delay_days'])} after the promised date, and we are following it closely."
                )
            else:
                lines.append(f"Shipment {sid} is on schedule for delivery on {_day(sla['eta_date'])}.")
        credit = f.get("credit") or {}
        status = credit.get("status")
        if status == "issued" and credit.get("duplicate_of"):
            lines.append(
                f"You were right: invoice {credit['invoice_id']} duplicated invoice "
                f"{credit['duplicate_of']}. We have issued a credit of {credit['amount_eur']:.2f} EUR "
                f"(credit note {credit['credit_id']})."
            )
        elif status == "issued":
            verb = "arrived" if sla.get("delivered") else "is running"
            lines.append(
                f"Shipment {sid} {verb} {_days(int(sla.get('delay_days', 0)))} behind the promised date, "
                f"so we have issued a credit of {credit['amount_eur']:.2f} EUR against invoice "
                f"{credit['invoice_id']} (credit note {credit['credit_id']})."
            )
        elif status == "denied":
            lines.append("We have reviewed your request and are not able to issue a credit in this case.")
        elif status == "already_credited":
            lines.append(f"A credit for this was already issued against invoice {credit['invoice_id']}.")
        elif status == "not_eligible" and credit.get("why") == "not_late":
            lines.append(
                f"Our records show shipment {sid} arriving on the promised date, so no late-delivery credit applies."
            )
        elif status == "not_eligible":
            verb = "arrived" if sla.get("delivered") else "is running"
            lines.append(
                f"Shipment {sid} {verb} {_days(int(sla.get('delay_days', 0)))} late. Your service agreement "
                "does not include a late-delivery credit, and we apologise for the delay."
            )
    body = "\n\n".join([greeting, *lines, f"Kind regards,\nAurora Freight customer operations\nTicket {t.ticket_id}"])
    return body
