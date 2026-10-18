from __future__ import annotations

import hmac
import os
from contextlib import asynccontextmanager

import httpx
from fastapi import BackgroundTasks, Body, FastAPI, Header, HTTPException, Request, status

from app.config import load_config, BridgeConfig
from app.engine import TemplateEngine
from app.dispatcher import SinkDispatcher

# Config path can be overridden via environment variable
CONFIG_PATH = os.getenv("ALERT_BRIDGE_CONFIG", "config.yaml")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Load configuration on startup (crashes if config is missing/invalid)
    config = load_config(CONFIG_PATH)
    http_client = httpx.AsyncClient(timeout=10.0)
    app.state.config = config
    app.state.engine = TemplateEngine()
    app.state.http_client = http_client
    app.state.dispatcher = SinkDispatcher(config.sinks, http_client)
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


@app.post("/alerts/{route_id}", status_code=status.HTTP_202_ACCEPTED)
async def handle_alert(
    route_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    payload: dict = Body(default={}),
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

    # 3. Extract query parameters for template rendering (excluding the auth token)
    query_params = {k: v for k, v in request.query_params.items() if k != "token"}

    # 4. Render template asynchronously
    engine: TemplateEngine = app.state.engine
    template = config.template_by_id(route.template_ref)
    template_body = template.body if template else "{{ payload | tojson }}"
    template_format = template.format if template else "text"
    
    success, rendered_message = await engine.render(template_body, payload, query_params)
    if not success:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=rendered_message,
        )

    # 5. Schedule background dispatch to sinks
    dispatcher: SinkDispatcher = app.state.dispatcher
    background_tasks.add_task(dispatcher.dispatch_many, route.forward_to, rendered_message, fmt=template_format)

    return {
        "status": "accepted",
        "route": route_id,
        "scheduled_sinks": route.forward_to,
    }
