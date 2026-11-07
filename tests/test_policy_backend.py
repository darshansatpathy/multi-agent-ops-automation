from pathlib import Path

import pytest

from opsagents.backend import Conflict, Crash, FaultPlan, NotFound, OpsBackend
from opsagents.policy import Policy, PolicyError, load_policy


def test_policy_loads_and_computes_credits(policy: Policy) -> None:
    assert policy.credit_for("gold", 1800) == 180.0
    assert policy.credit_for("standard", 1800) == 0.0
    assert policy.credit_for("unknown", 1800) == 0.0
    assert "issue_credit" in policy.tools_for("billing")
    assert "issue_credit" not in policy.tools_for("comms")
    assert policy.tools_for("nobody") == frozenset()


def test_policy_rejects_bad_files(tmp_path: Path) -> None:
    bad = tmp_path / "p.yaml"
    bad.write_text("credits: {}\n")
    with pytest.raises(PolicyError):
        load_policy(bad)
    text = (
        Path("policies/ops-policy.yaml").read_text().replace("auto_approve_max_eur: 250", "auto_approve_max_eur: 9000")
    )
    bad.write_text(text)
    with pytest.raises(PolicyError, match="must not exceed"):
        load_policy(bad)


def test_sla_states(backend: OpsBackend) -> None:
    assert backend.sla("AF-24001")["state"] == "at_risk"
    assert backend.sla("AF-24002")["state"] == "on_time"
    late = backend.sla("AF-24004")
    assert (late["state"], late["delay_days"], late["delivered"]) == ("breached", 2, True)
    with pytest.raises(NotFound):
        backend.sla("AF-99999")


def test_capacity_sorted_by_arrival_then_price(backend: OpsBackend) -> None:
    options = backend.find_capacity("NLRTM-DEHAM", "2026-11-02")
    assert [o["carrier_id"] for o in options] == ["CR-02", "CR-06"]
    assert options[0]["eta_date"] == "2026-11-03"
    assert backend.find_capacity("NLRTM-DEHAM", "2026-11-04") == []


def test_writes_are_idempotent_by_key(backend: OpsBackend) -> None:
    a = backend.issue_credit("k1", "INV-8001", 180.0, "sla_breach")
    b = backend.issue_credit("k1", "INV-8001", 180.0, "sla_breach")
    assert a == b
    assert backend.write_count == 1
    assert backend.applied("k1") == a
    assert backend.applied("nope") is None
    with pytest.raises(Conflict):
        backend.issue_credit("k2", "INV-8001", 1700.0, "sla_breach")  # would exceed the invoice
    with pytest.raises(NotFound):
        backend.issue_credit("k3", "INV-0000", 1.0, "sla_breach")


def test_rebook_updates_eta_and_rejects_missing_capacity(backend: OpsBackend) -> None:
    rb = backend.rebook("k", "AF-24001", "CR-02", "2026-11-02")
    assert rb["new_eta"] == "2026-11-03"
    assert backend.shipment("AF-24001")["eta_date"] == "2026-11-03"
    with pytest.raises(Conflict):
        backend.rebook("k2", "AF-24006", "CR-01", "2026-11-02")


def test_fault_plan_is_deterministic_and_bounded() -> None:
    plan = FaultPlan(rate=0.5, seed=4)
    pattern = [plan.should_fail(f"c{i}", 1) for i in range(50)]
    assert pattern == [FaultPlan(rate=0.5, seed=4).should_fail(f"c{i}", 1) for i in range(50)]
    assert 10 < sum(pattern) < 40
    assert not plan.should_fail("c1", plan.max_failures_per_call + 1)
    assert not FaultPlan().should_fail("c", 1)


def test_crash_happens_once_after_the_write() -> None:
    backend = OpsBackend.from_file(faults=FaultPlan(crash_after="escalate"))
    with pytest.raises(Crash):
        backend.escalate("k", "T-0001", "billing", "reason")
    assert backend.escalations  # applied before the crash
    assert backend.escalate("k", "T-0001", "billing", "reason")["escalation_id"] == "ESC-0001"
    assert backend.escalate("k2", "T-0001", "billing", "again")["escalation_id"] == "ESC-0002"
