# Changelog

All notable changes are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-11-07

### Added

- LangGraph supervisor/worker graph: triage, shipment, billing and comms workers,
  escalation and finish steps, step and action budgets.
- Deterministic tool executor: per-worker scopes, strict schemas, ownership checks,
  idempotency keys, tool budget, policy guards, bounded retries, ledger.
- Human approval for credits above the auto limit, as a checkpointed interrupt bound to
  the exact call.
- Exactly-once side effects across retries, resumes, duplicate tickets and crashes, with
  reconciliation against the backend.
- Rule brain (reference), model brain (function calling, any OpenAI-compatible endpoint)
  and a misbehaving brain for guardrail tests; record and replay of model replies.
- Checkpoints and ledger in memory, SQLite or PostgreSQL.
- Evaluation over 42 scenarios with an independent policy auditor and a CI gate.
- FastAPI service (tickets, run status, approvals, metrics) and `opsagents` CLI.
- Fictional seed data with a reproducible generator; operating policy file.
- CI on Python 3.11 to 3.13 with PostgreSQL, CodeQL, pip-audit, Dependabot, and a
  container test that restarts the API while a run waits for approval.
