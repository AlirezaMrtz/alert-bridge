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
                                                       5. Concurrent Dispatcher (Shared Client)
                                                                     │
                                                   ┌─────────────────┴─────────────────┐
                                                   ▼                                   ▼
                                           [ MatrixSink ]                      [ WebhookSink ]
                                      (Matrix API Endpoint)                 (Slack/Discord/Custom)
```

1. **Ingress Ingest:** FastAPI receives the incoming HTTP POST webhook at `/alerts/{route_id}`.
2. **Strict Authentication:** The request is authenticated strictly via the `X-Secret-Token` HTTP header using `hmac.compare_digest` to prevent timing attacks.
3. **Template Rendering:** The payload and URL query parameters are passed to an isolated `SandboxedEnvironment` Jinja2 engine wrapped in `asyncio.wait_for(..., timeout=0.5)` to bound request rendering latency.
4. **Immediate Acknowledgment (`202 Accepted`):** The ingress controller returns an HTTP `202 Accepted` status immediately, decoupling caller response latency from downstream sink processing. If rendering fails, a `500 Internal Server Error` is returned immediately and background dispatch is aborted.
5. **Concurrent Background Dispatch:** Delivery tasks execute concurrently via FastAPI `BackgroundTasks` and `asyncio.gather` over a shared connection pool (`httpx.AsyncClient`). Sinks run with fault isolation and a 35-second execution timeout per target.

---

## 🛡️ Defense-in-Depth & Security Hardening

> **Threat Model Note**: Templates are loaded strictly from administrator-controlled YAML configuration files (`config.yaml`), while incoming webhooks supply runtime data as context. `SandboxedEnvironment` is employed as defense-in-depth against config-author syntax/attribute privilege escalation or future architectures that might accept externally supplied templates.

| Security Layer | Implementation Mechanism | Threat Mitigated |
| :--- | :--- | :--- |
| **Sandboxed Jinja2** | `jinja2.sandbox.SandboxedEnvironment` | Defense-in-depth against arbitrary attribute/method access and privilege escalation in templates. |
| **Execution Latency Bound** | `asyncio.wait_for(asyncio.to_thread(...), timeout=0.5)` | Bounds rendering execution latency per request to 500ms. |
| **Constant-Time Auth** | `hmac.compare_digest(x_secret_token, expected)` | Side-channel timing attacks guessing secret tokens. |
| **URL Path Sanitization** | `quote(str(room_id), safe="!:")` | RFC 3986 path percent-encoding preventing injection in Matrix room IDs. |
| **Secret Hygiene** | Query parameter `token` stripping & masked log messages | Secret leakage into logs or Jinja2 context variables. |
| **Connection Pooling & Lifecycle** | Shared `httpx.AsyncClient` in application lifespan | Resource leaks and connection overhead across high-throughput dispatches. |
| **Fault Isolation** | `asyncio.wait_for(dispatch, timeout=35.0)` + `asyncio.gather` | Single hanging sink blocking delivery to sibling sinks. |
| **Transient Fault & Rate-Limit Resilience** | 3-attempt exponential retry loop with HTTP 429 `Retry-After` clamping ($\le 2.0\text{s}$) | Prevents alert loss on transient 5xx spikes and rate-limit bursts while guaranteeing $\le 35.0\text{s}$ SLA bound. |
| **Non-Root Container** | Custom `appuser` (UID 10001) in docker runtime stage | Privilege escalation inside container runtime environments. |

---

## 🔌 API Specification & Usage

### 1. Alert Ingress Endpoint: `POST /alerts/{route_id}`

Accepts incoming alert webhooks matching a configured route ID.

#### Standard Dispatch Request
```bash
curl -X POST "http://localhost:8000/alerts/prometheus-in" \
  -H "Content-Type: application/json" \
  -H "X-Secret-Token: supersecrettoken123" \
  -d '{
    "status": "firing",
    "message": "High CPU utilization detected",
    "host": "prod-db-01"
  }'
```

#### Dynamic Query Parameter Injection
Query parameters passed in the request URL are exposed directly in the Jinja2 template under the `params` namespace:

```bash
curl -X POST "http://localhost:8000/alerts/prometheus-in?env=production&cluster=eu-central-1" \
  -H "Content-Type: application/json" \
  -H "X-Secret-Token: supersecrettoken123" \
  -d '{"message": "Kubernetes Pod CrashLoopBackOff"}'
```

In the template:
```jinja2
🚨 Alert: {{ payload.message }}
Environment: {{ params.env | default('prod') }}
Cluster: {{ params.cluster }}
```

#### Response Status Codes & Contracts

| HTTP Status | Description | Response Body Example |
| :--- | :--- | :--- |
| **`202 Accepted`** | Successfully validated, rendered, and queued for async delivery. | `{"status": "accepted", "route": "prometheus-in", "scheduled_sinks": ["matrix-ops", "slack-devs"]}` |
| **`401 Unauthorized`** | Missing or invalid `X-Secret-Token` header. | `{"detail": "Invalid or missing secret token"}` |
| **`404 Not Found`** | The requested `route_id` is not declared in configuration. | `{"detail": "Route 'unknown-route' not found"}` |
| **`422 Unprocessable Entity`** | Malformed JSON request payload. | `{"detail": [{"loc": ["body"], "msg": "JSON decode error"}]}` |
| **`500 Internal Server Error`** | Template render failure or timeout (>0.5s); delivery halted. | `{"detail": "Template rendering failed: ..."}` (timeout variant: `{"detail": "Template rendering timed out (>0.5s)"}`) |

---

### 2. Health & Interactive Documentation

* **Health Probe:** `GET /health` $\rightarrow$ `200 OK` `{"status": "ok"}`
* **Interactive Swagger UI:** `GET /docs`
* **ReDoc Specification:** `GET /redoc`
* **OpenAPI JSON Schema:** `GET /openapi.json`

---

## ⚙️ Configuration Schema

Configuration is defined in YAML (`config.yaml`) with support for environment variable substitution using `${VAR_NAME}` syntax:

```yaml
apiVersion: v1alpha1

sinks:
  - id: matrix-ops
    type: matrix
    config:
      room_id: "!ops-alerts:matrix.example.com"
      access_token: "${MATRIX_TOKEN}"
      homeserver_url: "https://matrix.example.com"

  - id: slack-devs
    type: webhook
    config:
      url: "https://hooks.slack.com/services/T00000000/B00000000/XXXXXXXXXXXXXXXXXXXXXXXX"
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
* **`routes[].forward_to`**: Must contain at least one sink ID (`min_length=1`), and all referenced sink IDs must exist in `sinks`.
* **`routes[].template_ref`**: Must reference a declared template in `templates`.
* **`sinks[].type`**: Constrained via Pydantic `Literal["matrix", "webhook"]` to fail fast on typos during startup.
* **`sinks[].config.homeserver_url`**: Explicitly required for Matrix sinks to prevent domain-guessing vulnerabilities.
* **`sinks[].config.url`**: Required for Webhook sinks and must begin with `http://` or `https://`.

---

## 🌐 Environment & Runtime Configuration

| Variable | Type | Default | Description |
| :--- | :--- | :--- | :--- |
| `ALERT_BRIDGE_CONFIG` | String / Path | `config.yaml` | Absolute or relative path to the YAML configuration file. |
| `${CUSTOM_VAR}` | String | *None* | Any arbitrary environment variable referenced inside `config.yaml` using `${VAR_NAME}` syntax. |

### Environment Variable Substitution
Alert Bridge pre-processes `config.yaml` before schema validation. Placeholders matching `${VARIABLE_NAME}` are resolved against the active process environment via `os.getenv()`. If an environment variable is referenced but not set, Alert Bridge raises a `ConfigError` and crashes on startup to prevent silent misconfigurations.

---

## 📊 Observability & Security Logging

Alert Bridge categorizes all log output under standard hierarchical loggers:

| Logger Namespace | Purpose |
| :--- | :--- |
| `bridge.config` | Configuration loading, schema validation, and route/sink cataloging. |
| `bridge.engine` | Template compilation, URL filter conversions, and rendering error tracking. |
| `bridge.dispatcher` | Multi-sink async execution, timeout tracking, and result collection. |
| `bridge.sinks.matrix` | Matrix room message formatting, endpoint construction, and HTTP status handling. |
| `bridge.sinks.webhook` | Generic webhook body generation, custom header injection, and HTTP delivery. |

### Zero-Leak Credential Hygiene
* **Route Token Masking:** Route secrets use Pydantic's `SecretStr` to prevent plain-text token exposure in string representations or structured startup logs (`prometheus-in->**********`).
* **Query Parameter Scrubbing:** Any `token` query parameters sent to the ingress endpoint are scrubbed prior to populating the `params` dictionary in Jinja2 template contexts.
* **Payload Isolation on Failure:** Template rendering exceptions log structured error messages without dumping raw JSON payload dictionaries, preventing sensitive payload values from leaking into central logging systems.

---

## 🚀 Local Development & Testing

This project uses [`uv`](https://github.com/astral-sh/uv) for fast, deterministic dependency management.

### Setup
```bash
# Clone repository
git clone https://github.com/AlirezaMrtz/alert-bridge.git
cd alert-bridge

# Copy example configuration
cp config.example.yaml config.yaml

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
Before starting the container, initialize `config.yaml` from the example template and export required environment variables:

```bash
# 1. Initialize configuration
cp config.example.yaml config.yaml

# 2. Set environment variables (or define in a .env file)
export MATRIX_TOKEN="your-matrix-token"
export PROMETHEUS_SECRET_TOKEN="supersecrettoken123"

# 3. Start container
docker compose up -d --build
```

The container runs as a non-root system user (`appuser:10001`), mounts `config.yaml` read-only (`:ro`), and monitors container health via `GET /health`.
