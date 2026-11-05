"""Run a local alert through the real API, template engine, and webhook sink."""

from __future__ import annotations

import json
import os
import queue
import secrets
import socket
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener

import uvicorn


def run_demo() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(repo_root))
    received: queue.Queue[dict] = queue.Queue()

    class Receiver(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers["Content-Length"]))
            received.put(json.loads(body))
            self.send_response(204)
            self.end_headers()

        def log_message(self, format: str, *args) -> None:
            pass

    receiver = ThreadingHTTPServer(("127.0.0.1", 0), Receiver)
    receiver_thread = threading.Thread(target=receiver.serve_forever, daemon=True)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(128)

    # These settings apply only to this demo process. No config.yaml is created.
    token = secrets.token_urlsafe(32)
    os.environ["ALERT_BRIDGE_CONFIG"] = str(Path(__file__).with_name("bridge.yaml"))
    os.environ["DEMO_ROUTE_TOKEN"] = token
    os.environ["DEMO_WEBHOOK_URL"] = f"http://127.0.0.1:{receiver.server_port}/webhook"
    os.environ["NO_PROXY"] = "*"
    os.environ["no_proxy"] = "*"

    server = uvicorn.Server(uvicorn.Config("app.main:app", log_level="warning", lifespan="on"))
    bridge_thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    opener = build_opener(ProxyHandler({}))

    receiver_thread.start()
    bridge_thread.start()
    try:
        deadline = time.monotonic() + 10
        while True:
            try:
                with opener.open(f"{base_url}/health", timeout=0.5) as response:
                    if response.status == 200:
                        break
            except (URLError, TimeoutError):
                pass
            if not bridge_thread.is_alive() or time.monotonic() >= deadline:
                raise RuntimeError("The local bridge did not become healthy within 10 seconds")
            time.sleep(0.05)

        url = f"{base_url}/alerts/local-demo?env=local"
        unauthorized = Request(url, data=b"{malformed json", headers={"Content-Type": "application/json"})
        try:
            with opener.open(unauthorized, timeout=5):
                raise RuntimeError("The bridge accepted a request without a token")
        except HTTPError as exc:
            if exc.code != 401:
                raise RuntimeError(f"Expected HTTP 401, received {exc.code}") from exc
            exc.close()
        print("1. Request without a token rejected (401).")

        payload = {"message": "CPU usage > 90%", "host": "demo-host"}
        request = Request(
            url,
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "X-Secret-Token": token},
        )
        with opener.open(request, timeout=5) as response:
            if response.status != 202:
                raise RuntimeError(f"Expected HTTP 202, received {response.status}")
        print("2. Authenticated alert accepted (202).")

        try:
            delivered = received.get(timeout=5)
        except queue.Empty as exc:
            raise RuntimeError("The local webhook receiver did not receive an alert") from exc
        expected = {"text": "Alert: CPU usage > 90%\nHost: demo-host\nEnvironment: local"}
        if delivered != expected:
            raise RuntimeError(f"Unexpected webhook body: {delivered!r}")
        print("3. Local receiver confirmed the rendered message:")
        print(json.dumps(delivered, indent=2))
        print("Demo passed.")
    finally:
        server.should_exit = True
        bridge_thread.join(timeout=5)
        receiver.shutdown()
        receiver.server_close()
        receiver_thread.join(timeout=2)
        listener.close()
        if bridge_thread.is_alive():
            raise RuntimeError("The local bridge did not shut down cleanly")


if __name__ == "__main__":
    try:
        run_demo()
    except Exception as exc:
        print(f"Demo failed: {exc}", file=sys.stderr)
        sys.exit(1)
