# Alert Bridge

[![CI](https://github.com/AlirezaMrtz/alert-bridge/actions/workflows/ci.yml/badge.svg)](https://github.com/AlirezaMrtz/alert-bridge/actions/workflows/ci.yml)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue.svg)](https://www.python.org/downloads/release/python-3120/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-009688.svg)](https://fastapi.tiangolo.com)
[![uv](https://img.shields.io/badge/packager-uv-purple.svg)](https://github.com/astral-sh/uv)

An asynchronous webhook router built with Python and FastAPI. It authenticates incoming alerts, renders messages with configurable Jinja2 templates, and forwards them to Matrix rooms or HTTP webhook destinations. Slack and Discord integrations use the generic HTTP sink with a destination-specific JSON configuration.

## Try the local demo

From a checkout of this repository, with Python 3.12+ and `uv` installed:

```bash
uv sync --locked --all-groups
uv run --locked python examples/local-demo/run.py
```

The demo starts the bridge and an HTTP receiver on automatically assigned
loopback ports, rejects a request without a token, sends an authenticated alert,
and checks the message received after template rendering and dispatch:

```json
{
  "text": "Alert: CPU usage > 90%\nHost: demo-host\nEnvironment: local"
}
```

It prints `Demo passed.` and exits with status 0 only after the receiver confirms
the expected message. Both servers shut down automatically. No Matrix account,
external webhook, or local `config.yaml` is needed. A fresh token is generated
for each run; it is never printed or saved. Proxy routing is disabled inside
the demo process so its HTTP traffic stays on the loopback interface.

The first dependency installation needs access to your Python package index.
After dependencies are installed, the demo runs without external network access.
The demo uses [its own example configuration](examples/local-demo/bridge.yaml).

---

## 🏗️ Architecture & Request Lifecycle

```mermaid
flowchart TD
    Source["Webhook source"] --> Ingress["Route, token, admission, size, and JSON checks"]
    Ingress -->|invalid| Rejected["4xx response"]
    Ingress -->|overloaded| Overloaded["503 and Retry-After"]
    Ingress -->|valid| Template["Sandboxed Jinja2 template"]
    Template -->|failure| Failed["500 response"]
    Template -->|success| Scheduled["202 and in-process background dispatch"]
    Scheduled --> Matrix["Matrix room"]
    Scheduled --> Webhook["HTTP webhook"]
```

1. **Ingress:** FastAPI receives `POST /alerts/{route_id}` and looks up the configured route.
2. **Strict Authentication:** The request is authenticated strictly via the `X-Secret-Token` HTTP header using `hmac.compare_digest` to prevent timing attacks.
3. **Payload and template:** After authentication, the application reads at most 1 MiB into its body buffer, validates the JSON body, and renders the configured template. The 0.5-second render timeout limits how long the caller waits for the worker result; it does not terminate the worker thread.
4. **Acknowledgment (`202 Accepted`):** The response means validation and rendering succeeded and background delivery was scheduled. It does not confirm receipt by a destination. A rendering failure returns `500` and prevents dispatch.
5. **Background dispatch:** Sends use a shared HTTP client and a semaphore allowing at most 50 active sink sends per process. Each target has a 35-second timeout that includes waiting for a semaphore slot. Admission reserves the complete fan-out before body parsing: at most 1,000 outstanding sink deliveries per process by default, including reservations, waiting sends, and active sends.

Delivery is **best effort**. In-flight work can be lost on a crash or forced shutdown, and HTTP retries can produce duplicate messages. This service currently has no durable queue, delivery-status API, or recovery mechanism. See [design decisions and limitations](docs/design-decisions.md) for the reasoning and operating boundaries.

---

## 🛡️ Defense-in-Depth & Security Hardening

> **Threat Model Note**: Templates are loaded strictly from administrator-controlled YAML configuration files (`config.yaml`), while incoming webhooks supply runtime data as context. `SandboxedEnvironment` is employed as defense-in-depth against config-author syntax/attribute privilege escalation or future architectures that might accept externally supplied templates.

| Security Layer | Implementation Mechanism | Threat Mitigated |
| :--- | :--- | :--- |
| **Sandboxed Jinja2** | `jinja2.sandbox.SandboxedEnvironment` | Defense-in-depth against arbitrary attribute/method access and privilege escalation in templates. |
| **Render Wait Timeout** | `asyncio.wait_for(asyncio.to_thread(...), timeout=0.5)` | Stops waiting for a slow render result; does not stop the worker thread or establish a response-time SLA under load. |
| **Constant-Time Auth** | `hmac.compare_digest(x_secret_token, expected)` | Side-channel timing attacks guessing secret tokens. |
| **Authentication-First, Size-Bounded Payload Parsing** | `read_alert_payload()` stream cap (`1 MiB`) after HMAC check | Avoids unauthenticated JSON parsing and rejects oversized bodies before JSON decoding. |
| **URL Path Sanitization** | `quote(str(room_id), safe="!:")` | RFC 3986 path percent-encoding preventing injection in Matrix room IDs. |
| **Secret Hygiene** | Query parameter `token` stripping & masked log messages | Secret leakage into logs or Jinja2 context variables. |
| **Connection Pooling & Lifecycle** | Shared `httpx.AsyncClient` in application lifespan | Resource leaks and connection overhead across high-throughput dispatches. |
| **Dispatch Concurrency and Timeout** | `asyncio.Semaphore(max_concurrency)` + per-target `asyncio.wait_for(..., timeout=35.0)` | Limits active sink sends and isolates ordinary target failures. The timeout includes slot waiting; pending work is bounded by the admission budget. |
| **Transient Error Retries** | Up to 3 attempts for network errors, 5xx, and 429; numeric `Retry-After` clamped to 2 seconds | Retries selected transient failures. Exhausted retries can still fail, and an ambiguous network failure can cause duplicate delivery. |
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

#### Template Engine Features & Resilient Rendering

The template engine uses `jinja2.sandbox.SandboxedEnvironment` with missing-key handling and URL-encoding helpers for administrator-controlled templates:

* **Resilient Key Traversal (`ChainableUndefined`):** Configured with `ChainableUndefined` so missing deeply nested fields (e.g. `{{ payload.metrics.cpu }}`) evaluate safely to an empty string instead of raising an `AttributeError` or crashing during an incident.
* **RFC 3986 URL Encoding (`| urlencode`):** Built-in filter supporting both scalar strings and dictionaries for safely constructing percent-encoded query parameters and dashboard links:
  ```jinja2
  {# Scalar string encoding #}
  [Search Logs](https://logs.internal/?query={{ payload.error | urlencode }})

  {# Dictionary parameter encoding #}
  [Grafana Dashboard](https://grafana.internal/d/alerts?{{ payload.dashboard_params | urlencode }})
  ```

#### Response Status Codes & Contracts

| HTTP Status | Description | Response Body Example |
| :--- | :--- | :--- |
| **`202 Accepted`** | Validated, rendered, and scheduled for in-process background delivery; destination receipt is not confirmed. | `{"status": "accepted", "route": "prometheus-in", "scheduled_sinks": ["matrix-ops", "slack-devs"]}` |
| **`401 Unauthorized`** | Missing or invalid `X-Secret-Token` header. | `{"detail": "Invalid or missing secret token"}` |
| **`404 Not Found`** | The requested `route_id` is not declared in configuration. | `{"detail": "Route 'unknown-route' not found"}` |
| **`413 Payload Too Large`** | Alert body exceeds 1 MiB (checked after authentication). | `{"detail": "Alert payload too large"}` |
| **`422 Unprocessable Entity`** | Invalid JSON, non-object JSON, or a nonempty body without a JSON Content-Type. | `{"detail": [{"loc": ["body"], "msg": "JSON decode error", "type": "json_invalid"}]}` |
| **`503 Service Unavailable`** | Insufficient delivery capacity for the complete fan-out; no body parsing, rendering, or sends. Retry later (`Retry-After: 1`). | `{"detail": "Delivery capacity exhausted"}` |
| **`500 Internal Server Error`** | Template render failure or timeout (>0.5s); delivery halted. | `{"detail": "Template rendering failed (SecurityError)"}` (timeout variant: `{"detail": "Template rendering timed out (>0.5s)"}`) |

Nonempty bodies require `application/json` or an `application/*+json` media type. Empty bodies and JSON `null` retain the default empty object. The 1 MiB limit applies to the received bytes, including when Content-Length is missing or understates the size. The limit bounds the application body buffer; it does not impose a request rate limit or a server-wide memory limit.

---

### 2. Health & Interactive Documentation

* **Health Probe:** `GET /health` -> `200 OK` `{"status": "ok"}`
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

### Delivery admission

```yaml
delivery:
  max_outstanding: 1000
  max_concurrency: 50
```

Both settings are positive integers, with the defaults above when omitted.
`max_outstanding` counts sink deliveries, not alerts: a route with three targets
reserves three units atomically. A route whose fan-out exceeds this budget is
rejected at startup. `max_concurrency` limits active sends, including retry waits.

The handler checks route and token first, then attempts admission without waiting.
At capacity it returns `503` with `Retry-After: 1`, even if the body would otherwise
be invalid or oversized. Nothing is partially scheduled. Callers should retry
with backoff; the header is advice, not a guarantee that space will be available.

The reservation covers body reading, rendering, response sending, and the entire
background fan-out. It is released on completion, errors, and cancellation.
A slow target conservatively holds the whole reservation until all targets finish.
Each worker has its own budget. This limits delivery work, not total server
memory, HTTP connections, request rate, or timed-out render threads; configure
server/proxy read timeouts and resource limits separately.

### Schema Rules & Validation
* **`apiVersion`**: Constrained via `Literal["v1alpha1"]`.
* **`templates[].engine`**: Constrained via `Literal["jinja2"]`.
* **`templates[].format`**: Constrained via `Literal["text", "markdown", "html"]`; setting `"html"` enables `org.matrix.custom.html` (`formatted_body`) in Matrix sinks.
* **`sinks[].type`**: Constrained via Pydantic `Literal["matrix", "webhook"]` to fail fast on typos during startup.
* **`sinks[].config.homeserver_url`**: Explicitly required for Matrix sinks to prevent domain-guessing vulnerabilities.
* **`sinks[].config.url`**: Required for Webhook sinks and must begin with `http://` or `https://`.
* **`routes[].template_ref`**: Must reference a declared template in `templates`.
* **`routes[].forward_to`**: Must contain at least one sink ID (`min_length=1`), and all referenced sink IDs must exist in `sinks`.
* **ID Uniqueness**: All IDs across `sinks`, `templates`, and `routes` are validated for uniqueness at startup to prevent shadowed routes or sink collisions.
* **Configuration Fields**: Models use `frozen=True` to prevent field reassignment, and `forward_to` is a tuple. Nested lists and dictionaries are not deeply immutable. Configuration is loaded at startup; there is no hot reload.
* **Whitespace Sanitization**: Nested string values inside `sinks[].config` (including headers and query dicts) are automatically stripped of leading and trailing whitespace.

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

### Credential Hygiene
* **Route Token Masking:** Route secrets use Pydantic's `SecretStr` to prevent plain-text token exposure in string representations or structured startup logs (`prometheus-in->**********`).
* **Query Parameter Scrubbing:** Any `token` query parameters sent to the ingress endpoint are scrubbed prior to populating the `params` dictionary in Jinja2 template contexts.
* **Payload Isolation on Failure:** Template rendering failures report only the exception class in logs and API responses, because raw exception messages can include sensitive payload values.
* **Rendered Payload & URL Redaction:** `bridge.dispatcher` logs only `message_len`, never raw rendered alert bodies, while sink transport errors are sanitized to avoid leaking credential-bearing webhook URLs.
* **Transport Logging:** Application startup sets `httpx` and `httpcore` loggers to `WARNING` to suppress request URLs and low-level DEBUG details. Keep these levels when supplying custom logging configuration. Configure reverse-proxy and server access logs separately to avoid recording sensitive query parameters.

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
uv sync --locked --all-groups
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

Each variable referenced by your configuration must also be passed into the
container. Host exports and a Compose `.env` file do not automatically populate
the container environment. For the example Matrix route, add these entries to
the service's `environment` section in `docker-compose.yml`:

```yaml
environment:
  - ALERT_BRIDGE_CONFIG=/app/config.yaml
  - MATRIX_TOKEN=${MATRIX_TOKEN:?Set MATRIX_TOKEN}
  - PROMETHEUS_SECRET_TOKEN=${PROMETHEUS_SECRET_TOKEN:?Set PROMETHEUS_SECRET_TOKEN}
```

```bash
# 1. Initialize configuration
cp config.example.yaml config.yaml

# 2. Set environment variables (or define in a .env file)
export MATRIX_TOKEN="your-matrix-token"
export PROMETHEUS_SECRET_TOKEN="supersecrettoken123"

# 3. Start container
docker compose up -d --build
```

The container runs as a non-root system user (`appuser:10001`), mounts `config.yaml` read-only (`:ro`), and monitors API health via `GET /health`. This health check does not verify downstream destinations or delivery success.
