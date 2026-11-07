# ADR-0005: Grade with an independent auditor; test with a misbehaving brain

**Status:** accepted

## Context

If the code that enforces a rule is also the code that checks it, a bug in the rule is
invisible. And a reference brain that follows policy exercises the happy path of the
guards, not the guards themselves.

## Decision

- The **auditor** re-derives every rule from the policy file and the original seed data
  and judges the backend's final state and the ledger: credit amounts, caps, approvals,
  duplicates, rebooking conditions, reply content and recipients, tool scopes, duplicate
  writes. It shares no code with the executor's guards.
- The **rogue brain** tries unsafe actions in every worker before behaving normally:
  inflated and out-of-scope credits, unjustified rebooking, another customer's shipment,
  an invented tool, malformed arguments, replies with internal notes, other customers' data
  and the wrong recipient.
- The gate requires zero violations and zero unexpected or duplicate side effects for
  both brains.

## Consequences

- A guard regression fails CI with a named rule, even if the reference brain never trips it.
- The rogue brain's attacks are fixed, not generated; new failure modes get added as new
  attempts.
