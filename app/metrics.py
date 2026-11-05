"""Per-worker Prometheus text metrics, updated on the ASGI event loop.

Format: https://prometheus.io/docs/instrumenting/exposition_formats/
Only configured sink IDs and fixed outcome/reason labels are exported.
"""
from __future__ import annotations

BUCKETS = (0.01, 0.05, 0.1, 0.5, 1.0, 5.0, 10.0, 35.0, float("inf"))
OUTCOMES = ("success", "failure", "timeout", "cancelled")


def labels(**values: str) -> str:
    def escape(value: str) -> str:
        return value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
    return "{" + ",".join(f'{key}="{escape(value)}"' for key, value in values.items()) + "}"


class DeliveryMetrics:
    def __init__(self, sink_ids) -> None:
        self.alerts = {"accepted": 0, "overloaded": 0}
        self.active = 0
        self.sinks = {
            sink_id: {"outcomes": dict.fromkeys(OUTCOMES, 0), "retries": {"http": 0, "network": 0},
                      "buckets": [0] * len(BUCKETS), "sum": 0.0, "count": 0}
            for sink_id in sorted(sink_ids)
        }

    def record_delivery(self, sink_id: str, outcome: str, seconds: float) -> None:
        data = self.sinks.get(sink_id)
        if data is None:
            return  # Never create series from unconfigured identifiers.
        data["outcomes"][outcome] += 1
        data["sum"] += seconds
        data["count"] += 1
        for i, bound in enumerate(BUCKETS):
            if seconds <= bound:
                data["buckets"][i] += 1

    def retry(self, sink_id: str, reason: str) -> None:
        if sink_id in self.sinks:
            self.sinks[sink_id]["retries"][reason] += 1

    def render(self, *, capacity: int, outstanding: int, concurrency: int) -> str:
        lines = []

        def family(name, kind, help_text):
            name = "alert_bridge_" + name
            lines.extend([f"# HELP {name} {help_text}", f"# TYPE {name} {kind}"])
            return name

        for name, value, help_text in [
            ("delivery_capacity", capacity, "Per-worker outstanding delivery budget."),
            ("delivery_outstanding", outstanding, "Reserved, waiting and active sink deliveries."),
            ("delivery_active", self.active, "Sink sends holding a semaphore slot, including retry waits."),
            ("send_concurrency_limit", concurrency, "Per-worker active send limit."),
        ]:
            name = family(name, "gauge", help_text)
            lines.append(f"{name} {value}")
        name = family("alerts_total", "counter", "Alerts scheduled or rejected for overload; excludes other ingress errors.")
        for outcome, count in self.alerts.items():
            lines.append(f"{name}{labels(outcome=outcome)} {count}")
        name = family("deliveries_total", "counter", "Final outcomes of started target dispatches.")
        for sink_id, data in self.sinks.items():
            for outcome, count in data["outcomes"].items():
                lines.append(f"{name}{labels(sink_id=sink_id, outcome=outcome)} {count}")
        name = family("retries_scheduled_total", "counter", "Retries scheduled before backoff; cancellation may prevent the next attempt.")
        for sink_id, data in self.sinks.items():
            for reason, count in data["retries"].items():
                lines.append(f"{name}{labels(sink_id=sink_id, reason=reason)} {count}")
        name = family("delivery_duration_seconds", "histogram", "Completed target duration including semaphore waiting and retries.")
        for sink_id, data in self.sinks.items():
            for bound, count in zip(BUCKETS, data["buckets"]):
                le = "+Inf" if bound == float("inf") else str(bound)
                lines.append(f"{name}_bucket{labels(sink_id=sink_id, le=le)} {count}")
            lines.append(f"{name}_sum{labels(sink_id=sink_id)} {data['sum']}")
            lines.append(f"{name}_count{labels(sink_id=sink_id)} {data['count']}")
        return "\n".join(lines) + "\n"
