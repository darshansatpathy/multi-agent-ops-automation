from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from opsagents.api import create_app
from opsagents.cli import main
from opsagents.config import ConfigError, Settings, build

STAFF = {"X-User-Id": "u-ops", "X-User-Groups": "ops-staff"}
APPROVER = {"X-User-Id": "u-boss", "X-User-Groups": "ops-staff, ops-approvers"}


def _client(token: str | None = None) -> TestClient:
    settings = Settings(service_token=token)
    return TestClient(create_app(build(settings), settings))


@pytest.fixture
def client() -> Iterator[TestClient]:
    c = _client()
    yield c
    c.app.state.wiring.close()  # type: ignore[attr-defined]


def _submit(client: TestClient, body: str, customer: str = "C-1003", ticket_id: str = "T-7001") -> dict[str, object]:
    r = client.post(
        "/v1/tickets", json={"ticket_id": ticket_id, "subject": "s", "body": body}, headers={"X-Customer-Id": customer}
    )
    assert r.status_code == 200, r.text
    return r.json()  # type: ignore[no-any-return]


def test_health_and_metrics(client: TestClient) -> None:
    assert client.get("/healthz").json()["brain"] == "rule"
    _submit(client, "Where is AF-24003?")
    text = client.get("/metrics").text
    assert 'ops_runs_total{status="resolved"}' in text
    assert 'ops_http_requests_total{route="/v1/tickets",status="200"}' in text


def test_customer_sees_outcome_only(client: TestClient) -> None:
    data = _submit(client, "Where is AF-24003?")
    assert set(data) == {"run_id", "status", "duplicate"}
    again = _submit(client, "Where is AF-24003?")
    assert again["duplicate"] is True and again["run_id"] == data["run_id"]


def test_ticket_needs_a_customer(client: TestClient) -> None:
    r = client.post("/v1/tickets", json={"ticket_id": "T-7001", "subject": "s", "body": "b"})
    assert r.status_code == 422


def test_approval_flow_over_http(client: TestClient) -> None:
    data = _submit(client, "AF-24003 is two days late. We expect the SLA credit.")
    run_id = data["run_id"]
    assert data["status"] == "awaiting_approval"
    assert client.get(f"/v1/runs/{run_id}").status_code == 403
    view = client.get(f"/v1/runs/{run_id}", headers=STAFF).json()
    assert "420.00 EUR" in view["pending_approval"]["summary"]
    assert client.post(f"/v1/runs/{run_id}/approval", json={"approved": True}, headers=STAFF).status_code == 403
    done = client.post(f"/v1/runs/{run_id}/approval", json={"approved": True, "note": "fine"}, headers=APPROVER)
    assert done.status_code == 200 and done.json()["status"] == "resolved"
    assert client.post(f"/v1/runs/{run_id}/approval", json={"approved": True}, headers=APPROVER).status_code == 409
    assert client.get("/v1/runs/nope", headers=STAFF).status_code == 404
    assert client.post("/v1/runs/nope/approval", json={"approved": True}, headers=APPROVER).status_code == 404
    assert 'ops_approvals_total{decision="approved"} 1' in client.get("/metrics").text


def test_bad_groups_and_request_ids(client: TestClient) -> None:
    r = client.get("/v1/runs/x", headers={"X-User-Id": "u", "X-User-Groups": "ops staff;drop"})
    assert r.status_code == 400
    assert len(client.get("/healthz", headers={"X-Request-ID": "bad id"}).headers["X-Request-ID"]) == 32
    assert client.get("/healthz", headers={"X-Request-ID": "req-1"}).headers["X-Request-ID"] == "req-1"


def test_service_token() -> None:
    c = _client("s3cret")
    body = {"ticket_id": "T-7001", "subject": "s", "body": "Where is AF-24003?"}
    assert c.post("/v1/tickets", json=body, headers={"X-Customer-Id": "C-1003"}).status_code == 401
    ok = c.post("/v1/tickets", json=body, headers={"X-Customer-Id": "C-1003", "Authorization": "Bearer s3cret"})
    assert ok.status_code == 200
    c.app.state.wiring.close()  # type: ignore[attr-defined]


def test_settings_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPS_STORE", "postgres")
    with pytest.raises(ConfigError, match="OPS_DATABASE_URL"):
        Settings.from_env()
    monkeypatch.setenv("OPS_STORE", "redis")
    with pytest.raises(ConfigError):
        Settings.from_env()


def test_sqlite_store(tmp_path: Path) -> None:
    wiring = build(Settings(store="sqlite", sqlite_path=str(tmp_path / "x.db")))
    try:
        from opsagents.models import Ticket

        r = wiring.engine.submit(
            Ticket(ticket_id="T-7002", customer_id="C-1001", subject="s", body="Where is AF-24001?")
        )
        assert r.status == "resolved"
    finally:
        wiring.close()


def test_postgres_store(postgres_url: str) -> None:
    from opsagents.models import Ticket

    wiring = build(Settings(store="postgres", database_url=postgres_url))
    try:
        t = Ticket(
            ticket_id="T-7003",
            customer_id="C-1003",
            subject="s",
            body="AF-24003 is two days late. We expect the SLA credit.",
        )
        assert wiring.engine.submit(t, run_id="pg-1").status == "awaiting_approval"
        assert wiring.engine.resume("pg-1", approved=True, approver="u").status == "resolved"
        assert any(e.tool == "issue_credit" and e.status == "ok" for e in wiring.engine.ledger.entries("pg-1"))
    finally:
        wiring.close()


def test_llm_settings_use_replay(tmp_path: Path) -> None:
    from opsagents.config import brain_factory
    from opsagents.policy import load_policy
    from opsagents.providers import ReplayProvider

    cassette = tmp_path / "c.jsonl"
    cassette.write_text("")
    brain = brain_factory(Settings(brain="llm", replay_cassette=str(cassette)))(load_policy())
    assert brain.name == "llm" and isinstance(brain.provider, ReplayProvider)  # type: ignore[attr-defined]


def test_cli_demo(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["demo"]) == 0
    out = capsys.readouterr().out
    assert "waiting for approval: Credit 420.00 EUR" in out
    assert 'queue": "security"' in out
    assert "blocked: get_shipment (not_found" in out


def test_cli_run(capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        main(
            ["run", "--customer", "C-1003", "--body", "AF-24003 is late, apply the SLA credit", "--approve", "--trace"]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "status: resolved" in out and "trace:" in out and "420.00 EUR" in out
    assert main(["run", "--customer", "C-1001", "--body", "Where is AF-24001?", "--json"]) == 0
    assert '"status": "resolved"' in capsys.readouterr().out


def test_cli_eval(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    report = tmp_path / "r.md"
    assert main(["eval", "--check", "--report", str(report)]) == 0
    assert "rule brain" in report.read_text() and "rogue brain" in report.read_text()
    assert summary.read_text().startswith("## Agent evaluation")
    assert main(["eval", "--brain", "rogue", "--json"]) == 0
    assert '"brain": "rogue"' in capsys.readouterr().out
    strict = tmp_path / "t.yaml"
    strict.write_text("max_mean_tool_calls: 1\n")
    assert main(["eval", "--brain", "rule", "--check", "--thresholds", str(strict)]) == 1
    assert "gate FAILED" in capsys.readouterr().err


def test_cli_config_error(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("OPS_BRAIN", "oracle")
    assert main(["demo"]) == 2
    assert "configuration error" in capsys.readouterr().err
