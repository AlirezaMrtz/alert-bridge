from __future__ import annotations

import hmac
import os
from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, Header, HTTPException, Query, Request, status


from app.config import load_config, BridgeConfig
from app.engine import TemplateEngine
from app.dispatcher import SinkDispatcher

# Config path can be overridden via environment variable
CONFIG_PATH = os.getenv("ALERT_BRIDGE_CONFIG", "config.yaml")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Load configuration on startup (crashes if config is missing/invalid)
    config = load_config(CONFIG_PATH)
    app.state.config = config
    app.state.engine = TemplateEngine()
    app.state.dispatcher = SinkDispatcher(config.sinks)
    yield


app = FastAPI(
    title="Alert Bridge",
    description="A decoupled, secure webhook alerting bridge",
    lifespan=lifespan,
)



@app.post("/alerts/{route_id}", status_code=status.HTTP_200_OK)
async def handle_alert(
    route_id: str,
    request: Request,
    payload: dict = Body(default={}),
    token: str | None = Query(default=None, description="Secret token for authentication"),
    x_secret_token: str | None = Header(default=None, alias="X-Secret-Token"),
):
    config: BridgeConfig | None = getattr(app.state, "config", None)
    if not config:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Configuration not loaded",
        )

    # 1. Look up the route
    route = config.route_by_id(route_id)
    if not route:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Route '{route_id}' not found",
        )

    # 2. Authenticate
    provided_token = x_secret_token or token
    expected_token = route.secret_token.get_secret_value()
    if not provided_token or not hmac.compare_digest(provided_token, expected_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing secret token",
        )


    # 4. Extract query parameters for template rendering (excluding the auth token)
    query_params = {k: v for k, v in request.query_params.items() if k != "token"}

    # 5. Render template
    engine: TemplateEngine = app.state.engine
    template = config.template_by_id(route.template_ref)
    template_body = template.body if template else "{{ payload | tojson }}"
    
    rendered_message = engine.render(template_body, payload, query_params)

    # 6. Dispatch to sinks
    dispatcher: SinkDispatcher = app.state.dispatcher
    dispatched_sinks = []
    
    for sink_id in route.forward_to:
        success = await dispatcher.dispatch(sink_id, rendered_message)
        if success:
            dispatched_sinks.append(sink_id)

    return {
        "status": "success",
        "route": route_id,
        "dispatched_to": dispatched_sinks,
    }
