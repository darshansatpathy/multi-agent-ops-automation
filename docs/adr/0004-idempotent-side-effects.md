# ADR-0004: Exactly-once side effects by business key

**Status:** accepted

## Context

Side effects get repeated in ordinary operation: webhooks retry, clients retry, LangGraph
re-runs nodes on resume, and a process can die after a write reached the backend but
before anything recorded it.

## Decision

Three layers:

1. **Ticket dedupe.** A ticket id already seen returns its existing run.
2. **Idempotency keys.** A write's key is `sha256(ticket id, tool, canonical arguments)`.
   The ledger returns the stored result for a key that already succeeded. Keys use the
   ticket, not the run, so a new run for the same ticket cannot repeat a write.
3. **Reconciliation.** When the ledger has no record, the executor asks the backend
   whether it already applied the key, as payment and booking APIs allow, and records
   that result instead of sending the write again. Guards are not re-run then: they would
   judge the write against the state it created.

## Consequences

- The evaluation includes duplicate delivery and crashes after a credit, a rebooking and
  a reply; each produces exactly one effect.
- Real backends must accept an idempotency key; most payment, booking and messaging APIs
  do. Where one doesn't, reconciliation needs a lookup by business reference.
