from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import unquote

from fastapi import FastAPI, HTTPException

ROOT = Path(__file__).resolve().parents[1]


def _data_file() -> Path:
    """vendor_risk.json from the active data directory.

    Read on every request (the file is tiny) so an overlay dataset set through
    ``PROCUREMENT_DATA_DIR`` is honoured without restarting the service.
    """
    base = Path(os.getenv("PROCUREMENT_DATA_DIR") or ROOT / "data")
    return base / "vendor_risk.json"


def _load() -> dict:
    return json.loads(_data_file().read_text(encoding="utf-8"))


# Kept for backwards compatibility with code that imported DATA directly.
DATA = _load()

app = FastAPI(title="FDE Mock Vendor Risk API", version="1.1")


@app.get("/health")
def health() -> dict:
    return {"status": "ok", "service": "vendor-risk"}


@app.get("/vendor-risk/{vendor_name}")
def vendor_risk(vendor_name: str) -> dict:
    # FastAPI has already URL-decoded the path segment. The original starter
    # decoded it a second time, so a vendor literally named "Acme%20Corp" was
    # looked up as "Acme Corp". We use the value exactly once-decoded.
    name = vendor_name
    if "%" in name and _load().get(name) is None:
        # Tolerate clients that double-encode, but only as a fallback.
        name = unquote(name)
    record = _load().get(name)
    if record is None:
        raise HTTPException(status_code=404, detail=f"No vendor-risk record for '{name}'")
    if record.get("force_error"):
        raise HTTPException(status_code=503, detail=record.get("error_message", "Vendor-risk service unavailable"))
    return {"vendor_name": name, **record}
