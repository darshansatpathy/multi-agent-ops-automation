"""A simulated operations backend: TMS (shipments), CRM (customers), ERP (invoices).

It stands in for the real systems behind the tools. Writes take an idempotency key,
as payment and booking APIs do, so a retried call can't apply twice. A fault plan can
make calls fail transiently, deterministically, to exercise retries in tests and evals.
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

_PACKAGED = Path(__file__).resolve().parent / "data" / "ops_seed.json"
_REPO = Path(__file__).resolve().parents[2] / "data" / "ops_seed.json"


def default_seed_path() -> Path:
    return _PACKAGED if _PACKAGED.exists() else _REPO


class BackendError(Exception):
    kind = "backend"


class NotFound(BackendError):
    kind = "not_found"


class Transient(BackendError):
    """Timeouts, 503s: worth retrying."""

    kind = "transient"


class Conflict(BackendError):
    kind = "conflict"


class Crash(Exception):
    """The worker process died right after a write was applied, before it was recorded."""


@dataclass(frozen=True)
class FaultPlan:
    """Fail a share of calls, decided by a hash of (seed, call, attempt): reproducible chaos."""

    rate: float = 0.0
    seed: int = 0
    max_failures_per_call: int = 2  # then succeed, so retries can recover
    crash_after: str | None = None  # write tool after which the process "dies" once

    def should_fail(self, call_id: str, attempt: int) -> bool:
        if self.rate <= 0 or attempt > self.max_failures_per_call:
            return False
        h = hashlib.sha256(f"{self.seed}:{call_id}:{attempt}".encode()).digest()
        return int.from_bytes(h[:4], "big") / 2**32 < self.rate


@dataclass
class OpsBackend:
    seed: dict[str, Any]
    faults: FaultPlan = field(default_factory=FaultPlan)

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self.as_of = date.fromisoformat(self.seed["as_of"])
        self.customers = {c["id"]: c for c in self.seed["customers"]}
        self.carriers = {c["id"]: c for c in self.seed["carriers"]}
        self.shipments = {s["id"]: copy.deepcopy(s) for s in self.seed["shipments"]}
        self.invoices = {i["id"]: i for i in self.seed["invoices"]}
        self.capacity = list(self.seed["capacity"])
        self.credits: list[dict[str, Any]] = [
            {"credit_id": f"CN-PRIOR-{n}", **c} for n, c in enumerate(self.seed.get("prior_credits", []), 1)
        ]
        self.messages: list[dict[str, Any]] = []
        self.incidents: list[dict[str, Any]] = []
        self.rebookings: list[dict[str, Any]] = []
        self.escalations: list[dict[str, Any]] = []
        self._idempotent: dict[str, dict[str, Any]] = {}
        self._crashed = False
        self.write_count = 0  # writes actually applied (not replays): the evaluation checks this

    @classmethod
    def from_file(cls, path: str | Path | None = None, faults: FaultPlan | None = None) -> OpsBackend:
        seed = json.loads(Path(path or default_seed_path()).read_text(encoding="utf-8"))
        return cls(seed=seed, faults=faults or FaultPlan())

    # ------------------------------------------------------------------ plumbing

    def maybe_fail(self, call_id: str, attempt: int) -> None:
        if self.faults.should_fail(call_id, attempt):
            raise Transient("backend timed out")

    def applied(self, key: str) -> dict[str, Any] | None:
        """The stored result of a write already applied under this idempotency key, if any."""
        with self._lock:
            return self._idempotent.get(key)

    def _apply_once(self, key: str, fn: Any, tool: str) -> dict[str, Any]:
        with self._lock:
            if key in self._idempotent:
                return self._idempotent[key]
            result: dict[str, Any] = fn()
            self._idempotent[key] = result
            self.write_count += 1
        if self.faults.crash_after == tool and not self._crashed:
            self._crashed = True
            raise Crash(f"process died after {tool}")
        return result

    # ------------------------------------------------------------------ reads

    def customer(self, customer_id: str) -> dict[str, Any]:
        c = self.customers.get(customer_id)
        if c is None:
            raise NotFound(f"customer {customer_id} not found")
        return {"id": c["id"], "name": c["name"], "tier": c["tier"], "country": c["country"]}

    def shipment(self, shipment_id: str) -> dict[str, Any]:
        s = self.shipments.get(shipment_id)
        if s is None:
            raise NotFound(f"shipment {shipment_id} not found")
        return dict(s)

    def tracking(self, shipment_id: str) -> list[dict[str, Any]]:
        self.shipment(shipment_id)
        return list(self.seed.get("tracking", {}).get(shipment_id, []))

    def sla(self, shipment_id: str) -> dict[str, Any]:
        s = self.shipment(shipment_id)
        promised = date.fromisoformat(s["promised_date"])
        eta = date.fromisoformat(s["eta_date"])
        delay = max(0, (eta - promised).days)
        delivered = s["status"] == "delivered"
        if delay == 0:
            state = "on_time"
        elif delivered:
            state = "breached"
        else:
            state = "at_risk"
        return {
            "shipment_id": shipment_id,
            "promised_date": s["promised_date"],
            "eta_date": s["eta_date"],
            "delay_days": delay,
            "state": state,
            "delivered": delivered,
        }

    def find_capacity(self, lane: str, earliest: str) -> list[dict[str, Any]]:
        day = date.fromisoformat(earliest)
        out = []
        for c in self.capacity:
            pickup = date.fromisoformat(c["pickup_date"])
            if c["lane"] == lane and pickup >= day:
                eta = pickup + timedelta(days=int(c["transit_days"]))
                out.append({**c, "eta_date": eta.isoformat()})
        return sorted(out, key=lambda o: (o["eta_date"], o["rate_eur"], o["carrier_id"]))

    def invoices_for(self, shipment_id: str) -> list[dict[str, Any]]:
        self.shipment(shipment_id)
        out = []
        for inv in self.invoices.values():
            if inv["shipment_id"] == shipment_id:
                credits = [c for c in self.credits if c["invoice_id"] == inv["id"]]
                out.append({**inv, "credits": credits})
        return sorted(out, key=lambda i: i["id"])

    # ------------------------------------------------------------------ writes

    def rebook(self, key: str, shipment_id: str, carrier_id: str, pickup_date: str) -> dict[str, Any]:
        s = self.shipment(shipment_id)
        option = next(
            (
                c
                for c in self.find_capacity(s["lane"], pickup_date)
                if c["carrier_id"] == carrier_id and c["pickup_date"] == pickup_date
            ),
            None,
        )
        if option is None:
            raise Conflict(f"no capacity with {carrier_id} on {pickup_date} for {s['lane']}")

        def apply() -> dict[str, Any]:
            record = {
                "rebooking_id": f"RB-{len(self.rebookings) + 1:04d}",
                "shipment_id": shipment_id,
                "from_carrier": s["carrier_id"],
                "to_carrier": carrier_id,
                "pickup_date": pickup_date,
                "new_eta": option["eta_date"],
                "rate_eur": option["rate_eur"],
            }
            self.rebookings.append(record)
            self.shipments[shipment_id].update(carrier_id=carrier_id, eta_date=option["eta_date"])
            return record

        return self._apply_once(key, apply, "rebook_shipment")

    def issue_credit(self, key: str, invoice_id: str, amount_eur: float, reason: str) -> dict[str, Any]:
        inv = self.invoices.get(invoice_id)
        if inv is None:
            raise NotFound(f"invoice {invoice_id} not found")
        already = sum(c["amount_eur"] for c in self.credits if c["invoice_id"] == invoice_id)
        if key not in self._idempotent and already + amount_eur > inv["amount_eur"]:
            raise Conflict("credit would exceed the invoice amount")

        def apply() -> dict[str, Any]:
            record = {
                "credit_id": f"CN-{len(self.credits) + 1:04d}",
                "invoice_id": invoice_id,
                "amount_eur": amount_eur,
                "reason": reason,
            }
            self.credits.append(record)
            return record

        return self._apply_once(key, apply, "issue_credit")

    def send_message(self, key: str, customer_id: str, ticket_id: str, body: str) -> dict[str, Any]:
        self.customer(customer_id)

        def apply() -> dict[str, Any]:
            record = {
                "message_id": f"MSG-{len(self.messages) + 1:04d}",
                "customer_id": customer_id,
                "ticket_id": ticket_id,
                "body": body,
            }
            self.messages.append(record)
            return record

        return self._apply_once(key, apply, "send_customer_message")

    def create_incident(self, key: str, kind: str, shipment_id: str, severity: str, summary: str) -> dict[str, Any]:
        self.shipment(shipment_id)

        def apply() -> dict[str, Any]:
            record = {
                "incident_id": f"INC-{len(self.incidents) + 1:04d}",
                "kind": kind,
                "shipment_id": shipment_id,
                "severity": severity,
                "summary": summary,
            }
            self.incidents.append(record)
            return record

        return self._apply_once(key, apply, "create_incident")

    def escalate(self, key: str, ticket_id: str, queue: str, reason: str) -> dict[str, Any]:
        def apply() -> dict[str, Any]:
            record = {
                "escalation_id": f"ESC-{len(self.escalations) + 1:04d}",
                "ticket_id": ticket_id,
                "queue": queue,
                "reason": reason,
            }
            self.escalations.append(record)
            return record

        return self._apply_once(key, apply, "escalate")
