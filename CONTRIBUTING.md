# Contributing

## Set up

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"
make check
```

`make check` runs lint, formatting, strict type checks, the tests with a coverage gate,
and the evaluation gate. PostgreSQL tests run when `OPS_TEST_DATABASE_URL` is set
(`make postgres-test` starts one with Docker).

## Changing agents, tools or policy

1. Add or change scenarios in `evals/scenarios.yaml` first: the route, the side effects
   that must and must not happen, and what the reply says. Include a case that must be
   refused.
2. If the change adds a tool or a write, add a guard in the executor, a check in the
   auditor, and an attempt to misuse it in the rogue brain.
3. Run `opsagents eval --report before.md` on `main` and `--report after.md` on your
   branch, and put both in the pull request.

Do not relax a threshold in `evals/thresholds.yaml` to make a change pass. The safety
limits are zero and stay zero.

## Seed data

Edit `scripts/generate_seed.py`, run `make seed`, commit the regenerated file. CI fails
if the committed data differs from the generator's output.

## Commits

Small commits with a message that says what changed and why. Update `CHANGELOG.md` under
an "Unreleased" heading.
