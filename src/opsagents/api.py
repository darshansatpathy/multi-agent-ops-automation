"""HTTP API.

Identity is asserted by the authenticating gateway in front of this service:
`X-Customer-Id` on customer endpoints, `X-User-Id` and `X-User-Groups` on staff ones.
With `OPS_SERVICE_TOKEN` set, every call must also carry `Authorization: Bearer <token>`,
so only that gateway can assert identities.
"""

from __future__ import annotations

import hmac
import re
import threading
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field, ValidationError

from opsagents import __version__
from opsagents.config import Settings, Wiring, build
from opsagents.graph import ApprovalError, RunResult
from opsagents.models import Ticket
from opsagents.observability import configure_logging, log

_GROUP = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,63}$")


class TicketIn(BaseModel):
    ticket_id: str = Field(pattern=r"^T-[0-9]{4,8}$")
    channel: str = "portal"
    subject: str = Field(max_length=200)
    body: str = Field(max_length=4000)


class ApprovalIn(BaseModel):
    approved: bool
    note: str = Field(default="", max_length=500)


class Metrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.requests: Counter[tuple[str, int]] = Counter()
        self.runs: Counter[str] = Counter()
        self.blocked: Counter[str] = Counter()
        self.approvals: Counter[str] = Counter()
        self.latency_sum: dict[str, float] = {}

    def request(self, route: str, status: int, seconds: float) -> None:
        with self._lock:
            self.requests[(route, status)] += 1
            self.latency_sum[route] = self.latency_sum.get(route, 0.0) + seconds

    def run(self, result: RunResult) -> None:
        with self._lock:
            self.runs[result.status] += 1
            for e in result.ledger:
                if e["status"] == "rejected":
                    self.blocked[str(e["error_kind"])] += 1

    def render(self) -> str:
        out = ["# TYPE ops_http_requests_total counter"]
        with self._lock:
            out += [
                f'ops_http_requests_total{{route="{r}",status="{s}"}} {n}'
                for (r, s), n in sorted(self.requests.items())
            ]
            out.append("# TYPE ops_http_request_seconds_sum counter")
            out += [f'ops_http_request_seconds_sum{{route="{r}"}} {v:.6f}' for r, v in sorted(self.latency_sum.items())]
            out.append("# TYPE ops_runs_total counter")
            out += [f'ops_runs_total{{status="{k}"}} {n}' for k, n in sorted(self.runs.items())]
            out.append("# TYPE ops_blocked_actions_total counter")
            out += [f'ops_blocked_actions_total{{kind="{k}"}} {n}' for k, n in sorted(self.blocked.items())]
            out.append("# TYPE ops_approvals_total counter")
            out += [f'ops_approvals_total{{decision="{k}"}} {n}' for k, n in sorted(self.approvals.items())]
        return "\n".join(out) + "\n"


def _public(result: RunResult, staff: bool) -> dict[str, Any]:
    data = result.to_dict()
    if not staff:  # customers see the outcome, not the internals
        return {"run_id": data["run_id"], "status": data["status"], "duplicate": data["duplicate"]}
    return data


def create_app(wiring: Wiring | None = None, settings: Settings | None = None) -> FastAPI:
    settings = settings or Settings.from_env()
    configure_logging(settings.log_level)
    wired = wiring or build(settings)
    engine = wired.engine
    if not settings.service_token:
        log.warning("OPS_SERVICE_TOKEN is not set: identity headers are trusted (development mode)")
    lock = threading.Lock()  # the simulated backend is in-process; serialise runs against it
    metrics = Metrics()
    app = FastAPI(title="multi-agent-ops-automation", version=__version__)

    @app.middleware("http")
    async def timing(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", rid):
            rid = uuid.uuid4().hex
        started = time.perf_counter()
        response = await call_next(request)
        route = getattr(request.scope.get("route"), "path", "unmatched")
        metrics.request(route, response.status_code, time.perf_counter() - started)
        response.headers["X-Request-ID"] = rid
        return response

    def gateway(authorization: str | None = Header(default=None)) -> None:
        if settings.service_token:
            presented = (authorization or "").removeprefix("Bearer ").strip()
            if not hmac.compare_digest(presented.encode(), settings.service_token.encode()):
                raise HTTPException(status_code=401, detail="missing or invalid service token")

    def staff(
        _: None = Depends(gateway),
        x_user_id: str | None = Header(default=None),
        x_user_groups: str | None = Header(default=None),
    ) -> tuple[str, set[str]]:
        groups = {g.strip().lower() for g in (x_user_groups or "").split(",") if g.strip()}
        if any(not _GROUP.match(g) for g in groups):
            raise HTTPException(status_code=400, detail="invalid group name")
        if not x_user_id or not groups & {settings.staff_group, settings.approver_group}:
            raise HTTPException(status_code=403, detail="staff only")
        return x_user_id, groups

    @app.get("/healthz")
    def healthz() -> dict[str, Any]:
        return {"status": "ok", "version": __version__, "brain": engine.brain.name}

    @app.get("/metrics")
    def prometheus() -> Response:
        return Response(metrics.render(), media_type="text/plain; version=0.0.4")

    @app.post("/v1/tickets")
    def submit(
        body: TicketIn,
        _: None = Depends(gateway),
        x_customer_id: str | None = Header(default=None),
    ) -> dict[str, Any]:
        try:
            ticket = Ticket(customer_id=x_customer_id or "", **body.model_dump())
        except ValidationError as exc:
            raise HTTPException(status_code=422, detail="invalid ticket or customer id") from exc
        with lock:
            result = engine.submit(ticket)
        if not result.duplicate:
            metrics.run(result)
        return _public(result, staff=False)

    @app.get("/v1/runs/{run_id}")
    def get_run(run_id: str, who: tuple[str, set[str]] = Depends(staff)) -> dict[str, Any]:
        try:
            return _public(engine.get(run_id), staff=True)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc

    @app.post("/v1/runs/{run_id}/approval")
    def approve(
        run_id: str,
        body: ApprovalIn,
        who: tuple[str, set[str]] = Depends(staff),
    ) -> dict[str, Any]:
        user, groups = who
        if settings.approver_group not in groups:
            raise HTTPException(status_code=403, detail=f"approvals need the {settings.approver_group} group")
        try:
            with lock:
                result = engine.resume(run_id, approved=body.approved, approver=user, note=body.note)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc
        except ApprovalError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        metrics.approvals["approved" if body.approved else "denied"] += 1
        metrics.run(result)
        return _public(result, staff=True)

    app.state.wiring = wired
    return app
