# Local saturation and load check

From a checkout with the locked development dependencies installed:

```bash
uv sync --locked --all-groups
uv run --locked python examples/local-demo/load.py
```

The command runs one bridge worker and one HTTP receiver on automatically
assigned loopback ports. It uses a generated token, a temporary configuration
containing environment placeholders, and synthetic alert text. It bypasses proxy
routing inside this process. No external account or destination is used.

## Functional saturation check

The receiver initially withholds acknowledgments behind a gate. The script fills
the outstanding-delivery budget and verifies that the next alert receives HTTP
503 with Retry-After: 1. Health and metrics remain available. It then releases
the receiver, waits for the budget to drain, and checks that a new alert receives
202 and reaches the destination.

This gate makes saturation deterministic instead of assuming a particular sleep
or request rate will fill the service. The gate has a safety timeout; unusually
slow setup can fail the check rather than hanging indefinitely.

## Measured burst

After recovery, a thread pool sends a finite burst with the specified client
concurrency. The receiver delays each successful acknowledgment. The script
requires only 202/503 outcomes and verifies the exact multiset of rendered
messages against all accepted alerts. It also reconciles accepted, overloaded,
and successful-delivery metrics, checks the receiver's peak work concurrency,
and requires the outstanding/active gauges to return to zero.

Defaults are 200 burst requests, 16 client threads, an outstanding budget of 8,
2 concurrent sends, and a receiver delay of 0.05 seconds. The workload is designed
to exercise overload, not maximize accepted throughput.

```bash
uv run --locked python examples/local-demo/load.py \
  --requests 200 --concurrency 16 --capacity 8 --send-concurrency 2 --delay 0.05
```

The JSON output includes environment and dependency versions, a digest of the
application Python source, parameters, status counts, ingress p50/p95 by status,
elapsed times, confirmed deliveries, and receiver peak concurrency. Redirect
stdout to retain a result:

```bash
uv run --locked python examples/local-demo/load.py > local-load-results.json
```

## Interpretation

This is a finite closed-loop workload: at most the requested number of client
threads issue requests concurrently. The generator, bridge, and receiver share
the same machine. It is not an open-loop traffic generator, a memory profiler,
a production capacity estimate, or a durability/restart test.

Ingress latency measures time to read an API response. A fast 503 is not a
successful delivery. Delivery throughput includes the post-burst drain period;
delivery histograms also include semaphore waits and retries. Reported CPU count
is Python's logical CPU count and may not reflect container CPU quotas.

The [recorded reference run](load-test-results.json) is one actual run in the
development environment on 2026-11-05. Status counts and timings can vary on
another run. All accepted messages in that run were confirmed; the rejected
messages were intentionally not sent. The result is evidence for that workload,
not an SLA or a claim about maximum service throughput.

CI runs a smaller functional check:

```bash
uv run --locked python examples/local-demo/load.py \
  --requests 40 --concurrency 8 --capacity 4 --delay 0.01
```

CI has no throughput or latency threshold. It checks saturation, recovery,
message/metric reconciliation, and cleanup. The script exits nonzero if those
checks fail and shuts down its local servers in a finally block.
