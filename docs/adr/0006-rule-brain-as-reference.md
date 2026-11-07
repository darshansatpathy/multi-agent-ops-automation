# ADR-0006: A rule-based reference brain, with the model brain held to the same gate

**Status:** accepted

## Context

A model-driven demo needs an API key to run and gives different answers run to run, which
makes it a poor CI fixture and a poor baseline.

## Decision

Ship a rule brain that follows the policy and runs offline, and a model brain behind the
same interface (function calling over any OpenAI-compatible endpoint, temperature 0, fixed
seed). The evaluation can run either. For CI with a model, record a run's replies to a
cassette and replay them; the replay is keyed by a hash of the exact request, so a change
in prompts, tools or state fails loudly instead of drifting.

## Consequences

- The repository runs, tests and evaluates without secrets.
- The rule brain's 42/42 shows the system matches its scenarios. It is not evidence about
  model quality; the README says so.
- No model recording ships, because none was made for this repository; the mechanism is
  tested with scripted replies.
