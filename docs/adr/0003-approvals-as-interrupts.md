# ADR-0003: Human approval as a checkpointed interrupt bound to one call

**Status:** accepted

## Context

Some actions should not happen without a person: here, credits above 250 EUR. The
approver may answer in minutes or the next morning, and the service may be redeployed in
between.

## Decision

When the executor sees a credit that needs approval and none is recorded for that exact
call, it raises `ApprovalRequired` with the request. The worker node calls `interrupt()`;
LangGraph checkpoints the run. Staff see the request over the API; an approver in the
`ops-approvers` group resumes it. The decision is stored against the call's fingerprint
(the idempotency key of the credit), so it approves that invoice, amount and reason only.

## Alternatives considered

- **Approval as a separate queue and a new run afterwards.** Loses the run's context and
  needs custom state handling.
- **Ask the model to wait.** Not a control.

## Consequences

- A paused run survives restarts (tested with SQLite in the suite and PostgreSQL in CI).
- The approval request is logged once even though the node re-runs on resume.
- A denied approval reaches the brain as an explicit `denied` error; the reply says no
  credit could be issued.
