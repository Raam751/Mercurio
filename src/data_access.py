"""Low-level data access for the procurement copilot.

Fixes over the original starter helpers:

* Blank CSV cells are returned as ``None`` instead of pandas ``NaN``. ``NaN`` is
  truthy in Python, so ``if row["security_review_date"]:`` silently treated a
  missing review date as present. Every loader now cleans that up.
* String values are whitespace-stripped, so ``" Approved "`` and ``"Approved"``
  compare equal.
* The data directory can be overridden with ``PROCUREMENT_DATA_DIR`` so the
  extended evaluation can run against an overlay dataset without touching the
  shipped files.
* The policy reference date is read from ``procurement_policy.md`` rather than
  hardcoded or taken from the computer's clock.
"""
from __future__ import annotations

import json
import os
import re
from datetime import date
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = ROOT / "data"
DATA_DIR = DEFAULT_DATA_DIR  # kept for backwards compatibility with the starter


def data_dir() -> Path:
    """Active data directory (``PROCUREMENT_DATA_DIR`` overrides the default)."""
    override = os.getenv("PROCUREMENT_DATA_DIR")
    return Path(override) if override else DEFAULT_DATA_DIR


def _clean_value(value: Any) -> Any:
    if isinstance(value, str):
        stripped = value.strip()
        return stripped or None
    return value


def _clean_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Replace NaN with None and strip strings, keeping the DataFrame shape."""
    df = df.astype(object).where(pd.notna(df), None)
    for column in df.columns:
        df[column] = df[column].map(_clean_value)
    return df


def _read_csv(name: str) -> pd.DataFrame:
    return _clean_frame(pd.read_csv(data_dir() / name))


def _records(name: str) -> list[dict[str, Any]]:
    """CSV rows as plain Python dicts (no NaN, no numpy scalar types)."""
    raw = pd.read_csv(data_dir() / name)
    rows = json.loads(raw.to_json(orient="records"))
    return [{k: _clean_value(v) for k, v in row.items()} for row in rows]


# --------------------------------------------------------------------------- #
# DataFrame loaders (original starter API, now NaN-safe)
# --------------------------------------------------------------------------- #
def load_employees() -> pd.DataFrame:
    return _read_csv("employees.csv")


def load_budgets() -> pd.DataFrame:
    return _read_csv("department_budgets.csv")


def load_software_catalog() -> pd.DataFrame:
    return _read_csv("software_catalog.csv")


def load_vendors() -> pd.DataFrame:
    return _read_csv("vendors.csv")


def load_purchase_history() -> pd.DataFrame:
    return _read_csv("purchase_history.csv")


# --------------------------------------------------------------------------- #
# Record loaders (preferred by the copilot tools)
# --------------------------------------------------------------------------- #
def employee_records() -> list[dict[str, Any]]:
    return _records("employees.csv")


def budget_records() -> list[dict[str, Any]]:
    return _records("department_budgets.csv")


def catalog_records() -> list[dict[str, Any]]:
    return _records("software_catalog.csv")


def vendor_records() -> list[dict[str, Any]]:
    return _records("vendors.csv")


def purchase_records() -> list[dict[str, Any]]:
    return _records("purchase_history.csv")


def load_requests() -> list[dict]:
    return json.loads((data_dir() / "requests.json").read_text(encoding="utf-8"))


def get_request(request_id: str) -> dict:
    for request in load_requests():
        if request.get("request_id") == request_id:
            return request
    raise KeyError(f"Unknown request_id: {request_id}")


# --------------------------------------------------------------------------- #
# Policy text
# --------------------------------------------------------------------------- #
def load_policy_text() -> str:
    return (data_dir() / "procurement_policy.md").read_text(encoding="utf-8")


_REFERENCE_DATE = re.compile(r"reference date:\**\s*(\d{4}-\d{2}-\d{2})", re.IGNORECASE)
_SECTION = re.compile(r"^##\s+(\d+)\.\s+(.+?)\s*$", re.MULTILINE)


def get_reference_date() -> date:
    """Evaluation reference date declared in the policy (never the system clock)."""
    match = _REFERENCE_DATE.search(load_policy_text())
    if not match:
        raise ValueError("procurement_policy.md does not declare a reference date")
    return date.fromisoformat(match.group(1))


def policy_sections() -> dict[int, dict[str, str]]:
    """Policy split into numbered sections: {5: {"title": ..., "text": ...}}."""
    text = load_policy_text()
    matches = list(_SECTION.finditer(text))
    sections: dict[int, dict[str, str]] = {}
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        sections[int(match.group(1))] = {
            "title": match.group(2),
            "text": text[match.end():end].strip(),
        }
    return sections
