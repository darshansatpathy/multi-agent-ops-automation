# ADR-0002: Agents decide, a deterministic executor enforces

**Status:** accepted

## Context

A model can propose any tool call with any arguments. Prompts that say "never credit more
than 250 EUR without approval" work most of the time. For money, customer data and carrier
bookings, most of the time is not the bar.

## Decision

Every tool call, from every brain, goes through `ToolExecutor`: scope, strict schema,
ownership, idempotency, budget, policy guard, bounded retries, ledger entry, in that order.
Brains are not trusted; they produce `ToolCall` and `Handoff` values and nothing else.

Each decision is stored under `(run, worker, visit, step)` before it runs. When a node
re-runs (after an interrupt, or recovery from a crash), it replays stored decisions, and
reads come back from the per-run cache, so the node reaches the same point with the same
observations.

## Consequences

- The policy is enforced in one place, independent of prompts and models.
- Swapping the rule brain for a model changes quality, not safety: the evaluation shows a
  brain attempting 413 unsafe actions producing zero violations.
- The rule brain duplicates some policy logic so it can behave well; the executor's
  checks are the ones that matter, and the auditor checks both (ADR-0005).
