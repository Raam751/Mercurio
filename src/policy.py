"""Deterministic procurement policy engine.

Everything in ``data/procurement_policy.md`` that can be decided by code is
decided here: required fields, budget, approval thresholds, vendor assessment
currency and conflicts, and the Security / Privacy / Legal triggers. The LLM
never computes these values; it can only add escalations on top (see
``guardrails.py``).

Input: the facts gathered by the tools. Output: a ``PolicyAssessment`` with the
required approvals, risk flags, missing information, findings (with policy
references) and an ordered list of recommended actions.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from src.screening import strip_injections

# --------------------------------------------------------------------------- #
# Vocabularies
# --------------------------------------------------------------------------- #
APPROVAL_ORDER = ["Manager", "Department Head", "Finance", "CFO", "Procurement", "Security", "Privacy", "Legal"]

RISK_FLAGS = [
    "missing_information",
    "prompt_injection_detected",
    "vendor_risk_unavailable",
    "conflicting_vendor_evidence",
    "vendor_review_expired",
    "vendor_assessment_missing",
    "vendor_security_rejected",
    "high_vendor_risk",
    "budget_insufficient",
    "budget_data_inconsistent",
    "existing_tool_overlap",
    "limited_use_restriction",
    "security_review_required",
    "privacy_review_required",
    "legal_review_required",
    "cross_region_data",
    "evidence_incomplete",
]

# Recommended actions, most restrictive first. The first one present becomes
# the primary recommendation.
ACTIONS = [
    "REQUEST_CLARIFICATION",
    "VERIFY_VENDOR",
    "BUDGET_EXCEPTION",
    "REVIEW_EXISTING_TOOL",
    "SPECIALIST_REVIEW",
    "STANDARD_APPROVAL",
]

ACTION_LABELS = {
    "REQUEST_CLARIFICATION": "Request clarification before review",
    "VERIFY_VENDOR": "Hold for manual vendor verification",
    "BUDGET_EXCEPTION": "Route to Finance for a budget exception review",
    "REVIEW_EXISTING_TOOL": "Review the existing tool before any new purchase",
    "SPECIALIST_REVIEW": "Route for specialist review before approval",
    "STANDARD_APPROVAL": "Route for standard approval",
}

VALIDITY_DAYS = 365
NEW_VENDOR_LEGAL_THRESHOLD = Decimal("10000")


@dataclass
class Finding:
    rule: str
    text: str
    reference: str


@dataclass
class PolicyAssessment:
    approvals: list[str]
    approval_reasons: dict[str, list[str]]
    risk_flags: list[str]
    missing_information: list[str]
    findings: list[Finding]
    actions: list[str]
    action_reasons: dict[str, list[str]]
    annual_amount: Decimal | None
    data_profile: dict[str, Any]
    vendor_state: dict[str, Any]
    overlap_candidates: list[dict[str, Any]] = field(default_factory=list)

    @property
    def primary_action(self) -> str:
        return self.actions[0]


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _money(value: Decimal | None) -> str:
    if value is None:
        return "unknown"
    return f"${value:,.2f}".replace(".00", "")


def _parse_amount(raw: Any) -> tuple[Decimal | None, str | None]:
    """Return (amount, problem). Accepts numbers or strings like "$12,000"."""
    if raw is None or isinstance(raw, bool):
        return None, None if raw is None else "not a number"
    try:
        text = str(raw).replace("$", "").replace(",", "").strip()
        if not text:
            return None, None
        amount = Decimal(text)
    except InvalidOperation:
        return None, "not a number"
    if amount.is_nan() or amount < 0:
        return None, "negative or invalid"
    return amount, None


def annual_amount(request: dict) -> tuple[Decimal | None, str | None, str]:
    """Annualised amount, a problem description, and how it was derived."""
    amount, problem = _parse_amount(request.get("annual_cost_usd"))
    if amount is not None or problem:
        return amount, problem, "annual_cost_usd"
    monthly, problem = _parse_amount(request.get("monthly_cost_usd"))
    if monthly is not None:
        return monthly * 12, None, "monthly_cost_usd x 12 (estimate)"
    return None, problem, "missing"


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _norm(text: Any) -> str:
    return re.sub(r"[\s\-]+", "_", str(text or "").strip().lower())


def threshold_approvals(amount: Decimal) -> list[str]:
    """Policy section 4 - minimum business approvals by annual amount."""
    if amount <= Decimal("1000"):
        return ["Manager"]
    if amount <= Decimal("10000"):
        return ["Department Head", "Procurement"]
    if amount <= Decimal("25000"):
        return ["Department Head", "Finance", "Procurement"]
    return ["Department Head", "Finance", "CFO", "Procurement"]


def threshold_band(amount: Decimal) -> str:
    if amount <= Decimal("1000"):
        return "up to $1,000"
    if amount <= Decimal("10000"):
        return "$1,000.01-$10,000"
    if amount <= Decimal("25000"):
        return "$10,000.01-$25,000"
    return "above $25,000"


# --------------------------------------------------------------------------- #
# Data sensitivity
# --------------------------------------------------------------------------- #
_UNKNOWN_LEVELS = {"", "unknown", "tbd", "n/a", "na", "unsure", "not_sure", "to_be_confirmed"}
_PROD_INTEGRATION = re.compile(r"\b(production|prod|cloud[ _-]?account|aws|azure|gcp|kubernetes|k8s)\b", re.I)
_CODE_INTEGRATION = re.compile(r"\b(git|github|gitlab|bitbucket|source[ _-]?code|code[ _-]?repositor(y|ies))\b", re.I)
_SECRET_INTEGRATION = re.compile(r"\b(secrets?|vault|credentials?|passwords?|key[ _-]?management)\b", re.I)


def data_profile(level: Any, integrations: Any) -> dict[str, Any]:
    """Classify the requested data access + integrations against sections 5-6."""
    norm = _norm(level)
    unknown = level is None or norm in _UNKNOWN_LEVELS
    security: list[str] = []
    pii: list[str] = []

    if "source" in norm and "code" in norm:
        security.append("source code access")
    if "confidential" in norm:
        security.append("confidential documents")
    if "credential" in norm or "secret" in norm or "password" in norm:
        security.append("credentials/secrets")
    if "production" in norm:
        security.append("production data/access")
    if "employee" in norm and ("pii" in norm or "personal" in norm):
        pii.append("employee PII")
    elif "customer" in norm and ("pii" in norm or "personal" in norm):
        pii.append("customer PII")
    elif "pii" in norm or "personal" in norm:
        pii.append("personal data (PII)")
    security.extend(pii)

    integration_list = integrations if isinstance(integrations, list) else []
    for item in integration_list:
        text = str(item)
        if _PROD_INTEGRATION.search(text):
            security.append(f"production or cloud-account integration ({text})")
        if _CODE_INTEGRATION.search(text):
            security.append(f"source code access via integration ({text})")
        if _SECRET_INTEGRATION.search(text):
            security.append(f"credentials/secrets integration ({text})")

    return {
        "level": level,
        "unknown": unknown,
        "security_reasons": list(dict.fromkeys(security)),
        "pii": pii,
        "sensitive": bool(security),
    }


# --------------------------------------------------------------------------- #
# Vendor assessment (sections 5 and 10)
# --------------------------------------------------------------------------- #
def _status_category(status: Any) -> str:
    s = _norm(status)
    if s in {"approved", "current", "complete", "completed", "passed"}:
        return "approved"
    if s in {"expired", "lapsed", "stale"}:
        return "expired"
    if s in {"rejected", "failed", "denied", "blocked"}:
        return "rejected"
    if s in {"pending", "not_completed", "in_progress", "incomplete", "draft", "requested", "not_started"}:
        return "incomplete"
    return "unknown"


def _source_state(category: str, review_date: date | None, ref: date) -> str:
    if category == "rejected":
        return "rejected"
    if category == "expired":
        return "expired"
    if category == "approved":
        if review_date is None:
            return "undated"
        if review_date > ref:
            return "future_dated"
        return "current" if (ref - review_date).days <= VALIDITY_DAYS else "expired"
    if category == "incomplete":
        return "incomplete"
    return "unknown"


def vendor_state(registry: dict | None, risk: dict | None, ref: date) -> dict[str, Any]:
    reg = (registry or {}).get("record") if registry else None
    reg_category = _status_category(reg.get("security_status")) if reg else "unknown"
    reg_date = _parse_date(reg.get("security_review_date")) if reg else None
    reg_state = _source_state(reg_category, reg_date, ref) if reg else "not_in_registry"

    api_status = (risk or {}).get("status", "not_run")
    api_record = (risk or {}).get("record") or {}
    api_category = _status_category(api_record.get("security_review_status")) if api_status == "ok" else "unknown"
    api_date = _parse_date(api_record.get("last_review_date")) if api_status == "ok" else None
    api_state = _source_state(api_category, api_date, ref) if api_status == "ok" else api_status

    conflicts: list[str] = []
    if reg and api_status == "ok":
        if reg_category != "unknown" and api_category != "unknown" and reg_category != api_category:
            conflicts.append(
                f"registry security status '{reg.get('security_status')}' vs vendor-risk service "
                f"'{api_record.get('security_review_status')}'")
        if reg_date and api_date and reg_date != api_date:
            conflicts.append(f"registry review date {reg_date} vs vendor-risk service {api_date}")

    ages = {name: (ref - d).days for name, d in (("registry", reg_date), ("vendor-risk service", api_date)) if d}
    current = (
        api_status == "ok"
        and api_state == "current"
        and reg_state in {"current", "not_in_registry"}
        and not conflicts
    )
    return {
        "registry_state": reg_state,
        "service_state": api_state,
        "service_status": api_status,
        "conflicts": conflicts,
        "review_age_days": ages,
        "current": current,
        "expired": "expired" in {reg_state, api_state},
        "rejected": "rejected" in {reg_state, api_state},
        "risk_level": api_record.get("risk_level"),
        "processes_personal_data": api_record.get("processes_personal_data"),
        "stores_data_outside_region": api_record.get("stores_data_outside_region"),
        "service_notes": api_record.get("notes"),
        "service_reason": (risk or {}).get("reason"),
    }


# --------------------------------------------------------------------------- #
# The engine
# --------------------------------------------------------------------------- #
class _Builder:
    def __init__(self) -> None:
        self.approvals: dict[str, list[str]] = {}
        self.flags: list[str] = []
        self.missing: list[str] = []
        self.findings: list[Finding] = []
        self.actions: dict[str, list[str]] = {}

    def approve(self, role: str, reason: str) -> None:
        self.approvals.setdefault(role, [])
        if reason not in self.approvals[role]:
            self.approvals[role].append(reason)

    def flag(self, name: str) -> None:
        if name not in self.flags:
            self.flags.append(name)

    def find(self, rule: str, text: str, ref: str) -> None:
        self.findings.append(Finding(rule, text, ref))

    def act(self, action: str, reason: str) -> None:
        self.actions.setdefault(action, []).append(reason)


def assess(facts: dict[str, Any], reference_date: date) -> PolicyAssessment:
    """Apply the procurement policy to the gathered facts.

    ``facts`` keys (each the ``data`` of the matching tool result, or None if the
    tool did not run): request, requester, budget, catalog, history, registry,
    risk, screening.
    """
    b = _Builder()
    request: dict = facts.get("request") or {}
    requester: dict | None = facts.get("requester")
    budget: dict | None = facts.get("budget")
    catalog: dict | None = facts.get("catalog")
    registry: dict | None = facts.get("registry")
    risk: dict | None = facts.get("risk")
    screening: dict | None = facts.get("screening")

    for key in ("budget", "catalog", "registry", "risk", "screening"):
        if facts.get(key) is None:
            b.flag("evidence_incomplete")
            b.find("evidence", f"Required check '{key}' did not run; its outcome is unverified.", "Policy §10")
            b.act("VERIFY_VENDOR" if key in {"registry", "risk"} else "REQUEST_CLARIFICATION",
                  f"{key} check did not run")

    # ---- Section 9: untrusted content ------------------------------------
    hits = (screening or {}).get("hits", [])
    if hits:
        b.flag("prompt_injection_detected")
        labels = sorted({h["label"] for h in hits})
        b.find("injection",
               f"Embedded instructions found in business data ({', '.join(labels)}); they were ignored and "
               "do not count as approval.", "Policy §9")

    # ---- Section 1: required information ---------------------------------
    if not requester:
        b.missing.append(f"Requester and department (requester ID '{request.get('requester_id')}' "
                         "not found in the employee directory)")
    if not request.get("product_name") or not request.get("vendor_name"):
        b.missing.append("Product and vendor name")

    amount, amount_problem, amount_basis = annual_amount(request)
    if amount is None:
        detail = f" (value is {amount_problem})" if amount_problem else ""
        b.missing.append(f"Annual cost or a reasonable annual estimate{detail}")

    users = request.get("user_count")
    if not isinstance(users, int) or isinstance(users, bool) or users <= 0:
        b.missing.append("Number of users / licenses")

    purpose = strip_injections(request.get("business_justification"))
    if len(re.findall(r"[A-Za-z]{3,}", purpose)) < 4:
        b.missing.append("Business purpose (justification is missing or too vague once embedded "
                         "instructions are removed)")

    profile = data_profile(request.get("data_access_level"), request.get("requested_integrations"))
    if profile["unknown"]:
        b.missing.append("Intended data-access level")
    if request.get("requested_integrations") is None:
        b.missing.append("Required integrations (list them, or confirm none)")

    if b.missing:
        b.flag("missing_information")
        b.act("REQUEST_CLARIFICATION", "material request information is missing")
        b.find("required_info", f"{len(b.missing)} required field(s) missing or unusable; the copilot "
               "does not invent values.", "Policy §1")

    # ---- Section 4: thresholds ----------------------------------------------
    if amount is not None:
        for role in threshold_approvals(amount):
            b.approve(role, f"annual amount {_money(amount)} is in the {threshold_band(amount)} band")
        basis = "" if amount_basis == "annual_cost_usd" else f" ({amount_basis})"
        b.find("threshold", f"Annual amount {_money(amount)}{basis} falls in the {threshold_band(amount)} "
               f"band: minimum approvals {', '.join(threshold_approvals(amount))}.", "Policy §4")
    else:
        b.find("threshold", "Approval tier cannot be determined until the annual cost is provided.", "Policy §4")

    # ---- Section 2: budget --------------------------------------------------
    if budget:
        status = budget.get("status")
        if status == "insufficient":
            b.flag("budget_insufficient")
            b.approve("Finance", "budget exception: cost exceeds available department budget")
            b.act("BUDGET_EXCEPTION", "cost exceeds available software budget")
            b.find("budget", f"{budget['department']} has {_money(Decimal(str(budget['available_usd'])))} "
                   f"available; request {_money(amount)} exceeds it by "
                   f"{_money(Decimal(str(budget['shortfall_usd'])))}.", "Policy §2")
        elif status == "within":
            b.find("budget", f"Request fits {budget['department']}'s available budget. A positive budget "
                   "check does not imply approval.", "Policy §2")
        elif status == "unknown_requester":
            b.find("budget", "Budget not checked: the requester (and so the department) is unknown.", "Policy §2")
        elif status == "unknown_department":
            b.flag("budget_data_inconsistent")
            b.approve("Finance", "department budget could not be verified")
            b.act("BUDGET_EXCEPTION", "department budget could not be found")
        if budget.get("consistent") is False:
            b.flag("budget_data_inconsistent")
            b.approve("Finance", "budget record arithmetic is inconsistent")
            b.find("budget", "Budget record arithmetic is inconsistent (available != annual - committed).",
                   "Policy §2")

    # ---- Section 3: overlap ---------------------------------------------------
    candidates = (catalog or {}).get("matches", [])
    overlapping = [m for m in candidates if set(m["match_types"]) - {"same_vendor"}]
    if overlapping:
        b.flag("existing_tool_overlap")
        names = ", ".join(f"{m['product_name']} ({m['software_id']})" for m in overlapping)
        b.find("overlap", f"Existing approved option(s) overlap with this request: {names}. Overlap is not an "
               "automatic rejection; confirm the gap or exception reason.", "Policy §3")
        exact = [m for m in overlapping if "exact_product" in m["match_types"]]
        if exact:
            b.act("REVIEW_EXISTING_TOOL", f"exact duplicate of {exact[0]['product_name']}")
    limited = [m for m in candidates if "limited" in _norm(m.get("status"))]
    if limited and (profile["sensitive"] or profile["unknown"]):
        b.flag("limited_use_restriction")
        b.find("ai_tools", f"{limited[0]['product_name']} is approved for limited use only; an earlier approval "
               "does not extend to this data class.", "Policy §8")

    # ---- Section 5: security ---------------------------------------------------
    vstate = vendor_state(registry, risk, reference_date)
    for reason in profile["security_reasons"]:
        b.approve("Security", reason)
    if profile["security_reasons"]:
        b.find("security", "Security review required for: " + "; ".join(profile["security_reasons"]) + ".",
               "Policy §5")

    if risk is not None:
        if vstate["service_status"] == "unavailable":
            b.flag("vendor_risk_unavailable")
            b.approve("Security", "vendor security status could not be verified (service unavailable)")
            b.act("VERIFY_VENDOR", "vendor-risk service unavailable; status not assumed favourable")
            b.find("vendor", f"Vendor-risk service unavailable ({vstate['service_reason']}); vendor status "
                   "was not assumed favourable.", "Policy §10")
        elif vstate["service_status"] == "not_found":
            b.flag("vendor_assessment_missing")
            b.approve("Security", "no vendor security assessment on record")
            b.act("VERIFY_VENDOR", "no vendor security assessment on record")
            b.find("vendor", "No vendor-risk record exists for this vendor; the security assessment is missing.",
                   "Policy §5")
    if vstate["conflicts"]:
        b.flag("conflicting_vendor_evidence")
        b.approve("Security", "internal registry and vendor-risk service disagree")
        b.act("VERIFY_VENDOR", "conflicting vendor evidence must be reconciled by a human")
        b.find("vendor", "Vendor evidence conflicts: " + "; ".join(vstate["conflicts"]) + ". Neither source "
               "was silently preferred.", "Policy §5")
    if vstate["expired"]:
        b.flag("vendor_review_expired")
        b.approve("Security", "vendor security assessment expired (older than 365 days)")
        ages = ", ".join(f"{k}: {v} days" for k, v in vstate["review_age_days"].items())
        b.find("vendor", f"Vendor security assessment is expired ({ages} as of {reference_date}).", "Policy §5")
    if vstate["rejected"]:
        b.flag("vendor_security_rejected")
        b.approve("Security", "vendor security assessment was rejected")
        b.act("VERIFY_VENDOR", "vendor security assessment rejected")
    if risk is not None and vstate["service_status"] == "ok" and not vstate["current"] and not vstate["expired"] \
            and not vstate["conflicts"] and not vstate["rejected"]:
        b.approve("Security", "vendor security assessment is missing or not completed")
        b.find("vendor", f"Vendor security assessment is not current (service state: {vstate['service_state']}).",
               "Policy §5")
    if _norm(vstate.get("risk_level")) in {"high", "critical"}:
        b.flag("high_vendor_risk")
        b.approve("Security", "vendor-risk service rates the vendor high risk")
    if limited and (profile["sensitive"] or profile["unknown"]):
        b.approve("Security", "limited-use product requested for a sensitive or unknown data class")

    # ---- Section 6: privacy -----------------------------------------------------
    outside = vstate.get("stores_data_outside_region") is True
    for reason in profile["pii"]:
        b.approve("Privacy", f"tool will process {reason}")
    if outside and profile["sensitive"]:
        b.flag("cross_region_data")
        b.approve("Privacy", "sensitive data may be stored outside the operating region")
    if profile["pii"] or (outside and profile["sensitive"]):
        b.find("privacy", "Privacy review required: " + "; ".join(b.approvals.get("Privacy", [])) + ".",
               "Policy §6")
    if outside and profile["unknown"]:
        b.find("privacy", "Vendor stores data outside the operating region and the data class is unknown, so a "
               "Privacy review cannot be ruled out yet.", "Policy §6")

    # ---- Section 7: legal ---------------------------------------------------------
    reg_record = (registry or {}).get("record")
    new_vendor = reg_record is None or _norm(reg_record.get("procurement_status")) not in {"approved", "active"}
    terms = _norm((reg_record or {}).get("legal_terms_status"))
    if new_vendor and amount is not None and amount >= NEW_VENDOR_LEGAL_THRESHOLD:
        b.approve("Legal", f"new vendor with annual spend {_money(amount)} (>= $10,000)")
    if terms not in {"approved", "standard"}:
        shown = (reg_record or {}).get("legal_terms_status") or "not on record"
        b.approve("Legal", f"legal terms are not approved/standard ({shown})")
    if outside and (profile["pii"] or profile["sensitive"]):
        b.approve("Legal", "material cross-region data-processing issue")
    if "Legal" in b.approvals:
        b.find("legal", "Legal review required: " + "; ".join(b.approvals["Legal"]) + ".", "Policy §7")

    # ---- Review flags + specialist action ---------------------------------------
    for role, flag in (("Security", "security_review_required"), ("Privacy", "privacy_review_required"),
                       ("Legal", "legal_review_required")):
        if role in b.approvals:
            b.flag(flag)
    specialists = [r for r in ("Security", "Privacy", "Legal") if r in b.approvals]
    if specialists:
        b.act("SPECIALIST_REVIEW", "required reviews: " + ", ".join(specialists))
    b.act("STANDARD_APPROVAL", "business approvals by threshold")

    if _norm(request.get("urgency")) in {"high", "urgent", "critical", "asap"}:
        b.find("urgency", f"Urgency '{request.get('urgency')}' noted; it does not remove any review or approval.",
               "Policy §11")

    approvals = [r for r in APPROVAL_ORDER if r in b.approvals] + [r for r in b.approvals if r not in APPROVAL_ORDER]
    actions = [a for a in ACTIONS if a in b.actions]
    flags = [f for f in RISK_FLAGS if f in b.flags] + [f for f in b.flags if f not in RISK_FLAGS]
    return PolicyAssessment(
        approvals=approvals,
        approval_reasons=b.approvals,
        risk_flags=flags,
        missing_information=b.missing,
        findings=b.findings,
        actions=actions,
        action_reasons=b.actions,
        annual_amount=amount,
        data_profile=profile,
        vendor_state=vstate,
        overlap_candidates=overlapping,
    )
