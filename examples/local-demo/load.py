"""Verify saturation/recovery, then measure a finite local concurrent burst."""
from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
from pathlib import Path
import platform
import secrets
import socket
import sys
import tempfile
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

import httpx
import uvicorn
import yaml


def positive(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def run(args):
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root))
    gate = threading.Event()
    lock = threading.Lock()
    delivered = []
    active = peak = 0

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self):
            nonlocal active, peak
            data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            with lock:
                active += 1
                peak = max(peak, active)
            try:
                if not gate.wait(8):
                    status = 503
                else:
                    time.sleep(args.delay)
                    with lock:
                        delivered.append(data["text"])
                    status = 204
            finally:
                with lock:
                    active -= 1
            # Finish measuring receiver work before acknowledging to the sender.
            self.send_response(status)
            self.end_headers()

        def log_message(self, format, *values):
            pass

    receiver = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)
    base = f"http://127.0.0.1:{listener.getsockname()[1]}"
    token = secrets.token_urlsafe(32)
    os.environ.update(DEMO_ROUTE_TOKEN=token, DEMO_WEBHOOK_URL=f"http://127.0.0.1:{receiver.server_port}/webhook",
                      NO_PROXY="*", no_proxy="*")

    def request(path, payload=None, authorized=True):
        headers = {"Content-Type": "application/json"}
        if authorized:
            headers["X-Secret-Token"] = token
        data = json.dumps(payload).encode() if payload is not None else None
        started = time.perf_counter()
        try:
            response = build_opener(ProxyHandler({})).open(Request(base + path, data=data, headers=headers), timeout=10)
        except HTTPError as exc:
            response = exc
        with response:
            body = response.read().decode()
            if response.status == 503 and response.headers.get("Retry-After") != "1":
                raise RuntimeError("Overload response lacks Retry-After: 1")
            return response.status, body, time.perf_counter() - started

    def metrics():
        code, body, _ = request("/metrics")
        if code != 200:
            raise RuntimeError("Metrics scrape failed")
        return {line.rsplit(" ", 1)[0]: float(line.rsplit(" ", 1)[1])
                for line in body.splitlines() if line and not line.startswith("#")}

    def drain(expected):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            values = metrics()
            if values["alert_bridge_delivery_outstanding"] == 0:
                with lock:
                    actual = list(delivered)
                if Counter(actual) != Counter(expected):
                    raise RuntimeError("Received messages differ from accepted alerts")
                if values['alert_bridge_deliveries_total{sink_id="local-receiver",outcome="success"}'] != len(expected):
                    raise RuntimeError("Success metrics do not match receiver confirmations")
                return values
            time.sleep(0.01)
        raise RuntimeError("Deliveries did not drain within 20 seconds")

    def post(name):
        return request("/alerts/local-demo?env=local", {"message": name, "host": "load-host"})

    def rendered(name):
        return f"Alert: {name}\nHost: load-host\nEnvironment: local"

    with tempfile.TemporaryDirectory() as temporary:
        config = yaml.safe_load(Path(__file__).with_name("bridge.yaml").read_text())
        config["delivery"] = {"max_outstanding": args.capacity, "max_concurrency": args.send_concurrency}
        config_path = Path(temporary) / "bridge.yaml"
        config_path.write_text(yaml.safe_dump(config))  # Contains placeholders, not the generated token.
        os.environ["ALERT_BRIDGE_CONFIG"] = str(config_path)
        server = uvicorn.Server(uvicorn.Config("app.main:app", log_level="warning", lifespan="on"))
        receiver_thread = threading.Thread(target=receiver.serve_forever, daemon=True)
        bridge_thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        receiver_thread.start()
        bridge_thread.start()
        try:
            deadline = time.monotonic() + 10
            while True:
                try:
                    if request("/health")[0] == 200:
                        break
                except (URLError, TimeoutError):
                    pass
                if not bridge_thread.is_alive() or time.monotonic() > deadline:
                    raise RuntimeError("Bridge startup failed")
                time.sleep(0.05)

            expected = []
            for i in range(args.capacity):
                name = f"fill-{i}"
                if post(name)[0] != 202:
                    raise RuntimeError("Could not fill the admission budget")
                expected.append(rendered(name))
            saturated = metrics()
            if saturated["alert_bridge_delivery_outstanding"] != args.capacity or post("must-reject")[0] != 503:
                raise RuntimeError("Saturation did not reject a complete alert")
            if request("/health")[0] != 200 or request("/metrics")[0] != 200:
                raise RuntimeError("Operational endpoints unavailable during saturation")
            gate.set()
            drain(expected)
            if post("recovery")[0] != 202:
                raise RuntimeError("Admission did not recover after draining")
            expected.append(rendered("recovery"))
            drain(expected)

            started = time.perf_counter()
            with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
                results = list(pool.map(lambda i: post(f"burst-{i}"), range(args.requests)))
            burst_seconds = time.perf_counter() - started
            counts = Counter(code for code, _, _ in results)
            if set(counts) - {202, 503} or not counts[202]:
                raise RuntimeError(f"Unexpected burst outcomes: {dict(counts)}")
            expected.extend(rendered(f"burst-{i}") for i, (code, _, _) in enumerate(results) if code == 202)
            final = drain(expected)
            completion_seconds = time.perf_counter() - started
            if final['alert_bridge_alerts_total{outcome="accepted"}'] != len(expected):
                raise RuntimeError("Admission metrics do not match accepted messages")
            if final['alert_bridge_alerts_total{outcome="overloaded"}'] != counts[503] + 1:
                raise RuntimeError("Overload counter mismatch")
            if final["alert_bridge_delivery_active"] != 0 or peak > args.send_concurrency:
                raise RuntimeError("Active send limit or cleanup failed")

            latency = {}
            for code in (202, 503):
                values = sorted(seconds * 1000 for status, _, seconds in results if status == code)
                latency[str(code)] = {f"p{p}": round(values[math.ceil(p / 100 * len(values)) - 1], 3)
                                      if values else None for p in (50, 95)}
            digest = hashlib.sha256()
            for path in sorted((root / "app").rglob("*.py")):
                digest.update(path.relative_to(root).as_posix().encode() + b"\0" + path.read_bytes())
            return {
                "checks": {"saturation_503": True, "recovery_202": True, "all_accepted_received": True,
                           "gauges_drained": True},
                "environment": {"python": platform.python_version(), "platform": platform.platform(),
                                "logical_cpus": os.cpu_count(), "httpx": httpx.__version__, "uvicorn": uvicorn.__version__},
                "application_sha256": digest.hexdigest(),
                "parameters": vars(args), "burst_status_counts": {str(k): v for k, v in sorted(counts.items())},
                "burst_seconds": round(burst_seconds, 4), "burst_plus_drain_seconds": round(completion_seconds, 4),
                "offered_requests_per_second": round(args.requests / burst_seconds, 2),
                "accepted_deliveries_per_second_including_drain": round(counts[202] / completion_seconds, 2),
                "ingress_latency_ms": latency, "receiver_peak_concurrency": peak,
                "total_confirmed_messages_including_checks": len(expected),
            }
        finally:
            gate.set()
            server.should_exit = True
            bridge_thread.join(timeout=10)
            receiver.shutdown()
            receiver.server_close()
            receiver_thread.join(timeout=2)
            listener.close()
            if bridge_thread.is_alive():
                raise RuntimeError("Bridge did not shut down cleanly")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=positive, default=200)
    parser.add_argument("--concurrency", type=positive, default=16)
    parser.add_argument("--capacity", type=positive, default=8)
    parser.add_argument("--send-concurrency", type=positive, default=2)
    parser.add_argument("--delay", type=float, default=0.05, help="Receiver delay in seconds")
    args = parser.parse_args()
    if not math.isfinite(args.delay) or args.delay < 0 or args.capacity > 64:
        parser.error("delay must be finite and nonnegative; capacity must be at most 64")
    try:
        print(json.dumps(run(args), indent=2))
    except Exception as exc:
        print(f"Load check failed: {exc}", file=sys.stderr)
        sys.exit(1)
