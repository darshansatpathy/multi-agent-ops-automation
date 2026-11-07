"""Typed tools. Arguments are validated strictly before anything runs.

Each tool declares an argument model (unknown fields rejected, no type coercion), a kind
(read or write), and a handler against the backend. The JSON schema of the argument
model is what a model sees for function calling, so the contract is the same for the
rule-based brain, a model, and the tests.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from opsagents.backend import OpsBackend

SHIPMENT_ID = r"^AF-[0-9]{5}$"
CUSTOMER_ID = r"^C-[0-9]{4}$"
INVOICE_ID = r"^INV-[0-9]{4}(-[A-Z])?$"
DATE = r"^20[0-9]{2}-[01][0-9]-[0-3][0-9]$"


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ShipmentArgs(_Args):
    shipment_id: str = Field(pattern=SHIPMENT_ID)


class CustomerArgs(_Args):
    customer_id: str = Field(pattern=CUSTOMER_ID)


class CapacityArgs(_Args):
    lane: str = Field(pattern=r"^[A-Z]{5}-[A-Z]{5}$")
    earliest_pickup: str = Field(pattern=DATE)


class RebookArgs(_Args):
    shipment_id: str = Field(pattern=SHIPMENT_ID)
    carrier_id: str = Field(pattern=r"^CR-[0-9]{2}$")
    pickup_date: str = Field(pattern=DATE)


class CreditArgs(_Args):
    invoice_id: str = Field(pattern=INVOICE_ID)
    amount_eur: float = Field(gt=0, le=1_000_000)
    reason: Literal["sla_breach", "duplicate_invoice"]


class MessageArgs(_Args):
    customer_id: str = Field(pattern=CUSTOMER_ID)
    ticket_id: str = Field(pattern=r"^T-[0-9]{4,8}$")
    body: str = Field(min_length=20, max_length=4000)


class IncidentArgs(_Args):
    kind: Literal["damage_claim", "customs_hold", "carrier_failure"]
    shipment_id: str = Field(pattern=SHIPMENT_ID)
    severity: Literal["low", "medium", "high"]
    summary: str = Field(min_length=5, max_length=500)


class EscalateArgs(_Args):
    ticket_id: str = Field(pattern=r"^T-[0-9]{4,8}$")
    queue: Literal["customer_service", "billing", "claims", "security"]
    reason: str = Field(min_length=5, max_length=500)


Handler = Callable[[OpsBackend, str, Any], Any]


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[_Args]
    kind: Literal["read", "write"]
    handler: Handler

    def json_schema(self) -> dict[str, Any]:
        schema = self.args_model.model_json_schema()
        schema.pop("title", None)
        return {
            "type": "function",
            "function": {"name": self.name, "description": self.description, "parameters": schema},
        }


def _registry() -> dict[str, ToolSpec]:
    specs = [
        ToolSpec(
            "get_customer",
            "Look up a customer's name, tier and country.",
            CustomerArgs,
            "read",
            lambda b, _k, a: b.customer(a.customer_id),
        ),
        ToolSpec(
            "get_shipment",
            "Look up a shipment: lane, carrier, status, promised and expected dates.",
            ShipmentArgs,
            "read",
            lambda b, _k, a: b.shipment(a.shipment_id),
        ),
        ToolSpec(
            "get_tracking",
            "List tracking events for a shipment.",
            ShipmentArgs,
            "read",
            lambda b, _k, a: b.tracking(a.shipment_id),
        ),
        ToolSpec(
            "check_sla",
            "Compare a shipment's expected date with its promised date.",
            ShipmentArgs,
            "read",
            lambda b, _k, a: b.sla(a.shipment_id),
        ),
        ToolSpec(
            "find_capacity",
            "Find spare carrier capacity on a lane from a pickup date.",
            CapacityArgs,
            "read",
            lambda b, _k, a: b.find_capacity(a.lane, a.earliest_pickup),
        ),
        ToolSpec(
            "get_invoices",
            "List invoices for a shipment, with credits already issued.",
            ShipmentArgs,
            "read",
            lambda b, _k, a: b.invoices_for(a.shipment_id),
        ),
        ToolSpec(
            "rebook_shipment",
            "Move a delayed shipment to another carrier's capacity.",
            RebookArgs,
            "write",
            lambda b, k, a: b.rebook(k, a.shipment_id, a.carrier_id, a.pickup_date),
        ),
        ToolSpec(
            "issue_credit",
            "Issue a credit note against an invoice.",
            CreditArgs,
            "write",
            lambda b, k, a: b.issue_credit(k, a.invoice_id, a.amount_eur, a.reason),
        ),
        ToolSpec(
            "send_customer_message",
            "Send the reply to the customer who raised the ticket.",
            MessageArgs,
            "write",
            lambda b, k, a: b.send_message(k, a.customer_id, a.ticket_id, a.body),
        ),
        ToolSpec(
            "create_incident",
            "Open an operations incident (claims, customs, carrier failure).",
            IncidentArgs,
            "write",
            lambda b, k, a: b.create_incident(k, a.kind, a.shipment_id, a.severity, a.summary),
        ),
        ToolSpec(
            "escalate",
            "Hand the ticket to a human team.",
            EscalateArgs,
            "write",
            lambda b, k, a: b.escalate(k, a.ticket_id, a.queue, a.reason),
        ),
    ]
    return {s.name: s for s in specs}


TOOLS: dict[str, ToolSpec] = _registry()
WRITE_TOOLS = frozenset(n for n, s in TOOLS.items() if s.kind == "write")
