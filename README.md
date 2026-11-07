# Multi-Agent Ops Automation

[![CI](https://github.com/darshansatpathy/multi-agent-ops-automation/actions/workflows/ci.yml/badge.svg)](https://github.com/darshansatpathy/multi-agent-ops-automation/actions/workflows/ci.yml)
[![CodeQL](https://github.com/darshansatpathy/multi-agent-ops-automation/actions/workflows/codeql.yml/badge.svg)](https://github.com/darshansatpathy/multi-agent-ops-automation/actions/workflows/codeql.yml)
![Python](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)
![LangGraph](https://img.shields.io/badge/LangGraph-1.x-informational)
![Policy violations](https://img.shields.io/badge/policy%20violations-0-success)
![License](https://img.shields.io/badge/license-MIT-green)

**Supervisor and worker agents that handle freight operations tickets, where the
agents decide and a deterministic executor decides whether the decision may happen.**

Customer tickets come in: "where is my shipment", "we want the SLA credit", "we were
charged twice", "the pallets arrived crushed". A supervisor routes each ticket through
triage, shipment, billing and comms workers built on LangGraph. They look things up,
rebook delayed loads, issue credits, open claims and reply to the customer.

The interesting part is what sits between an agent's decision and a real side effect:

- **Every tool call goes through one executor.** It checks the worker's tool scope,
  validates arguments against a strict schema (no coercion, no unknown fields), checks
  the customer owns the shipment or invoice, enforces the tool budget, applies policy
  (credit caps, rebooking rules, message content rules), retries transient failures and
  records the outcome in a ledger.
- **Credits above 250 EUR wait for a person.** The graph pauses with a LangGraph
  interrupt, the run is checkpointed, and an approver resumes it, even after the service
  restarts. The approval is bound to the exact credit; a different amount needs a new one.
- **Nothing happens twice.** Writes carry an idempotency key derived from the ticket and
  the canonical arguments. A webhook delivering the same ticket twice, a retry, or a
  process dying between a write and its record all leave exactly one credit.
- **A misbehaving model is contained.** The evaluation runs every scenario twice: once
  with the reference brain, once with a brain that tries 413 unsafe actions across them.
  None of those reach the systems of record, and every ticket still resolves.

```
ticket ─▶ supervisor ─┬─▶ triage ───┐
                      ├─▶ shipment ─┤      every tool call:
                      ├─▶ billing ──┤ ─▶   scope → schema → ownership → idempotency
                      ├─▶ comms ────┤      → budget → policy → retry → ledger
                      ├─▶ escalate ─┘
                      └─▶ finish        billing can pause for an approver (interrupt + checkpoint)
```

> Built on a fictional freight company (Aurora Freight) with invented customers,
> shipments and invoices. The rule brain runs fully offline; any OpenAI-compatible model
> can drive the same graph through `OPS_BRAIN=llm`.

---

## Try it

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
opsagents demo
```

```text
### A delayed shipment gets rebooked
  status: resolved
  route: triage -> shipment -> comms
  wrote: rebook_shipment {"shipment_id": "AF-24001", "carrier_id": "CR-02", "pickup_date": "2026-11-02"}
  reply:
    Hello Nordwind Retail BV,
    Shipment AF-24001 was running late, so we have moved it to an earlier departure.
    We now expect delivery on 3 November 2026, on the promised date.

### An SLA credit above the auto limit waits for an approver
  status: awaiting_approval
  waiting for approval: Credit 420.00 EUR on INV-8003 (sla_breach) for C-1003
  approver decides: approve
  status: resolved
  wrote: issue_credit {"invoice_id": "INV-8003", "amount_eur": 420.0, "reason": "sla_breach"}

### A ticket that tries to instruct the agent goes to security
  status: escalated
  wrote: escalate {"ticket_id": "T-1003", "queue": "security", ...}

### A customer asking about someone else's shipment learns nothing
  status: escalated
  blocked: get_shipment (not_found: shipment AF-24001 not found for this customer)
```

The reply to the last one is a plain "we've passed this to customer service". Another
customer's shipment looks exactly like a shipment that doesn't exist.

```bash
opsagents run --customer C-1002 --body "We were charged twice for AF-24008" --approve --trace
opsagents eval --check        # the CI gate
docker compose up             # API on :8080 with PostgreSQL-backed runs
```

## Results

From `opsagents eval` over 42 scenarios ([evals/scenarios.yaml](evals/scenarios.yaml)):
delays and rebooking, SLA credits, approvals granted and denied, damage claims,
duplicate invoices, cross-customer access, prompt injection, out-of-scope requests,
injected backend failures at 35 to 100%, duplicate delivery and simulated crashes. Each
scenario fixes the route, the side effects that must and must not happen, and what the
reply says. An independent auditor then re-checks every side effect against the policy
file and the original data, without using the executor's code.

| | Reference brain | Misbehaving brain |
|---|---|---|
| Scenarios passed | **42 / 42** | **42 / 42** |
| Unsafe actions attempted | 0 | **413** |
| Blocked by the executor | (5: ownership checks and denied approvals) | **413** (not found 147, policy 120, validation 93, scope 51, denied 2) |
| Policy violations found by the auditor | **0** | **0** |
| Unexpected or duplicate side effects | **0** | **0** |
| Transient failures absorbed by retries | 20 | 20 |
| Tool calls per ticket, mean / max | 4.7 / 6 | 4.7 / 6 |

**Read the left column carefully.** The rule brain is the reference implementation, and
the scenarios were written alongside it, so 42/42 shows the system does what its tests
say, not that it is clever. The numbers that carry weight are the right column and the
robustness rows. A brain that inflates credits tenfold, calls tools outside its scope,
rebooks for no reason, reads other customers' shipments, invents tools and drafts replies
full of internal notes still produces zero violations, because none of that is the
brain's decision to make.

For a model, the same gate applies: run `OPS_BRAIN=llm opsagents eval --brain llm --check`
against your endpoint. To keep it in CI without a key, record once with
`OPS_RECORD_CASSETTE=...` and replay with `OPS_REPLAY_CASSETTE=...`; the replay fails
loudly if a prompt, tool schema or state changes. No recording ships with this repo.

## How it works

| Concern | Mechanism | Where |
|---|---|---|
| Orchestration | LangGraph `StateGraph`: supervisor routes, workers are bounded decide-execute loops | [graph.py](src/opsagents/graph.py) |
| Who may do what | Tool scopes per worker from the policy file; supervisor only escalates | [ops-policy.yaml](policies/ops-policy.yaml) |
| Argument safety | Pydantic models, `strict=True`, `extra="forbid"`; the same schema is the model's function definition | [tools.py](src/opsagents/tools.py) |
| Object-level authorisation | Shipment, invoice and customer ids must belong to the ticket's customer; otherwise "not found" | [executor.py](src/opsagents/executor.py) |
| Money | SLA credit = tier rate × freight cost, and the executor caps any credit at that; auto ≤ 250 EUR, approval ≤ 2,000 EUR, never above | [executor.py](src/opsagents/executor.py) |
| Human approval | `interrupt()` with the exact request; resume with `Command(resume=...)`; approval bound to the call's fingerprint | [ADR-0003](docs/adr/0003-approvals-as-interrupts.md) |
| Exactly-once effects | Idempotency key = hash(ticket, tool, canonical args); ledger first, then the backend's own record after a crash | [ADR-0004](docs/adr/0004-idempotent-side-effects.md) |
| Deterministic replay | Each decision stored under (run, worker, visit, step); a re-run node replays instead of asking again | [ADR-0002](docs/adr/0002-deterministic-executor.md) |
| Replies | Built from established facts only; checked for ticket reference, internal wording, other customers' data, emails | [rule.py](src/opsagents/brains/rule.py) |
| Durability | Checkpoints and ledger in memory, SQLite or PostgreSQL; a paused run survives a restart | [config.py](src/opsagents/config.py) |
| Budgets | Steps per ticket, tool calls per ticket, actions per worker visit | [ops-policy.yaml](policies/ops-policy.yaml) |

Design notes: [architecture](docs/architecture.md), [threat model](docs/threat-model.md)
(mapped to the OWASP Top 10 for LLM applications), and the decision records in
[docs/adr](docs/adr).

## HTTP API

```bash
opsagents serve --port 8080
curl -s localhost:8080/v1/tickets -H 'content-type: application/json' -H 'X-Customer-Id: C-1003' \
  -d '{"ticket_id": "T-4242", "subject": "Credit", "body": "AF-24003 is two days late. We expect the SLA credit."}'
# {"run_id": "run-…", "status": "awaiting_approval", "duplicate": false}

curl -s localhost:8080/v1/runs/run-…/approval -H 'content-type: application/json' \
  -H 'X-User-Id: u-anna' -H 'X-User-Groups: ops-staff,ops-approvers' -d '{"approved": true}'
```

| Endpoint | Who | Purpose |
|---|---|---|
| `POST /v1/tickets` | customer channel (`X-Customer-Id`) | Submit a ticket; returns only the run id and status; a repeated ticket id returns the same run |
| `GET /v1/runs/{id}` | `ops-staff` | Full state: facts, trace, pending approval, ledger |
| `POST /v1/runs/{id}/approval` | `ops-approvers` | Approve or deny the pending action |
| `GET /healthz`, `GET /metrics` | probes | Health; Prometheus counters for runs, blocked actions by kind, approvals |

Identities come from the authenticating gateway in front of the service. With
`OPS_SERVICE_TOKEN` set, only callers presenting it can assert them. CI starts the
container with PostgreSQL, pauses a ticket for approval, restarts the API, and approves
it.

## Configuration

| Variable | Values |
|---|---|
| `OPS_BRAIN` | `rule` (default), `llm` (`OPS_LLM_BASE_URL`, `OPS_LLM_MODEL`, `OPS_LLM_API_KEY`, Azure supported) |
| `OPS_STORE` | `memory` (default), `sqlite` (`OPS_SQLITE_PATH`), `postgres` (`OPS_DATABASE_URL`) |
| `OPS_RECORD_CASSETTE`, `OPS_REPLAY_CASSETTE` | record a model's replies, or replay them |
| `OPS_POLICY`, `OPS_SEED` | policy file and data (defaults bundled) |
| `OPS_SERVICE_TOKEN`, `OPS_APPROVER_GROUP`, `OPS_STAFF_GROUP` | API access |

All of them are in [.env.example](.env.example).

## Engineering

- 90 tests, 98% line coverage (gate: 90%), including PostgreSQL checkpoints and ledger
  (a service container in CI) and a SQLite restart test.
- `ruff`, `mypy --strict`, Python 3.11 to 3.13.
- The evaluation gate runs both brains on every push and writes the report to the run
  summary.
- The seed data is generated by [scripts/generate_seed.py](scripts/generate_seed.py) and
  CI checks the committed file matches it.
- CodeQL (security-extended), pip-audit, Dependabot.

## Layout

```
src/opsagents/
  graph.py         LangGraph supervisor/worker graph, approvals, recovery
  executor.py      the deterministic tool executor
  tools.py         typed tool definitions
  ledger.py        decisions, tool calls, approvals (memory, SQLite, PostgreSQL)
  policy.py        policy file loader
  backend.py       simulated TMS, CRM and ERP with idempotent writes and fault injection
  brains/          rule (reference), llm (function calling), rogue (guardrail test)
  providers.py     OpenAI-compatible client, record and replay
  evaluation.py    scenario runner, metrics, gate
  auditor.py       independent policy auditor
  api.py, cli.py, config.py, observability.py
evals/             scenarios and thresholds
policies/          operating policy
data/              generated seed data
```

## Known limits

- The backend is a simulation held in memory. Checkpoints and the ledger are durable;
  the "systems of record" reset on restart. In a deployment they are your TMS, CRM and
  ERP, which is why every write carries an idempotency key they can honour.
- Intent classification in the rule brain is keyword-based. It is a reference, not a
  classifier; the model brain is the intended production path, held to the same gate.
- One ticket, one shipment. Tickets naming several shipments are escalated.

## License

MIT. See [LICENSE](LICENSE).
