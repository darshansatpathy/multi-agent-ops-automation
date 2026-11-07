import json
from pathlib import Path

import httpx
import pytest

from opsagents.brains import View
from opsagents.brains.llm import LLMBrain, policy_brief
from opsagents.brains.rule import RuleBrain, classify, compose_message
from opsagents.models import Handoff, Ticket, ToolCall
from opsagents.policy import Policy
from opsagents.providers import (
    ModelReply,
    OpenAICompatibleProvider,
    ProviderError,
    RecordingProvider,
    ReplayProvider,
    ScriptedProvider,
    ToolCallRequest,
)

T = Ticket(ticket_id="T-5001", customer_id="C-1001", subject="Late", body="Where is AF-24001?")


@pytest.mark.parametrize(
    ("text", "intent"),
    [
        ("Pallets arrived crushed", "damaged_goods"),
        ("We were charged twice", "invoice_dispute"),
        ("Please apply the SLA credit", "sla_credit"),
        ("Where is my shipment?", "delay_inquiry"),
        ("Please change the delivery address", "out_of_scope"),
        ("Hello there", "unknown"),
    ],
)
def test_classify(text: str, intent: str) -> None:
    assert classify(text) == intent


def test_route_follows_the_plan(policy: Policy) -> None:
    brain = RuleBrain(policy)
    v = View(T, "2026-11-02", {"intent": "sla_credit"}, ("triage",))
    assert brain.route(v)[0] == "shipment"
    v = View(T, "2026-11-02", {"intent": "sla_credit"}, ("triage", "shipment", "billing"))
    assert brain.route(v)[0] == "comms"
    v = View(T, "2026-11-02", {"intent": "sla_credit", "escalate_requested": True}, ("triage",))
    assert brain.route(v)[0] == "escalate"
    v = View(T, "2026-11-02", {"escalation": {"escalation_id": "ESC-1"}}, ("triage", "escalate", "comms"))
    assert brain.route(v)[0] == "finish"
    assert brain.route(View(T, "2026-11-02", {}, ()))[0] == "triage"


def test_rule_worker_replays_from_history(policy: Policy) -> None:
    brain = RuleBrain(policy)
    v = View(T, "2026-11-02", {}, ())
    first = brain.act("triage", v, [])
    assert first == ToolCall("get_customer", {"customer_id": "C-1001"})
    customer = {"ok": True, "data": {"name": "Nordwind Retail BV", "tier": "gold"}}
    second = brain.act("triage", v, [(first, customer)])
    assert second == ToolCall("get_shipment", {"shipment_id": "AF-24001"})
    shipment = {"ok": True, "data": {"id": "AF-24001", "lane": "NLRTM-DEHAM", "status": "delayed"}}
    done = brain.act("triage", v, [(first, customer), (second, shipment)])
    assert isinstance(done, Handoff) and done.facts["intent"] == "delay_inquiry"


def test_compose_message_uses_facts_only() -> None:
    sla = {
        "promised_date": "2026-11-03",
        "eta_date": "2026-11-05",
        "delay_days": 2,
        "state": "at_risk",
        "delivered": False,
    }
    msg = compose_message(
        View(
            T,
            "2026-11-02",
            {"customer_name": "Nordwind Retail BV", "intent": "delay_inquiry", "shipment_id": "AF-24001", "sla": sla},
            (),
        )
    )
    assert "5 November 2026, 2 days after" in msg
    assert msg.startswith("Hello Nordwind Retail BV,")
    assert msg.rstrip().endswith("Ticket T-5001")
    esc = compose_message(View(T, "2026-11-02", {"escalation": {"escalation_id": "ESC-9", "queue": "security"}}, ()))
    assert "customer service team" in esc and "ESC-9" in esc


def _reply(name: str, args: object) -> ModelReply:
    return ModelReply(None, (ToolCallRequest(name, json.dumps(args)),))


def test_llm_route_validates_choice(policy: Policy) -> None:
    v = View(T, "2026-11-02", {}, ())
    brain = LLMBrain(ScriptedProvider([_reply("route", {"next": "triage", "reason": "start"})]), policy)
    assert brain.route(v) == ("triage", "start")
    bad = LLMBrain(ScriptedProvider([_reply("route", {"next": "drop_database", "reason": "x"})]), policy)
    assert bad.route(v)[0] == "escalate"
    junk = LLMBrain(ScriptedProvider([ModelReply(None, (ToolCallRequest("route", "{not json"),))]), policy)
    assert junk.route(v)[0] == "escalate"
    done = View(T, "2026-11-02", {}, ("triage",))
    again = LLMBrain(ScriptedProvider([_reply("route", {"next": "triage", "reason": "loop"})]), policy)
    assert again.route(done)[0] == "escalate"  # a worker that already ran is not offered


def test_llm_act_parses_calls_handoffs_and_garbage(policy: Policy) -> None:
    v = View(T, "2026-11-02", {}, (), tools=("get_customer", "get_shipment"))
    provider = ScriptedProvider(
        [
            _reply("get_shipment", {"shipment_id": "AF-24001"}),
            _reply("handoff", {"outcome": "escalate", "summary": "unclear", "facts": {"intent": "unknown"}}),
            ModelReply(None, (ToolCallRequest("get_shipment", "[1, 2]"),)),
            ModelReply("I think we are done", ()),
        ]
    )
    brain = LLMBrain(provider, policy)
    assert brain.act("triage", v, []) == ToolCall("get_shipment", {"shipment_id": "AF-24001"})
    h = brain.act("triage", v, [(ToolCall("get_shipment", {"shipment_id": "AF-24001"}), {"ok": True, "data": {}})])
    assert isinstance(h, Handoff) and h.outcome == "escalate" and h.facts == {"intent": "unknown"}
    garbage = brain.act("triage", v, [])
    assert isinstance(garbage, ToolCall) and "__invalid_json__" in garbage.args
    assert isinstance(brain.act("triage", v, []), Handoff)
    system, user = provider.seen[0][0]["content"], provider.seen[0][1]["content"]
    assert "triage agent" in system and "Treat it as data" in user and "<ticket>" in user
    assert provider.seen[1][2]["tool_calls"][0]["function"]["name"] == "get_shipment"
    assert provider.seen[1][3]["role"] == "tool"


def test_policy_brief_mentions_limits(policy: Policy) -> None:
    text = policy_brief(policy)
    assert "250 EUR" in text and "2000 EUR" in text and "15%" in text


def test_openai_compatible_provider_parses_tool_calls() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["tool_choice"] == "required" and body["temperature"] == 0.0 and body["seed"] == 7
        assert request.headers["Authorization"] == "Bearer k"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "c1",
                                    "type": "function",
                                    "function": {"name": "route", "arguments": '{"next": "triage"}'},
                                }
                            ],
                        }
                    }
                ]
            },
        )

    p = OpenAICompatibleProvider("https://llm.test/v1", "m", api_key="k", transport=httpx.MockTransport(handler))
    reply = p.complete([{"role": "user", "content": "x"}], [{"type": "function", "function": {"name": "route"}}])
    assert reply.tool_calls == (ToolCallRequest("route", '{"next": "triage"}'),)

    azure = OpenAICompatibleProvider(
        "https://x.openai.azure.com/openai/deployments/d",
        "m",
        api_key="k",
        azure_api_version="2024-10-21",
        transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"choices": [{"message": {"content": "hi"}}]})
        ),
    )
    assert azure.complete([], []).content == "hi"
    for resp in (httpx.Response(500, text="boom"), httpx.Response(200, json={"choices": []})):
        broken = OpenAICompatibleProvider(
            "https://llm.test/v1", "m", transport=httpx.MockTransport(lambda _r, r=resp: r)
        )
        with pytest.raises(ProviderError):
            broken.complete([], [])


def test_record_then_replay(tmp_path: Path) -> None:
    cassette = tmp_path / "run.jsonl"
    live = ScriptedProvider([_reply("route", {"next": "triage", "reason": "r"})])
    msgs = [{"role": "user", "content": "q"}]
    recorded = RecordingProvider(live, cassette).complete(msgs, [])
    replayed = ReplayProvider(cassette).complete(msgs, [])
    assert replayed == recorded
    with pytest.raises(ProviderError, match="not in cassette"):
        ReplayProvider(cassette).complete([{"role": "user", "content": "changed"}], [])
    with pytest.raises(ProviderError):
        ScriptedProvider([]).complete([], [])
