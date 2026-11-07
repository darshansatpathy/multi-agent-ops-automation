"""Plain data types shared by the executor, the agents and the evaluation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

Status = Literal["running", "awaiting_approval", "resolved", "escalated", "failed"]


class Ticket(BaseModel):
    """An inbound request. `customer_id` comes from the authenticated channel, never the body."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ticket_id: str = Field(pattern=r"^T-[0-9]{4,8}$")
    customer_id: str = Field(pattern=r"^C-[0-9]{4}$")
    channel: Literal["email", "portal", "chat"] = "portal"
    subject: str = Field(max_length=200)
    body: str = Field(max_length=4000)


def canonical(obj: Any) -> str:
    """Canonical JSON: sorted keys, no whitespace. Same arguments, same bytes."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def digest(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


@dataclass(frozen=True)
class ToolCall:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"type": "tool_call", "tool": self.tool, "args": self.args}


@dataclass(frozen=True)
class Handoff:
    """A worker finishing its visit: what it learned and how it ended."""

    outcome: str  # e.g. "done", "escalate", "no_action"
    summary: str
    facts: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"type": "handoff", "outcome": self.outcome, "summary": self.summary, "facts": self.facts}


Action = ToolCall | Handoff


def action_from_dict(data: dict[str, Any]) -> Action:
    if data.get("type") == "tool_call":
        return ToolCall(str(data["tool"]), dict(data.get("args") or {}))
    return Handoff(str(data["outcome"]), str(data.get("summary", "")), dict(data.get("facts") or {}))


@dataclass
class ToolResult:
    ok: bool
    data: Any = None
    error: str | None = None
    error_kind: str | None = None  # validation | permission | policy | not_found | transient | budget
    cached: bool = False
    call_id: str = ""
    attempts: int = 0

    def observation(self) -> dict[str, Any]:
        """What the brain sees. Errors are explicit, so a model can't mistake one for data."""
        if self.ok:
            return {"ok": True, "data": self.data}
        return {"ok": False, "error": self.error, "error_kind": self.error_kind}
