"""Start the local mock vendor-risk API on demand.

Problem in the starter pack: ``evals/run_public_evals.py`` calls
``handle_request`` directly, but nothing starts the mock API. Unless
``run_local.py`` happens to be running in another terminal, every vendor lookup
fails with a connection error. That made a missing *local dev process* look the
same as a genuine vendor-service outage.

Fix: when the configured vendor-risk URL points at localhost and nothing is
listening, start the mock API in a background thread of the current process.

* A real outage (HTTP 503 from a running service) is NOT masked. Only "nothing
  is listening on localhost" triggers the auto-start.
* Remote URLs are never auto-started.
* Set ``AUTO_START_MOCK_API=0`` to disable it (used to test the "service down"
  path).
"""
from __future__ import annotations

import os
import threading
import time
from urllib.parse import urlparse

import requests

_LOCAL_HOSTS = {"127.0.0.1", "localhost"}
_lock = threading.Lock()
_started_ports: set[int] = set()


def _healthy(base_url: str) -> bool:
    try:
        response = requests.get(f"{base_url}/health", timeout=0.5)
        return response.ok and response.json().get("status") == "ok"
    except (requests.RequestException, ValueError):
        return False


def auto_start_enabled() -> bool:
    return os.getenv("AUTO_START_MOCK_API", "1").strip().lower() not in {"0", "false", "no", "off"}


def ensure_mock_api(base_url: str, wait_seconds: float = 6.0) -> str:
    """Make sure the local mock API is reachable.

    Returns one of: ``running``, ``started``, ``disabled``, ``remote``, ``failed``.
    """
    parsed = urlparse(base_url)
    if parsed.hostname not in _LOCAL_HOSTS:
        return "remote"
    if _healthy(base_url):
        return "running"
    if not auto_start_enabled():
        return "disabled"

    port = parsed.port or 80
    with _lock:
        if _healthy(base_url):
            return "running"
        if port not in _started_ports:
            import uvicorn  # imported lazily: only needed when we start the service

            from mock_api.app import app

            config = uvicorn.Config(app, host=parsed.hostname, port=port, log_level="warning", lifespan="off")
            server = uvicorn.Server(config)
            thread = threading.Thread(target=server.run, name=f"mock-vendor-api-{port}", daemon=True)
            thread.start()
            _started_ports.add(port)

        deadline = time.monotonic() + wait_seconds
        while time.monotonic() < deadline:
            if _healthy(base_url):
                return "started"
            time.sleep(0.1)
    return "failed"
