"""Settings from the environment, and wiring an engine from them.

Defaults are offline: rule brain, in-memory checkpoints and ledger, seed data. Setting
`OPS_STORE=postgres` and `OPS_DATABASE_URL` makes runs durable: checkpoints and the
ledger live in PostgreSQL, so a restarted process resumes paused and crashed runs.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.checkpoint.memory import InMemorySaver

from opsagents.backend import FaultPlan, OpsBackend
from opsagents.brains import Brain
from opsagents.brains.llm import LLMBrain
from opsagents.brains.rule import RuleBrain
from opsagents.executor import ToolExecutor
from opsagents.graph import OpsEngine, make_engine
from opsagents.ledger import InMemoryLedger, LedgerStore, SqlLedger
from opsagents.policy import load_policy
from opsagents.providers import ModelProvider, OpenAICompatibleProvider, RecordingProvider, ReplayProvider


class ConfigError(ValueError):
    pass


def _choice(name: str, default: str, allowed: tuple[str, ...]) -> str:
    value = os.environ.get(name, default).strip().lower()
    if value not in allowed:
        raise ConfigError(f"{name} must be one of {', '.join(allowed)} (got {value!r})")
    return value


@dataclass(frozen=True)
class Settings:
    brain: str = "rule"  # rule | llm
    store: str = "memory"  # memory | sqlite | postgres
    sqlite_path: str = "opsagents.db"
    database_url: str | None = None
    policy_path: str | None = None
    seed_path: str | None = None
    service_token: str | None = None
    approver_group: str = "ops-approvers"
    staff_group: str = "ops-staff"
    record_cassette: str | None = None
    replay_cassette: str | None = None
    log_level: str = "INFO"

    @classmethod
    def from_env(cls) -> Settings:
        store = _choice("OPS_STORE", "memory", ("memory", "sqlite", "postgres"))
        url = os.environ.get("OPS_DATABASE_URL") or None
        if store == "postgres" and not url:
            raise ConfigError("OPS_STORE=postgres needs OPS_DATABASE_URL")
        return cls(
            brain=_choice("OPS_BRAIN", "rule", ("rule", "llm")),
            store=store,
            sqlite_path=os.environ.get("OPS_SQLITE_PATH", "opsagents.db"),
            database_url=url,
            policy_path=os.environ.get("OPS_POLICY") or None,
            seed_path=os.environ.get("OPS_SEED") or None,
            service_token=os.environ.get("OPS_SERVICE_TOKEN") or None,
            approver_group=os.environ.get("OPS_APPROVER_GROUP", "ops-approvers"),
            staff_group=os.environ.get("OPS_STAFF_GROUP", "ops-staff"),
            record_cassette=os.environ.get("OPS_RECORD_CASSETTE") or None,
            replay_cassette=os.environ.get("OPS_REPLAY_CASSETTE") or None,
            log_level=os.environ.get("OPS_LOG_LEVEL", "INFO"),
        )


@dataclass
class Wiring:
    engine: OpsEngine
    closers: list[Callable[[], None]] = field(default_factory=list)

    def close(self) -> None:
        for close in reversed(self.closers):
            close()


def provider_from(settings: Settings, inner: ModelProvider | None = None) -> ModelProvider:
    if settings.replay_cassette:
        return ReplayProvider(Path(settings.replay_cassette))
    model = inner or OpenAICompatibleProvider.from_env()
    if settings.record_cassette:
        return RecordingProvider(model, Path(settings.record_cassette))
    return model


def brain_factory(settings: Settings, provider: ModelProvider | None = None) -> Callable[[Any], Brain]:
    if settings.brain == "llm":
        model = provider_from(settings, provider)
        return lambda policy: LLMBrain(model, policy)
    return RuleBrain


def build(settings: Settings | None = None, faults: FaultPlan | None = None) -> Wiring:
    s = settings or Settings.from_env()
    policy = load_policy(s.policy_path)
    backend = OpsBackend.from_file(s.seed_path, faults=faults)
    closers: list[Callable[[], None]] = []
    ledger: LedgerStore
    checkpointer: BaseCheckpointSaver[Any]
    if s.store == "postgres":
        import psycopg
        from langgraph.checkpoint.postgres import PostgresSaver
        from psycopg.rows import dict_row

        assert s.database_url is not None
        conn: Any = psycopg.connect(s.database_url, autocommit=True, prepare_threshold=0, row_factory=dict_row)
        saver = PostgresSaver(conn)
        saver.setup()
        checkpointer = saver
        ledger = SqlLedger.postgres(s.database_url)
        closers += [conn.close, ledger.close]
    elif s.store == "sqlite":
        from langgraph.checkpoint.sqlite import SqliteSaver

        sconn = sqlite3.connect(s.sqlite_path, check_same_thread=False)
        checkpointer = SqliteSaver(sconn)
        ledger = SqlLedger.sqlite(s.sqlite_path)
        closers += [sconn.close, ledger.close]
    else:
        checkpointer, ledger = InMemorySaver(), InMemoryLedger()
    executor = ToolExecutor(backend, policy, ledger)
    engine = make_engine(backend, executor, brain_factory(s)(policy), checkpointer)
    return Wiring(engine, closers)
