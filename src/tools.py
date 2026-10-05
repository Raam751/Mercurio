"""Copilot tools.

Every tool is plain Python. Each returns a ``ToolResult`` with structured data
and the evidence items shown to the user, so every evidence line can be traced
back to a record ID, policy section or API endpoint.

| Tool                         | Kind           | Source                          |
|------------------------------|----------------|---------------------------------|
| get_request_details          | deterministic  | requests.json + employees.csv   |
| check_department_budget      | deterministic  | department_budgets.csv          |
| search_software_catalog      | deterministic  | software_catalog.csv            |
| get_purchase_history         | deterministic  | purchase_history.csv            |
| get_vendor_registry_record   | deterministic  | vendors.csv                     |
| get_vendor_risk_assessment   | external API   | mock vendor-risk service        |
| lookup_policy_sections       | retrieval      | procurement_policy.md           |
| screen_untrusted_text        | deterministic  | all free-text business fields   |
| apply_policy_rules           | deterministic  | policy engine (src/policy.py)   |

Tool arguments that identify records (requester, vendor, department) are bound
from the request by code, never taken from the LLM. The LLM can only choose
which tools run and supply search keywords / policy section numbers.
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable

from src import data_access
from src.contracts import EvidenceItem
from src.policy import annual_amount
from src.screening import screen_fields
from src.vendor_client import fetch_vendor_risk


@dataclass
class ToolResult:
    tool: str
    ok: bool
    data: dict[str, Any] | None
    evidence: list[EvidenceItem] = field(default_factory=list)
    error: str | None = None
    latency_ms: float = 0.0
    requested_by: str = "system"  # system | agent | guardrail


@dataclass
class ToolSpec:
    name: str
    description: str
    kind: str
    args: dict[str, str]
    func: Callable[..., ToolResult]
    mandatory: bool


def _money(value: Any) -> str:
    try:
        return f"${Decimal(str(value)):,.0f}"
    except Exception:  # noqa: BLE001
        return str(value)


def _tokens(text: Any) -> set[str]:
    stop = {"the", "and", "for", "pro", "plus", "team", "teams", "enterprise", "business", "advanced",
            "suite", "add", "on", "pack", "expansion", "workspace", "with", "of", "a", "an", "to"}
    return {t for t in re.findall(r"[a-z0-9]+", str(text or "").lower()) if len(t) > 2 and t not in stop}


def _norm(text: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


# --------------------------------------------------------------------------- #
# Tools
# --------------------------------------------------------------------------- #
def get_request_details(ctx: dict) -> ToolResult:
    request = data_access.get_request(ctx["request_id"])
    employees = {e["employee_id"]: e for e in data_access.employee_records()}
    requester = employees.get(request.get("requester_id"))
    manager = employees.get(requester["manager_id"]) if requester and requester.get("manager_id") else None
    dept_head = None
    if requester:
        same_dept = [e for e in employees.values() if e["department"] == requester["department"]
                     and str(e.get("level", "")).lower() in {"director", "vp", "head"}]
        dept_head = same_dept[0] if same_dept else (manager if manager and
                                                     str(manager.get("level", "")).lower() in {"director", "vp"} else None)
    ctx["request"] = request
    ctx["requester"] = requester
    evidence = []
    if requester:
        evidence.append(EvidenceItem(
            source="get_request_details",
            finding=f"Requester {requester['name']} ({requester['employee_id']}) is in {requester['department']}; "
                    f"manager {manager['name'] if manager else 'not on record'}.",
            reference=f"employees.csv:{requester['employee_id']}"))
    else:
        evidence.append(EvidenceItem(
            source="get_request_details",
            finding=f"Requester ID '{request.get('requester_id')}' is not in the employee directory.",
            reference="employees.csv"))
    return ToolResult("get_request_details", True,
                      {"request": request, "requester": requester, "manager": manager, "department_head": dept_head},
                      evidence)


def check_department_budget(ctx: dict) -> ToolResult:
    requester = ctx.get("requester")
    amount, _problem, _basis = annual_amount(ctx["request"])
    if not requester:
        return ToolResult("check_department_budget", True, {"status": "unknown_requester", "department": None},
                          [EvidenceItem(source="check_department_budget",
                                        finding="Budget not checked: requester department is unknown.",
                                        reference="department_budgets.csv")])
    dept = requester["department"]
    row = next((r for r in data_access.budget_records() if r["department"] == dept), None)
    if row is None:
        return ToolResult("check_department_budget", True, {"status": "unknown_department", "department": dept},
                          [EvidenceItem(source="check_department_budget",
                                        finding=f"No software budget record exists for {dept}.",
                                        reference="department_budgets.csv")])
    available = Decimal(str(row["available_usd"]))
    consistent = Decimal(str(row["annual_software_budget_usd"])) - Decimal(str(row["committed_usd"])) == available
    data = {"department": dept, "available_usd": float(available), "annual_budget_usd": row["annual_software_budget_usd"],
            "committed_usd": row["committed_usd"], "amount_usd": float(amount) if amount is not None else None,
            "consistent": consistent}
    if amount is None:
        data["status"] = "unknown_cost"
        finding = f"{dept} has {_money(available)} available; request cost is missing so the budget cannot be checked."
    elif amount > available:
        data["status"] = "insufficient"
        data["shortfall_usd"] = float(amount - available)
        finding = f"{dept} has {_money(available)} available; request {_money(amount)} exceeds it by {_money(amount - available)}."
    else:
        data["status"] = "within"
        finding = f"{dept} has {_money(available)} available; request {_money(amount)} is within budget."
    return ToolResult("check_department_budget", True, data,
                      [EvidenceItem(source="check_department_budget", finding=finding,
                                    reference=f"department_budgets.csv:{dept}")])


def search_software_catalog(ctx: dict, keywords: list[str] | None = None) -> ToolResult:
    request = ctx["request"]
    req_vendor, req_product, req_category = _norm(request.get("vendor_name")), _norm(request.get("product_name")), \
        _norm(request.get("category"))
    req_tokens = _tokens(request.get("product_name")) - _tokens(request.get("vendor_name"))
    extra = {k.lower() for k in (keywords or []) if isinstance(k, str) and len(k) > 2}
    matches = []
    for row in data_access.catalog_records():
        types = []
        if req_product and _norm(row["product_name"]) == req_product:
            types.append("exact_product")
        if req_vendor and _norm(row["vendor_name"]) == req_vendor:
            types.append("same_vendor")
        if req_category and _norm(row["category"]) == req_category:
            types.append("same_category")
        # Product-name tokens only: words in notes such as "assistant" or
        # "support" are too generic to signal overlap on their own. Functional
        # overlap is judged by the LLM via the keywords argument.
        row_tokens = _tokens(row["product_name"]) - _tokens(row["vendor_name"])
        if req_tokens & row_tokens:
            types.append("similar_name")
        haystack = " ".join(str(row.get(k) or "") for k in ("product_name", "category", "notes")).lower()
        if any(k in haystack for k in extra):
            types.append("keyword")
        if types:
            matches.append({**row, "match_types": types})
    evidence = []
    for m in matches:
        evidence.append(EvidenceItem(
            source="search_software_catalog",
            finding=f"{m['product_name']} ({m['vendor_name']}, {m['category']}) is '{m['status']}' with "
                    f"{m['licensed_seats']} seats, scope {m['scope']}: {m.get('notes') or 'no notes'} "
                    f"[match: {', '.join(m['match_types'])}].",
            reference=f"software_catalog.csv:{m['software_id']}"))
    if not matches:
        evidence.append(EvidenceItem(source="search_software_catalog",
                                     finding="No approved catalog product shares this product, vendor or category.",
                                     reference="software_catalog.csv"))
    return ToolResult("search_software_catalog", True, {"matches": matches, "keywords": sorted(extra)}, evidence)


def get_purchase_history(ctx: dict) -> ToolResult:
    vendor = _norm(ctx["request"].get("vendor_name"))
    rows = [r for r in data_access.purchase_records() if _norm(r["vendor_name"]) == vendor]
    evidence = [EvidenceItem(source="get_purchase_history",
                             finding=f"{r['purchase_date']}: {r['department']} bought {r['product_name']} for "
                                     f"{_money(r['annual_amount_usd'])} ({r['status']}; {r.get('notes') or 'no notes'}).",
                             reference=f"purchase_history.csv:{r['purchase_id']}") for r in rows]
    if not rows:
        evidence.append(EvidenceItem(source="get_purchase_history",
                                     finding="No previous purchases from this vendor.",
                                     reference="purchase_history.csv"))
    return ToolResult("get_purchase_history", True, {"purchases": rows}, evidence)


def get_vendor_registry_record(ctx: dict) -> ToolResult:
    vendor = _norm(ctx["request"].get("vendor_name"))
    record = next((r for r in data_access.vendor_records() if _norm(r["vendor_name"]) == vendor), None)
    if record is None:
        return ToolResult("get_vendor_registry_record", True, {"found": False, "record": None},
                          [EvidenceItem(source="get_vendor_registry_record",
                                        finding=f"'{ctx['request'].get('vendor_name')}' is not in the internal vendor "
                                                "registry (treated as a new vendor).", reference="vendors.csv")])
    finding = (f"Registry: procurement '{record['procurement_status']}', security '{record['security_status']}' "
               f"(reviewed {record['security_review_date'] or 'never'}), legal terms '{record['legal_terms_status']}'. "
               f"Note: {record.get('notes') or 'none'}.")
    return ToolResult("get_vendor_registry_record", True, {"found": True, "record": record},
                      [EvidenceItem(source="get_vendor_registry_record", finding=finding,
                                    reference=f"vendors.csv:{record['vendor_id']}")])


def get_vendor_risk_assessment(ctx: dict) -> ToolResult:
    vendor = ctx["request"].get("vendor_name") or ""
    result = fetch_vendor_risk(vendor)
    ref = f"vendor-risk API GET {result['endpoint']}"
    if result["status"] == "ok":
        r = result["record"]
        finding = (f"Vendor-risk service: risk '{r.get('risk_level')}', security review '{r.get('security_review_status')}' "
                   f"(last {r.get('last_review_date') or 'never'}), processes personal data: "
                   f"{r.get('processes_personal_data')}, stores data outside region: {r.get('stores_data_outside_region')}. "
                   f"Note: {r.get('notes') or 'none'}.")
    elif result["status"] == "not_found":
        finding = "Vendor-risk service has no record for this vendor (assessment missing)."
    else:
        finding = (f"Vendor-risk service unavailable (HTTP {result['http_status'] or 'n/a'}: {result['reason']}) after "
                   f"{result['attempts']} attempt(s); status not verified.")
    return ToolResult("get_vendor_risk_assessment", result["status"] != "unavailable", result,
                      [EvidenceItem(source="get_vendor_risk_assessment", finding=finding, reference=ref)],
                      error=None if result["status"] != "unavailable" else result["reason"])


def lookup_policy_sections(ctx: dict, sections: list[int] | None = None) -> ToolResult:
    all_sections = data_access.policy_sections()
    wanted = [int(s) for s in (sections or []) if str(s).isdigit() and int(s) in all_sections] or [1, 4, 5, 6, 7]
    data = {n: all_sections[n] for n in wanted}
    evidence = [EvidenceItem(source="lookup_policy_sections",
                             finding=f"Retrieved policy §{n} ({all_sections[n]['title']}).",
                             reference=f"procurement_policy.md §{n}") for n in wanted]
    return ToolResult("lookup_policy_sections", True, {"sections": data}, evidence)


def screen_untrusted_text(ctx: dict) -> ToolResult:
    request = ctx["request"]
    fields: list[tuple[str, str, str | None]] = [
        ("request", key, request.get(key) if isinstance(request.get(key), str) else None)
        for key in ("business_justification", "product_name", "vendor_name", "category", "data_access_level", "urgency")
    ]
    fields += [("request", "requested_integrations", str(i)) for i in (request.get("requested_integrations") or [])]
    results = ctx.get("results", {})
    reg = results.get("get_vendor_registry_record")
    if reg and reg.data and reg.data.get("record"):
        fields.append(("vendors.csv", "notes", reg.data["record"].get("notes")))
    risk = results.get("get_vendor_risk_assessment")
    if risk and risk.data and risk.data.get("record"):
        fields.append(("vendor-risk API", "notes", risk.data["record"].get("notes")))
    cat = results.get("search_software_catalog")
    if cat and cat.data:
        fields += [("software_catalog.csv", f"{m['software_id']}.notes", m.get("notes")) for m in cat.data["matches"]]
    hits = screen_fields(fields)
    if hits:
        evidence = [EvidenceItem(source="screen_untrusted_text",
                                 finding=f"Embedded instruction ignored ({h['label']}) in {h['source']} {h['field']}: "
                                         f"\"{h['excerpt']}\"", reference=f"{h['source']}:{h['field']}") for h in hits]
    else:
        evidence = [EvidenceItem(source="screen_untrusted_text",
                                 finding=f"Screened {len(fields)} free-text field(s); no embedded instructions found.",
                                 reference="Policy §9")]
    return ToolResult("screen_untrusted_text", True, {"hits": hits, "fields_scanned": len(fields)}, evidence)


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
TOOLS: dict[str, ToolSpec] = {
    spec.name: spec for spec in [
        ToolSpec("check_department_budget", "Compare the annual cost with the requester department's available "
                 "software budget.", "deterministic", {}, check_department_budget, True),
        ToolSpec("search_software_catalog", "Find approved catalog products that match the product, vendor, category "
                 "or extra capability keywords (overlap check).", "deterministic",
                 {"keywords": "optional list of capability words describing the need, e.g. [\"templates\"]"},
                 search_software_catalog, True),
        ToolSpec("get_purchase_history", "Previous purchases and renewals with this vendor.", "deterministic", {},
                 get_purchase_history, False),
        ToolSpec("get_vendor_registry_record", "Internal vendor registry: procurement, security and legal-terms "
                 "status.", "deterministic", {}, get_vendor_registry_record, True),
        ToolSpec("get_vendor_risk_assessment", "External vendor-risk service: risk level, security review status and "
                 "date, data handling.", "external_api", {}, get_vendor_risk_assessment, True),
        ToolSpec("lookup_policy_sections", "Retrieve procurement policy text by section number (1-11).", "retrieval",
                 {"sections": "list of section numbers"}, lookup_policy_sections, False),
        ToolSpec("screen_untrusted_text", "Scan request and vendor free text for embedded instructions (prompt "
                 "injection).", "deterministic", {}, screen_untrusted_text, True),
    ]
}

# Run by the orchestrator before the agent plans (the agent needs the request).
SYSTEM_TOOLS: dict[str, ToolSpec] = {
    "get_request_details": ToolSpec("get_request_details", "Load the request and the requester's employee record.",
                                    "deterministic", {}, get_request_details, True),
}

# screen_untrusted_text reads vendor and catalog notes, so it always runs last.
EXECUTION_ORDER = ["check_department_budget", "search_software_catalog", "get_purchase_history",
                   "get_vendor_registry_record", "get_vendor_risk_assessment", "lookup_policy_sections",
                   "screen_untrusted_text"]
MANDATORY = [name for name in EXECUTION_ORDER if TOOLS[name].mandatory]


def run_tool(name: str, ctx: dict, args: dict | None = None, requested_by: str = "agent") -> ToolResult:
    """Execute a registered tool with whitelisted arguments only."""
    spec = TOOLS.get(name) or SYSTEM_TOOLS[name]
    allowed = {k: v for k, v in (args or {}).items() if k in spec.args}
    start = time.perf_counter()
    try:
        result = spec.func(ctx, **allowed)
    except Exception as exc:  # noqa: BLE001 - a failing tool must not crash the run
        result = ToolResult(name, False, None,
                            [EvidenceItem(source=name, finding=f"Tool failed: {type(exc).__name__}: {exc}",
                                          reference=None)], error=str(exc))
    result.latency_ms = round((time.perf_counter() - start) * 1000, 1)
    result.requested_by = requested_by
    ctx.setdefault("results", {})[name] = result
    return result


def catalog_for_prompt() -> str:
    lines = []
    for name in EXECUTION_ORDER:
        spec = TOOLS[name]
        args = f" args: {spec.args}" if spec.args else ""
        lines.append(f"- {name} [{spec.kind}]: {spec.description}{args}")
    return "\n".join(lines)
