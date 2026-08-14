# Alert Bridge

[![CI](https://github.com/AlirezaMrtz/alert-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/AlirezaMrtz/alert-bridge/actions/workflows/ci.yml)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/release/python-3120/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688.svg)](https://fastapi.tiangolo.com)
[![uv](https://img.shields.io/badge/packager-uv-purple.svg)](https://github.com/astral-sh/uv)

A high-performance, asynchronous microservice built in Python 3.12 and FastAPI for accepting incoming webhooks (from Prometheus, Grafana, GitHub, etc.), securely authenticating callers, formatting payloads via sandboxed Jinja2 templates, and dispatching alerts concurrently to multi-channel sinks (Matrix, Slack, Discord, and generic HTTP webhooks).

---

## 🏗️ Architecture & Request Lifecycle

```
[ Webhook Source ] ──( HTTP POST /alerts/{route_id} )──> [ FastAPI Web Ingress ]
 (Prometheus / Grafana)                                              │
                                                       1. Constant-Time Auth Check (hmac)
                                                       2. Extract Params & Payload
                                                       3. Sandboxed Jinja2 Rendering
                                                       4. HTTP 202 Accepted Response
                                                                     │
                                                           ( BackgroundTasks )
                                                                     │
                                                       5. Concurrent Dispatcher
                                                                     │
                                                   ┌─────────────────┴─────────────────┐
                                                   ▼                                   ▼
                                           [ MatrixSink ]                      [ WebhookSink ]
                                      (Matrix API Endpoint)                 (Slack/Discord/Custom)
```

1. **Ingress Ingest:** FastAPI receives the incoming HTTP POST webhook at `/alerts/{route_id}`.
2. **Strict Authentication:** The request is authenticated strictly via the `X-Secret-Token` HTTP header using `hmac.compare_digest` to prevent timing attacks.
3. **Template Rendering:** The payload and URL query parameters are passed to an isolated `SandboxedEnvironment` Jinja2 engine wrapped in `asyncio.wait_for(..., timeout=0.5)` to guarantee protection against CPU/memory loop bombs and RCE.
4. **Immediate Acknowledgment (`202 Accepted`):** The ingress controller returns an HTTP `202 Accepted` status immediately, decoupling caller response latency from downstream sink processing.
5. **Concurrent Background Dispatch:** Delivery tasks execute concurrently via FastAPI `BackgroundTasks` and `asyncio.gather`. Sinks run with fault isolation and a 30-second execution timeout per target.

---

## 🛡️ Defense-in-Depth & Security Hardening

| Security Layer | Implementation Mechanism | Threat Mitigated |
| :--- | :--- | :--- |
| **Sandbox Execution** | `jinja2.sandbox.SandboxedEnvironment` | Remote Code Execution (RCE) via untrusted Jinja2 templates. |
| **CPU/Memory Protection** | `asyncio.wait_for(asyncio.to_thread(...), timeout=0.5)` | ReDoS or template loop bombs (`{{ range(10**7) }}`). |
| **Constant-Time Auth** | `hmac.compare_digest(x_secret_token, expected)` | Side-channel timing attacks guessing secret tokens. |
| **URL Path Sanitization** | `quote_plus(room_id, safe="!:")` | Path traversal and query parameter injection in Matrix room IDs. |
| **Secret Hygiene** | Query parameter `token` stripping & masked log messages | Secret leakage into logs or Jinja2 context variables. |
| **Fault Isolation** | `asyncio.wait_for(dispatch, timeout=30.0)` + `asyncio.gather` | Single hanging sink blocking delivery to sibling sinks. |
| **Non-Root Container** | Custom `appuser` (UID 10001) in docker runtime stage | Privilege escalation inside container runtime environments. |

---

## ⚙️ Configuration Schema

Configuration is defined in YAML (`config.yaml`) with support for environment variable substitution using `${VAR_NAME}` syntax:

```yaml
apiVersion: v1alpha1

sinks:
  - id: matrix-ops
    type: matrix
    config:
      room_id: "!roomid:matrix.example.com"
      access_token: "${MATRIX_TOKEN}"
      homeserver_url: "https://matrix.example.com"

  - id: slack-devs
    type: webhook
    config:
      url: "https://hooks.slack.com/services/XXX/YYY/ZZZ"
      method: "POST"
      text_field: "text"
      extra_fields:
        username: "AlertBridge"

templates:
  - id: alert-basic
    engine: jinja2
    format: markdown
    body: |
      🚨 **Alert**: {{ payload.message }}
      *Environment*: {{ params.env | default('prod') }}

routes:
  - id: prometheus-in
    secret_token: "${PROMETHEUS_SECRET_TOKEN}"
    template_ref: alert-basic
    forward_to:
      - matrix-ops
      - slack-devs
```

### Schema Rules & Validation
* **`routes[].forward_to`**: Must contain at least one sink ID (`min_length=1`).
* **`sinks[].type`**: Constrained via Pydantic `Literal["matrix", "webhook"]` to fail fast on typos during startup.
* **`sinks[].config.homeserver_url`**: Explicitly required for Matrix sinks to prevent domain-guessing vulnerabilities.

---

## 🚀 Local Development & Testing

This project uses [`uv`](https://github.com/astral-sh/uv) for fast, deterministic dependency management.

### Setup
```bash
# Clone repository
git clone https://github.com/AlirezaMrtz/alert-bridge.git
cd alert-bridge

# Sync virtual environment and dependencies
uv sync --all-groups
```

### Running the Server
```bash
export MATRIX_TOKEN="your-matrix-token"
export PROMETHEUS_SECRET_TOKEN="supersecrettoken123"
uv run uvicorn app.main:app --reload --port 8000
```

### Running Tests
```bash
uv run pytest -v
```

---

## 🐳 Containerized Deployment

### Docker Compose
Deploy locally or in production using Docker Compose:

```bash
docker-compose up -d --build
```

The container runs as a non-root system user (`appuser:10001`), mounts `config.yaml` read-only (`:ro`), and monitors container health via `GET /health`.

### Health Check Endpoint
* **URL:** `GET /health`
* **Response:** `200 OK` `{"status": "ok"}`
