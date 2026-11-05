# Design decisions and operating limits

Alert Bridge receives authenticated alerts, renders configured templates, and
forwards messages to Matrix rooms or HTTP webhooks. The current design favors
a small service with no external stateful infrastructure. Its delivery contract
is best effort.

This document describes the implementation as it stands. Proposed extensions
below are not implemented features.

## Async HTTP and a shared client

FastAPI provides the ingress API, and HTTPX handles outbound HTTP. A shared
`AsyncClient` is created during application startup and closed during shutdown,
so sends can reuse connections instead of creating a client for each alert.

Network waits can overlap through async I/O. This does not establish a throughput
or latency guarantee: no reproducible load-test results are published yet.

## Configuration is loaded at startup

YAML defines routes, templates, and sinks. Environment variables supply values
such as tokens. Pydantic validates required fields, supported values, unique
IDs, and references before the application starts serving traffic.

Configuration files and templates are trusted administrator input. They are
not accepted from incoming webhook callers. Models prevent field reassignment,
but their nested dictionaries and lists are not deeply immutable. There is no
configuration-management API or hot reload; changes require a restart.

For containers, explicitly pass every referenced environment variable into the
container. Shell exports and Compose interpolation alone do not do that.

## Authentication precedes body parsing

The ingress path performs route lookup, checks `X-Secret-Token` with
`hmac.compare_digest`, and then reads and validates the body. Unknown routes
return 404 and invalid tokens return 401 without consuming the body in the
application handler.

The application checks both declared Content-Length and actual streamed bytes
against a 1 MiB limit before decoding JSON. This limits the application body
buffer, not decoded-object memory, allocations already made by the HTTP server,
or aggregate memory across concurrent requests. It is not rate limiting.

Nonempty bodies require a JSON media type. Empty bodies and JSON `null` use an
empty object for compatibility with the original optional-body behavior.

## Templates run before acknowledgment

Templates receive the alert payload and query parameters, with the `token`
query parameter removed. Jinja's sandbox restricts attribute and method access;
it is defense in depth for trusted templates, not a resource-isolation boundary.
Missing nested fields can render as empty text through `ChainableUndefined`.
That convenience does not validate the business meaning of an alert.

Rendering runs in a worker thread. The application stops waiting for the result
after 0.5 seconds and returns an error, but Python does not terminate that
thread. Concurrent expensive renders can still consume resources. Deployment
resource limits and trusted template review remain necessary.

Template errors return and log only their exception class, because raw exception
messages can contain payload values. A render failure prevents dispatch.

## What HTTP 202 means

HTTP 202 means the alert was validated, rendered, and scheduled for background
delivery in the current process. It does not mean any destination received or
stored the message.

FastAPI background tasks keep downstream network waits out of the normal
response path. The trade-off is that there is no durable record of accepted
work, no delivery-status endpoint, and no restart recovery or dead-letter store.
A crash or forced shutdown can lose work after the caller receives 202.

This mode fits notifications where occasional loss is acceptable. Workloads
requiring recovery of every accepted alert need a persistent delivery design.

## Bounded admission and send concurrency

The service reserves the entire route fan-out before reading the authenticated
request body. `delivery.max_outstanding` defaults to 1,000 sink deliveries per
process and includes reservations, pending sends, and active sends. Admission
never waits: if the whole fan-out will not fit, HTTP 503 and Retry-After: 1 are
returned without parsing, rendering, or scheduling any sends. Route lookup and
authentication still take precedence. A route larger than the budget is a
configuration error at startup.

Reservations are synchronous event-loop operations, so checking and incrementing
the count cannot interleave with another request. They are owned by one worker,
not shared across threads or processes. A response-level finally block releases
capacity even if response sending fails or delivery is cancelled. Exceptions
before a response is created release capacity in the handler.

The full reservation is held until all targets finish. This is conservative: one
slow target retains units already used by faster siblings. It avoids partial
fan-out admission and gives callers one predictable overload response.

`delivery.max_concurrency` defaults to 50 active sink sends, including retry
backoff waits. The existing 35-second per-target timeout still includes waiting
for a slot. It relies on cooperative asyncio cancellation, so it is not a hard
wall-clock SLA. The outstanding budget bounds the number of waiting deliveries;
it does not remove the possibility of a queue-wait timeout.

Each worker has its own limits. This is not a global memory or request-rate cap,
and an authenticated slow body reader can occupy a reservation. Deployment
read timeouts and resource limits remain necessary. Timed-out render threads
can continue after their request releases its reservation.

Fan-out collects a result for each target without aborting siblings on ordinary
failures. The background caller does not persist those results. There are logs
but no built-in delivery metrics or status API yet.

## Retries and duplicates

The sinks make up to three attempts for selected network failures, HTTP 5xx,
and HTTP 429. Other HTTP 4xx responses fail without retrying. Numeric
Retry-After values are clamped to two seconds; HTTP-date Retry-After values
fall back to the short exponential backoff. These are small in-process retry
loops, not persistent retry scheduling.

If a destination processes a request but its response is lost, retrying may
produce a duplicate. The generic webhook sink sends no stable idempotency key.
Matrix reuses a transaction ID within a single send's retry loop. A repeated
ingress request creates another send with a new transaction ID; it is not
deduplicated against the earlier alert.

The service therefore provides no exactly-once or guaranteed-delivery contract.

## Message formats and logging

The HTTP sink constructs JSON using the configured text field and extra fields.
Matrix sends plain text for `text` and `markdown`; it does not convert Markdown
to HTML. For `html`, it includes Matrix's formatted HTML fields. Administrators
must select a format and template appropriate for the destination.

Application logs retain sink IDs, message length, HTTP status codes, and safe
error categories. They omit rendered messages and raw exception text. Startup
sets HTTPX and httpcore logging to WARNING. Custom logging configurations must
preserve those levels; proxy and HTTP server access logs are configured
separately. The health endpoint checks API availability, not delivery health.

## What the local demo proves

The [local demo](../examples/local-demo/run.py) starts a real API server and a
real HTTP receiver on loopback sockets. It checks rejection of an unauthenticated
request, a 202 response to an authenticated alert, and the exact rendered JSON
received by the destination. It uses an ephemeral token and shuts down its
servers afterward.

This verifies the basic HTTP path without an external account. It does not test
Matrix delivery, sustained load, crash recovery, or production deployment
settings. CI runs the same command alongside the existing test suite.

## Possible next steps

Delivery metrics would make overload and failures easier to measure. A reproducible load test should establish actual
throughput, latency, and resource use before making performance claims.

Durable delivery is a separate architectural extension. It would require a
persistent accepted-work record, recovery and acknowledgment rules, scheduled
retries, duplicate handling, and tests that interrupt and restart the service.
Adding a queue alone would not resolve all of those responsibilities.
