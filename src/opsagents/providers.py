"""Chat models with tool calling, behind one interface.

- `ScriptedProvider`: replies from a list; for tests.
- `OpenAICompatibleProvider`: any Chat Completions endpoint with function calling
  (OpenAI, Azure OpenAI, vLLM, Ollama, LiteLLM), temperature 0 by default.
- `RecordingProvider` / `ReplayProvider`: record a real model's replies to a cassette
  (JSONL keyed by a hash of the request), then replay them byte for byte. A recorded run
  becomes a deterministic regression test that needs no network and no key.
"""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from opsagents.models import canonical, digest

Message = dict[str, Any]


@dataclass(frozen=True)
class ToolCallRequest:
    name: str
    arguments: str  # raw JSON text, exactly as the model produced it


@dataclass(frozen=True)
class ModelReply:
    content: str | None = None
    tool_calls: tuple[ToolCallRequest, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "content": self.content,
            "tool_calls": [{"name": t.name, "arguments": t.arguments} for t in self.tool_calls],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ModelReply:
        calls = tuple(ToolCallRequest(str(t["name"]), str(t["arguments"])) for t in data.get("tool_calls") or [])
        return cls(data.get("content"), calls)


class ModelProvider(Protocol):
    def complete(self, messages: Sequence[Message], tools: Sequence[dict[str, Any]]) -> ModelReply: ...


class ProviderError(Exception):
    pass


def request_key(messages: Sequence[Message], tools: Sequence[dict[str, Any]]) -> str:
    return digest(canonical(list(messages)), canonical(list(tools)))


@dataclass
class ScriptedProvider:
    replies: list[ModelReply]
    seen: list[list[Message]] = field(default_factory=list)

    def complete(self, messages: Sequence[Message], tools: Sequence[dict[str, Any]]) -> ModelReply:
        self.seen.append([dict(m) for m in messages])
        if not self.replies:
            raise ProviderError("scripted provider has no replies left")
        return self.replies.pop(0)


@dataclass
class OpenAICompatibleProvider:
    base_url: str
    model: str
    api_key: str = ""
    azure_api_version: str | None = None
    timeout_seconds: float = 60.0
    temperature: float = 0.0
    seed: int | None = 7
    transport: Any = None

    @classmethod
    def from_env(cls) -> OpenAICompatibleProvider:
        return cls(
            base_url=os.environ.get("OPS_LLM_BASE_URL", "https://api.openai.com/v1"),
            model=os.environ.get("OPS_LLM_MODEL", "gpt-4o-mini"),
            api_key=os.environ.get("OPS_LLM_API_KEY", ""),
            azure_api_version=os.environ.get("OPS_AZURE_API_VERSION") or None,
        )

    def complete(self, messages: Sequence[Message], tools: Sequence[dict[str, Any]]) -> ModelReply:
        import httpx

        headers = {"Content-Type": "application/json"}
        params: dict[str, str] = {}
        if self.azure_api_version:
            headers["api-key"] = self.api_key
            params["api-version"] = self.azure_api_version
        elif self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body: dict[str, Any] = {"model": self.model, "messages": list(messages), "temperature": self.temperature}
        if self.seed is not None:
            body["seed"] = self.seed
        if tools:
            body["tools"] = list(tools)
            body["tool_choice"] = "required"
        with httpx.Client(timeout=self.timeout_seconds, transport=self.transport) as client:
            resp = client.post(
                self.base_url.rstrip("/") + "/chat/completions", headers=headers, params=params, json=body
            )
        if resp.status_code >= 400:
            raise ProviderError(f"model endpoint returned {resp.status_code}: {resp.text[:300]}")
        try:
            msg = resp.json()["choices"][0]["message"]
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderError("response has no choices[0].message") from exc
        calls = tuple(
            ToolCallRequest(str(c["function"]["name"]), str(c["function"].get("arguments") or "{}"))
            for c in msg.get("tool_calls") or []
        )
        return ModelReply(msg.get("content"), calls)


@dataclass
class RecordingProvider:
    inner: ModelProvider
    cassette: Path

    def complete(self, messages: Sequence[Message], tools: Sequence[dict[str, Any]]) -> ModelReply:
        reply = self.inner.complete(messages, tools)
        self.cassette.parent.mkdir(parents=True, exist_ok=True)
        with self.cassette.open("a", encoding="utf-8") as fh:
            fh.write(canonical({"key": request_key(messages, tools), "reply": reply.to_dict()}) + "\n")
        return reply


@dataclass
class ReplayProvider:
    cassette: Path

    def __post_init__(self) -> None:
        self._replies: dict[str, ModelReply] = {}
        for line in self.cassette.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                self._replies.setdefault(row["key"], ModelReply.from_dict(row["reply"]))

    def complete(self, messages: Sequence[Message], tools: Sequence[dict[str, Any]]) -> ModelReply:
        key = request_key(messages, tools)
        if key not in self._replies:
            raise ProviderError("request not in cassette: prompts, tools or state changed since recording")
        return self._replies[key]
