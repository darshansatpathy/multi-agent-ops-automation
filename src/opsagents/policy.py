"""The operating policy, loaded from YAML into typed settings."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

_PACKAGED = Path(__file__).resolve().parent / "policies" / "ops-policy.yaml"
_REPO = Path(__file__).resolve().parents[2] / "policies" / "ops-policy.yaml"


def default_policy_path() -> Path:
    return _PACKAGED if _PACKAGED.exists() else _REPO


class PolicyError(ValueError):
    pass


@dataclass(frozen=True)
class Policy:
    sla_breach_pct: dict[str, float]
    auto_approve_max_eur: float
    approval_max_eur: float
    one_credit_per_invoice_reason: bool
    rebook_only_when_at_risk: bool
    max_cost_increase_pct: float
    must_arrive_earlier: bool
    must_reference_ticket: bool
    forbidden_terms: tuple[str, ...]
    max_message_chars: int
    max_steps: int
    max_tool_calls: int
    max_actions_per_visit: int
    worker_tools: dict[str, frozenset[str]] = field(default_factory=dict)

    def credit_for(self, tier: str, freight_cost_eur: float) -> float:
        return round(freight_cost_eur * self.sla_breach_pct.get(tier, 0.0) / 100.0, 2)

    def tools_for(self, worker: str) -> frozenset[str]:
        return self.worker_tools.get(worker, frozenset())


def load_policy(path: str | Path | None = None) -> Policy:
    try:
        raw = yaml.safe_load(Path(path or default_policy_path()).read_text(encoding="utf-8"))
        c, r, m, b = raw["credits"], raw["rebooking"], raw["messages"], raw["budgets"]
        policy = Policy(
            sla_breach_pct={str(k): float(v) for k, v in c["sla_breach_pct"].items()},
            auto_approve_max_eur=float(c["auto_approve_max_eur"]),
            approval_max_eur=float(c["approval_max_eur"]),
            one_credit_per_invoice_reason=bool(c.get("one_credit_per_invoice_reason", True)),
            rebook_only_when_at_risk=bool(r.get("only_when_sla_at_risk", True)),
            max_cost_increase_pct=float(r["max_cost_increase_pct"]),
            must_arrive_earlier=bool(r.get("must_arrive_earlier", True)),
            must_reference_ticket=bool(m.get("must_reference_ticket", True)),
            forbidden_terms=tuple(str(t).lower() for t in m.get("forbidden_terms", [])),
            max_message_chars=int(m.get("max_chars", 1200)),
            max_steps=int(b["max_steps"]),
            max_tool_calls=int(b["max_tool_calls"]),
            max_actions_per_visit=int(b["max_actions_per_visit"]),
            worker_tools={str(w): frozenset(ts) for w, ts in raw["workers"].items()},
        )
    except (KeyError, TypeError, ValueError, yaml.YAMLError, OSError) as exc:
        raise PolicyError(f"invalid policy: {exc}") from exc
    if policy.auto_approve_max_eur > policy.approval_max_eur:
        raise PolicyError("auto_approve_max_eur must not exceed approval_max_eur")
    return policy
