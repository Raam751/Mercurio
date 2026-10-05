"""Client for the vendor-risk service.

The starter's ``get_vendor_risk`` raised the same kind of exception for three
situations the policy treats differently:

* the service is up but has no record for the vendor (HTTP 404) -> the vendor
  security assessment is *missing*;
* the service reports an outage (HTTP 5xx) or cannot be reached -> the evidence
  is *unavailable* and must not be assumed favourable (policy section 10);
* the response is malformed -> treat as unavailable.

``fetch_vendor_risk`` returns a structured result instead of raising, so the
copilot can apply the right rule. ``get_vendor_risk`` is kept unchanged for
compatibility.
"""
from __future__ import annotations

import os
import time
from typing import Any
from urllib.parse import quote

import requests

from src.mock_service import ensure_mock_api

EXPECTED_FIELDS = {"security_review_status", "last_review_date", "risk_level"}


def base_url() -> str:
    return os.getenv("VENDOR_RISK_BASE_URL", "http://127.0.0.1:8001").rstrip("/")


def get_vendor_risk(vendor_name: str, timeout_seconds: float = 3.0) -> dict:
    """Original low-level client (raises on any HTTP or network error)."""
    url = f"{base_url()}/vendor-risk/{quote(vendor_name, safe='')}"
    response = requests.get(url, timeout=timeout_seconds)
    response.raise_for_status()
    return response.json()


def fetch_vendor_risk(vendor_name: str, timeout_seconds: float = 3.0, retries: int = 1) -> dict[str, Any]:
    """Structured lookup that never raises.

    Returns ``{"status": "ok" | "not_found" | "unavailable", ...}`` plus the
    endpoint, HTTP status, a reason, the record (when ok) and attempt count.
    """
    root = base_url()
    service_state = ensure_mock_api(root)
    endpoint = f"/vendor-risk/{quote(vendor_name, safe='')}"
    url = f"{root}{endpoint}"
    result: dict[str, Any] = {"endpoint": endpoint, "service_state": service_state}

    attempts = 0
    while True:
        attempts += 1
        try:
            response = requests.get(url, timeout=timeout_seconds)
        except requests.Timeout:
            outcome = {"status": "unavailable", "http_status": None, "reason": "request timed out"}
        except requests.ConnectionError:
            outcome = {"status": "unavailable", "http_status": None, "reason": "service unreachable"}
        else:
            outcome = _interpret(response)
        retryable = outcome["status"] == "unavailable"
        if not retryable or attempts > retries:
            break
        time.sleep(0.2 * attempts)

    result.update(outcome)
    result["attempts"] = attempts
    return result


def _interpret(response: requests.Response) -> dict[str, Any]:
    detail = _detail(response)
    if response.status_code == 200:
        try:
            record = response.json()
        except ValueError:
            return {"status": "unavailable", "http_status": 200, "reason": "malformed JSON response"}
        if not isinstance(record, dict) or not EXPECTED_FIELDS & set(record):
            return {"status": "unavailable", "http_status": 200, "reason": "response missing assessment fields"}
        return {"status": "ok", "http_status": 200, "reason": None, "record": record}
    if response.status_code == 404:
        return {"status": "not_found", "http_status": 404, "reason": detail or "no vendor-risk record"}
    return {"status": "unavailable", "http_status": response.status_code, "reason": detail or "service error"}


def _detail(response: requests.Response) -> str | None:
    try:
        body = response.json()
    except ValueError:
        return None
    return body.get("detail") if isinstance(body, dict) else None
