# ADR-0001: A LangGraph supervisor/worker graph

**Status:** accepted

## Context

A ticket needs several kinds of work in an order that depends on what the ticket is:
classify it, check the shipment, maybe rebook, maybe credit, then reply. A single agent
with every tool is simple to start but hard to bound: its tool list is the union of all
jobs, and nothing stops it doing them in a harmful order.

## Decision

Model the flow as a LangGraph `StateGraph`: a supervisor node routes to worker nodes
(triage, shipment, billing, comms) and back, with escalate and finish as explicit steps.
Each worker has its own tool scope. State is a typed dictionary with reducers, so facts
accumulate and the route is recorded.

## Why LangGraph

- Checkpointing after every node, with memory, SQLite and PostgreSQL savers.
- `interrupt()` and `Command(resume=...)` for human approval, without inventing a
  pause/resume mechanism.
- The graph is explicit and testable; routing is a function of state.

## Consequences

- Routing can be done by rules or a model behind the same interface, and is validated by
  the supervisor either way.
- LangGraph re-runs a node from its start when resuming; that needs deterministic replay
  inside nodes (ADR-0002).
- The dependency is pinned by major version; the graph code is under 350 lines and does
  not use LangChain abstractions beyond LangGraph itself.
