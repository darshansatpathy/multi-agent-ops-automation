"""Scenario evaluation and the CI gate.

Each scenario runs on a fresh copy of the seed data with its own ledger and engine,
optionally with injected faults, an approver's decision, a duplicate delivery or a
simulated crash. A scenario passes when every expectation holds and the independent
auditor finds no violation.
"""

from __future__ import annotations

import statistics
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from opsagents.auditor import Violation, audit
from opsagents.backend import Crash, FaultPlan, OpsBackend, default_seed_path
from opsagents.brains import Brain
from opsagents.brains.rule import RuleBrain
from opsagents.executor import ToolExecutor
from opsagents.graph import OpsEngine, RunResult, make_engine
from opsagents.ledger import InMemoryLedger
from opsagents.models import Ticket
from opsagents.policy import Policy, load_policy
from opsagents.tools import WRITE_TOOLS

_PACKAGED = Path(__file__).resolve().parent / "evals"
_REPO = Path(__file__).resolve().parents[2] / "evals"
EVAL_DIR = _PACKAGED if _PACKAGED.exists() else _REPO
BrainFactory = Callable[[Policy], Brain]
_ALWAYS = {"send_customer_message", "escalate"}


def load_scenarios(path: str | Path | None = None) -> list[dict[str, Any]]:
    data = yaml.safe_load(Path(path or EVAL_DIR / "scenarios.yaml").read_text(encoding="utf-8"))
    scenarios: list[dict[str, Any]] = data["scenarios"]
    ids = [s["id"] for s in scenarios]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate scenario ids")
    return scenarios


def load_thresholds(path: str | Path | None = None) -> dict[str, float]:
    data = yaml.safe_load(Path(path or EVAL_DIR / "thresholds.yaml").read_text(encoding="utf-8"))
    return {str(k): float(v) for k, v in data.items()}


@dataclass
class ScenarioResult:
    sid: str
    category: str
    passed: bool
    status: str
    route: list[str]
    failures: list[str]
    violations: list[Violation]
    tool_calls: int
    retries: int
    steps: int
    unexpected_writes: list[str]
    duplicate_writes: int
    message: str = ""
    blocked: dict[str, int] = field(default_factory=dict)


@dataclass
class EvalReport:
    brain: str
    results: list[ScenarioResult] = field(default_factory=list)

    def summary(self) -> dict[str, float]:
        n = len(self.results) or 1
        calls = [r.tool_calls for r in self.results]
        return {
            "scenarios": float(len(self.results)),
            "task_success": sum(r.passed for r in self.results) / n,
            "route_accuracy": sum(not any(f.startswith("route") for f in r.failures) for r in self.results) / n,
            "policy_violations": float(sum(len(r.violations) for r in self.results)),
            "unexpected_writes": float(sum(len(r.unexpected_writes) for r in self.results)),
            "duplicate_writes": float(sum(r.duplicate_writes for r in self.results)),
            "mean_tool_calls": statistics.fmean(calls) if calls else 0.0,
            "max_tool_calls": float(max(calls, default=0)),
            "retries_absorbed": float(sum(r.retries for r in self.results)),
            "blocked_actions": float(sum(sum(r.blocked.values()) for r in self.results)),
        }

    def by_category(self) -> dict[str, tuple[int, int]]:
        acc: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for r in self.results:
            acc[r.category][0] += r.passed
            acc[r.category][1] += 1
        return {k: (v[0], v[1]) for k, v in sorted(acc.items())}

    def check(self, thresholds: dict[str, float]) -> list[str]:
        s = self.summary()
        failures = []
        for name, limit in thresholds.items():
            key = name[4:]
            value = s.get(key, 0.0)
            if name.startswith("max_") and value > limit:
                failures.append(f"{key} = {value:g} exceeds {limit:g}")
            if name.startswith("min_") and value < limit:
                failures.append(f"{key} = {value:.3f} below {limit:g}")
        return failures

    def markdown(self) -> str:
        s = self.summary()
        lines = [
            f"### Agent evaluation ({self.brain} brain, {int(s['scenarios'])} scenarios)",
            "",
            "| Metric | Value |",
            "|---|---|",
            f"| Task success (every expectation met, no violation) | {s['task_success']:.0%} |",
            f"| Route accuracy | {s['route_accuracy']:.0%} |",
            f"| Policy violations found by the independent auditor | {int(s['policy_violations'])} |",
            f"| Unexpected side effects | {int(s['unexpected_writes'])} |",
            f"| Duplicate side effects (retries, crashes, resubmits) | {int(s['duplicate_writes'])} |",
            f"| Transient failures absorbed by retries | {int(s['retries_absorbed'])} |",
            f"| Tool calls per ticket, mean / max | {s['mean_tool_calls']:.1f} / {int(s['max_tool_calls'])} |",
        ]
        if s["blocked_actions"]:
            kinds = Counter[str]()
            for r in self.results:
                kinds.update(r.blocked)
            detail = ", ".join(f"{k} {v}" for k, v in kinds.most_common())
            lines.append(f"| Actions blocked by the executor | {int(s['blocked_actions'])} ({detail}) |")
        lines += ["", "| Category | Passed |", "|---|---|"]
        for cat, (ok, total) in self.by_category().items():
            lines.append(f"| {cat} | {ok}/{total} |")
        failed = [r for r in self.results if not r.passed]
        if failed:
            lines += ["", "<details><summary>Failed scenarios</summary>", "", "| Id | Problems |", "|---|---|"]
            for r in failed:
                problems = "; ".join([*r.failures, *(f"{v.rule}: {v.detail}" for v in r.violations)])
                lines.append(f"| {r.sid} | {problems.replace('|', '/')} |")
            lines += ["", "</details>"]
        return "\n".join(lines)


def _route(result: RunResult) -> list[str]:
    return [str(t["next"]) for t in result.trace if t.get("step") == "route" and t.get("next") != "finish"]


def _drive(engine: OpsEngine, run_id: str, start: Callable[[], RunResult], approval: str | None) -> RunResult:
    """Run to completion: recover from simulated crashes, answer approvals when told how."""
    mode = "start"
    for _ in range(10):
        try:
            if mode == "start":
                result = start()
            elif mode == "recover":
                result = engine.recover(run_id)
            else:
                result = engine.resume(
                    run_id, approved=approval == "approve", approver="eval-approver", note="evaluation"
                )
        except Crash:
            mode = "recover"
            continue
        if result.status == "awaiting_approval" and approval in {"approve", "deny"}:
            mode = "resume"
            continue
        return result
    raise RuntimeError(f"run {run_id} did not settle")


def run_scenario(
    scenario: dict[str, Any],
    index: int,
    policy: Policy,
    brain_factory: BrainFactory,
    seed_path: Path | None = None,
) -> ScenarioResult:
    faults = FaultPlan(**(scenario.get("faults") or {}))
    backend = OpsBackend.from_file(seed_path, faults=faults)
    seed = backend.seed
    ledger = InMemoryLedger()
    executor = ToolExecutor(backend, policy, ledger, sleep=lambda _s: None)
    engine = make_engine(backend, executor, brain_factory(policy))
    ticket = Ticket(ticket_id=f"T-{9001 + index}", **scenario["ticket"])
    run_id = f"eval-{scenario['id']}"
    approval = scenario.get("approval")
    result = _drive(engine, run_id, lambda: engine.submit(ticket, run_id=run_id), approval)
    writes_before = backend.write_count
    duplicates = 0
    if scenario.get("resubmit"):
        again = engine.submit(ticket)
        duplicates = backend.write_count - writes_before
        if not again.duplicate or again.run_id != run_id:
            duplicates += 1

    exp = scenario["expect"]
    failures: list[str] = []
    route = _route(result)
    if route != list(exp["route"]):
        failures.append(f"route {route} != {exp['route']}")
    if result.status != exp["status"]:
        failures.append(f"status {result.status} != {exp['status']}")
    entries = ledger.entries()
    ok_writes = [e.tool for e in entries if e.kind == "write" and e.status == "ok"]
    for tool in exp.get("writes", []):
        if tool not in ok_writes:
            failures.append(f"missing write {tool}")
    allowed = set(exp.get("writes", [])) | _ALWAYS
    unexpected = [t for t in ok_writes if t not in allowed]
    issued = sum(float(c["amount_eur"]) for c in backend.credits[len(seed.get("prior_credits", [])) :])
    if abs(issued - float(exp.get("credit_eur", 0))) > 0.005:
        failures.append(f"credit {issued:.2f} != {float(exp.get('credit_eur', 0)):.2f}")
    asked = any(e.status == "approval_required" for e in entries)
    if asked != bool(exp.get("approval", False)):
        failures.append(f"approval requested = {asked}")
    if exp.get("queue"):
        queues = [e.args.get("queue") for e in entries if e.tool == "escalate" and e.status == "ok"]
        if exp["queue"] not in queues:
            failures.append(f"escalation queue {queues} != {exp['queue']}")
    message = backend.messages[-1]["body"] if backend.messages else ""
    for text in exp.get("says", []):
        if text not in message:
            failures.append(f"reply lacks {text!r}")
    for text in exp.get("never_says", []):
        if text in message:
            failures.append(f"reply contains {text!r}")
    if len(backend.messages) > 1:
        failures.append(f"{len(backend.messages)} replies sent")
    violations = audit(policy, OpsBackend.from_file(seed_path).seed, backend, ledger, ticket.model_dump())
    calls = sum(1 for e in entries if e.status in {"ok", "error"})
    retries = sum(max(0, e.attempts - 1) for e in entries)
    steps = sum(1 for t in result.trace if t.get("step") == "route")
    passed = not failures and not violations and not unexpected and duplicates == 0
    blocked = Counter(str(e.error_kind) for e in entries if e.status == "rejected")
    return ScenarioResult(
        scenario["id"],
        scenario.get("category", "other"),
        passed,
        result.status,
        route,
        failures,
        violations,
        calls,
        retries,
        steps,
        unexpected,
        duplicates,
        message,
        dict(blocked),
    )


def evaluate(
    scenarios: Sequence[dict[str, Any]] | None = None,
    policy: Policy | None = None,
    brain_factory: BrainFactory | None = None,
    seed_path: Path | None = None,
) -> EvalReport:
    policy = policy or load_policy()
    factory: BrainFactory = brain_factory or RuleBrain
    items = list(scenarios or load_scenarios())
    report = EvalReport(brain=factory(policy).name)
    for i, scenario in enumerate(items):
        report.results.append(run_scenario(scenario, i, policy, factory, seed_path or default_seed_path()))
    return report


def write_tool_counts(report: EvalReport) -> Counter[str]:
    return Counter(w for r in report.results for w in r.unexpected_writes if w in WRITE_TOOLS)


def evaluate_suite(policy: Policy | None = None) -> tuple[EvalReport, EvalReport]:
    """The rule brain (the reference) and the rogue brain (the guardrail test)."""
    from opsagents.brains.rogue import RogueBrain

    policy = policy or load_policy()
    return evaluate(policy=policy), evaluate(policy=policy, brain_factory=RogueBrain)
