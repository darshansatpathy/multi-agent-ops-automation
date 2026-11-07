# Security policy

## Reporting a vulnerability

Please do not open a public issue for security problems. Use GitHub's
[private vulnerability reporting](https://github.com/darshansatpathy/multi-agent-ops-automation/security/advisories/new)
for this repository. You should get an acknowledgement within three working days.

## Scope

This project is a reference implementation. The [threat model](docs/threat-model.md)
lists what it is designed to defend against and what is deliberately out of scope.

## Operating it safely

- Put the service behind a gateway that authenticates customers and staff, and set
  `OPS_SERVICE_TOKEN` so nothing else can assert an identity or an approver group.
- Keep the approver group small, and review `approval` log lines and the ledger's
  approval records regularly.
- Use `OPS_STORE=postgres` in any deployment, so paused and crashed runs survive restarts.
- Watch `ops_blocked_actions_total`: a rise after a model or prompt change means the model
  is proposing actions the policy refuses.
- Keep model API keys in a secret manager, never in a committed `.env`.
