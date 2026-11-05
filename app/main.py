from __future__ import annotations

import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from email.message import Message

import httpx
from fastapi import FastAPI, Header, HTTPException, Request, status
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse, Response
from starlette.types import Receive, Scope, Send

from app.admission import DeliveryAdmission, Reservation
from app.metrics import DeliveryMetrics
from app.config import load_config, BridgeConfig
from app.engine import TemplateEngine
from app.dispatcher import SinkDispatcher

# Config path can be overridden via environment variable
CONFIG_PATH = os.getenv("ALERT_BRIDGE_CONFIG", "config.yaml")
MAX_ALERT_BODY_BYTES = 1024 * 1024


@asynccontextmanager
async def lifespan(app: FastAPI):
    # HTTPX INFO and httpcore DEBUG logs can expose destination URLs and headers.
    for name in ("httpx", "httpcore"):
        logging.getLogger(name).setLevel(logging.WARNING)

    # Load configuration on startup (crashes if config is missing/invalid)
    config = load_config(CONFIG_PATH)
    http_client = httpx.AsyncClient(timeout=10.0)
    app.state.config = config
    app.state.engine = TemplateEngine()
    app.state.http_client = http_client
    app.state.admission = DeliveryAdmission(config.delivery.max_outstanding)
    app.state.metrics = DeliveryMetrics(s.id for s in config.sinks)
    app.state.dispatcher = SinkDispatcher(config.sinks, http_client, config.delivery.max_concurrency, metrics=app.state.metrics)
    try:
        yield
    finally:
        await http_client.aclose()


app = FastAPI(
    title="Alert Bridge",
    description="A decoupled, secure webhook alerting bridge",
    lifespan=lifespan,
)


@app.get("/health", status_code=status.HTTP_200_OK)
async def health_check():
    return {"status": "ok"}


@app.get("/metrics", include_in_schema=False)
async def metrics():
    admission = app.state.admission
    return Response(
        app.state.metrics.render(capacity=admission.capacity, outstanding=admission.in_use,
                                 concurrency=app.state.config.delivery.max_concurrency),
        headers={"Content-Type": "text/plain; version=0.0.4; charset=utf-8", "Cache-Control": "no-store"},
    )


class DeliveryResponse(JSONResponse):
    """Hold admission through response sending and background delivery."""

    def __init__(self, reservation: Reservation, **kwargs):
        self.reservation = reservation
        super().__init__(**kwargs)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Also covers disconnects/cancellation before background work starts.
            self.reservation.release()


async def read_alert_payload(request: Request) -> dict:
    """Read a bounded JSON body after authenticating the route."""
    declared_length = request.headers.get("content-length")
    if declared_length is not None:
        try:
            size = int(declared_length)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid Content-Length header") from exc
        if size < 0:
            raise HTTPException(status_code=400, detail="Invalid Content-Length header")
        if size > MAX_ALERT_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Alert payload too large")

    body = bytearray()
    async for chunk in request.stream():
        if len(body) + len(chunk) > MAX_ALERT_BODY_BYTES:
            raise HTTPException(status_code=413, detail="Alert payload too large")
        body.extend(chunk)

    if not body:
        return {}

    # Preserve the locked FastAPI version's JSON media-type validation.
    media_type = Message()
    media_type["content-type"] = request.headers.get("content-type", "")
    if media_type.get_content_maintype() != "application" or not (
        media_type.get_content_subtype() == "json"
        or media_type.get_content_subtype().endswith("+json")
    ):
        raise HTTPException(
            status_code=422,
            detail=[{"loc": ["body"], "msg": "Input should be a valid dictionary", "type": "dict_type"}],
        )

    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
        raise HTTPException(
            status_code=422,
            detail=[{"loc": ["body"], "msg": "JSON decode error", "type": "json_invalid"}],
        ) from exc

    # Body(default={}) previously used the default for JSON null as well.
    if payload is None:
        return {}

    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=422,
            detail=[{"loc": ["body"], "msg": "Input should be a valid dictionary", "type": "dict_type"}],
        )
    return payload


@app.post(
    "/alerts/{route_id}",
    status_code=status.HTTP_202_ACCEPTED,
    responses={503: {"description": "Delivery capacity exhausted; retry later"}},
    openapi_extra={
        "requestBody": {
            "content": {"application/json": {"schema": {"type": "object"}}},
            "required": False,
        }
    },
)
async def handle_alert(
    route_id: str,
    request: Request,
    x_secret_token: str | None = Header(default=None, alias="X-Secret-Token"),
):
    config: BridgeConfig = app.state.config

    # 1. Look up the route
    route = config.route_by_id(route_id)
    if not route:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Route '{route_id}' not found",
        )

    # 2. Authenticate strictly via X-Secret-Token header
    expected_token = route.secret_token.get_secret_value()
    if not x_secret_token or not hmac.compare_digest(x_secret_token, expected_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing secret token",
        )

    reservation = app.state.admission.try_reserve(len(route.forward_to))
    if reservation is None:
        app.state.metrics.alerts["overloaded"] += 1
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Delivery capacity exhausted",
            headers={"Retry-After": "1"},
        )

    try:
        payload = await read_alert_payload(request)
        query_params = {k: v for k, v in request.query_params.items() if k != "token"}
        engine: TemplateEngine = app.state.engine
        template = config.template_by_id(route.template_ref)
        template_body = template.body if template else "{{ payload | tojson }}"
        template_format = template.format if template else "text"
        success, rendered_message = await engine.render(template_body, payload, query_params)
        if not success:
            raise HTTPException(status_code=500, detail=rendered_message)

        dispatcher: SinkDispatcher = app.state.dispatcher
        response = DeliveryResponse(
            reservation,
            status_code=202,
            content={"status": "accepted", "route": route_id, "scheduled_sinks": list(route.forward_to)},
            background=BackgroundTask(dispatcher.dispatch_many, route.forward_to, rendered_message, fmt=template_format),
        )
        app.state.metrics.alerts["accepted"] += 1
        return response
    except BaseException:
        # Includes cancellation while reading or rendering, before response ownership.
        reservation.release()
        raise
