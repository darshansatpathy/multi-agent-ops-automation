"""Brains decide; the executor enforces. Both brains implement the same two methods."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from opsagents.models import Action, Ticket

WORKERS = ("triage", "shipment", "billing", "comms")
ROUTES = (*WORKERS, "escalate", "finish")


@dataclass(frozen=True)
class View:
    """What a brain is shown: the ticket, what is known so far, and where the run is."""

    ticket: Ticket
    as_of: str
    facts: dict[str, Any]
    done: tuple[str, ...]
    outcomes: tuple[str, ...] = ()
    tools: tuple[str, ...] = ()
    extra: dict[str, Any] = field(default_factory=dict)


History = list[tuple[Action, dict[str, Any]]]


class Brain(Protocol):
    name: str

    def route(self, view: View) -> tuple[str, str]:
        """Next step (a worker, "escalate" or "finish") and a one-line reason."""
        ...

    def act(self, worker: str, view: View, history: History) -> Action:
        """The worker's next action given what it has done during this visit."""
        ...
