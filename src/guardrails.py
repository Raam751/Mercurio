"""Guardrails between the LLM and the final decision.

The model's output is treated as a proposal. Nothing it says reaches the user
unless it passes these checks:

* Plans may only name registered tools and whitelisted arguments.
* Mandatory checks the model skipped are run anyway ("backfilled").
* Judgements may only ESCALATE: add an overlap review, a risk flag or a review.
  They can never remove an approval, flag or missing-information item computed
  by the policy engine, and can never mark the request as approved.
* Every evidence ID the model cites must exist; otherwise its text is replaced
  by a deterministic rationale.
* Text that echoes embedded instructions or claims an approval is discarded.
"""
from __future__ import annotations

import re
from typing import Any

from src.policy import ACTIONS, ACTION_LABELS, PolicyAssessment
from src.prompts import LLM_ADDABLE_FLAGS
from src.screening import scan_text
from src.tools import MANDATORY, TOOLS

FLAG_TO_ROLE = {
    "security_review_required": "Security",
    "privacy_review_required": "Privacy",
    "legal_review_required": "Legal",
}
VERDICTS = {"credible_gap", "likely_covered", "unclear", "not_applicable"}
_CITATION = re.compile(r"\bEV-\d+\b")
_AUTONOMY = re.compile(
    r"\b(i|we|the copilot)\s+(have\s+|has\s+)?(approved|purchased|authori[sz]ed|signed)\b"
    r"|\b(request|purchase)\s+(is|has been)\s+(now\s+)?approved\b"
    r"|\bapproval\s+(is\s+|has been\s+)?granted\b"
    r"|\bno\s+(further\s+)?(review|approval)s?\s+(is\s+|are\s+)?(needed|required)\b",
    re.IGNORECASE,
)


class Events(list):
    """Ordered guardrail log shown in the UI and counted in the evaluation."""

    def add(self, kind: str, detail: str) -> None:
        self.append({"kind": kind, "detail": detail})


def _unsafe(text: str) -> str | None:
    if not text:
        return None
    if _AUTONOMY.search(text):
        return "claims an approval or skips a review"
    if scan_text(text):
        return "repeats an embedded instruction"
    return None


# --------------------------------------------------------------------------- #
# Plans
# --------------------------------------------------------------------------- #
def validate_plan(plan: dict, events: Events) -> dict[str, dict]:
    """Return {tool_name: args} for valid tool calls in the model's plan."""
    selected: dict[str, dict] = {}
    for call in plan.get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        name = str(call.get("tool") or "").strip()
        if name not in TOOLS:
            events.add("plan_rejected_tool", f"ignored unknown tool '{name[:60]}'")
            continue
        raw_args = call.get("args") if isinstance(call.get("args"), dict) else {}
        args: dict[str, Any] = {}
        if name == "search_software_catalog" and isinstance(raw_args.get("keywords"), list):
            args["keywords"] = [str(k)[:40] for k in raw_args["keywords"] if isinstance(k, (str, int))][:8]
        if name == "lookup_policy_sections" and isinstance(raw_args.get("sections"), list):
            args["sections"] = [int(s) for s in raw_args["sections"] if str(s).isdigit()][:11]
        dropped = set(raw_args) - set(args)
        if dropped:
            events.add("plan_args_dropped", f"{name}: ignored argument(s) {sorted(dropped)}")
        selected[name] = args
    return selected


def backfill(selected: dict[str, dict], events: Events) -> list[str]:
    missing = [name for name in MANDATORY if name not in selected]
    for name in missing:
        events.add("tool_backfilled", f"mandatory check '{name}' was not selected by the agent and was run anyway")
    return missing


# --------------------------------------------------------------------------- #
# Staged handoff brief
# --------------------------------------------------------------------------- #
def validate_brief(brief: dict, valid_ids: set[str], events: Events) -> dict:
    clean: dict[str, Any] = {"facts": []}
    for item in brief.get("facts") or []:
        if not isinstance(item, dict):
            continue
        eid, claim = str(item.get("evidence_id") or ""), str(item.get("claim") or "")[:300]
        if eid not in valid_ids:
            events.add("brief_fact_dropped", f"fact cited unknown evidence id '{eid[:20]}'")
            continue
        problem = _unsafe(claim)
        if problem:
            events.add("brief_fact_dropped", f"fact for {eid} {problem}")
            continue
        clean["facts"].append({"evidence_id": eid, "claim": claim})
    for key in ("request_summary", "stated_need"):
        text = str(brief.get(key) or "")[:400]
        problem = _unsafe(text)
        if problem:
            events.add("brief_text_removed", f"{key} {problem}")
            text = "[removed: contained embedded instructions]"
        clean[key] = text
    for key in ("gaps", "conflicts"):
        items = [str(x)[:200] for x in (brief.get(key) or []) if isinstance(x, (str, int))]
        clean[key] = [x for x in items if not _unsafe(x)]
    clean["injection_observed"] = bool(brief.get("injection_observed"))
    return clean


# --------------------------------------------------------------------------- #
# Judgements
# --------------------------------------------------------------------------- #
def rules_judgement(assessment: PolicyAssessment) -> dict:
    """Deterministic stand-in for the model's judgement (rules-only mode / fallback)."""
    candidates = assessment.overlap_candidates
    overlap = {"verdict": "unclear" if candidates else "not_applicable",
               "existing_option": candidates[0]["software_id"] if candidates else None,
               "reason": ("Overlap found; Procurement must confirm the stated gap." if candidates
                          else "No overlapping catalog product."),
               "evidence_ids": []}
    questions = [f"Please provide: {item}." for item in assessment.missing_information]
    return {"overlap_assessment": overlap, "rationale": "", "clarifying_questions": questions,
            "additional_concerns": [], "injection_observed": False}


def validate_judgement(judgement: dict, assessment: PolicyAssessment, valid_ids: set[str],
                       events: Events) -> dict:
    out = rules_judgement(assessment)
    candidate_ids = {m["software_id"] for m in assessment.overlap_candidates}

    raw = judgement.get("overlap_assessment") if isinstance(judgement.get("overlap_assessment"), dict) else {}
    verdict = str(raw.get("verdict") or "").strip().lower()
    option = raw.get("existing_option")
    cited = [str(e) for e in (raw.get("evidence_ids") or []) if str(e) in valid_ids]
    reason = str(raw.get("reason") or "")[:300]
    if verdict not in VERDICTS:
        events.add("judgement_rejected", f"overlap verdict '{verdict[:30]}' is not allowed")
    elif candidate_ids and verdict == "not_applicable":
        events.add("judgement_rejected", "model said overlap is not applicable but the catalog found candidates")
    elif verdict == "likely_covered" and option not in candidate_ids:
        events.add("judgement_rejected", f"'likely_covered' cited {option!r}, which is not an overlap candidate")
    elif verdict in {"likely_covered", "credible_gap"} and not cited:
        events.add("judgement_rejected", f"overlap verdict '{verdict}' cited no valid evidence")
    elif _unsafe(reason):
        events.add("judgement_rejected", f"overlap reason {_unsafe(reason)}")
    elif not candidate_ids:
        pass  # nothing to judge; keep the deterministic "not_applicable"
    else:
        out["overlap_assessment"] = {"verdict": verdict, "existing_option": option if option in candidate_ids else None,
                                     "reason": reason, "evidence_ids": cited}

    rationale = str(judgement.get("rationale") or "")[:900]
    bad_ids = sorted({c for c in _CITATION.findall(rationale) if c not in valid_ids})
    problem = _unsafe(rationale)
    if bad_ids:
        events.add("rationale_replaced", f"cited evidence that does not exist: {', '.join(bad_ids)}")
    elif problem:
        events.add("rationale_replaced", f"model rationale {problem}")
    elif rationale:
        out["rationale"] = rationale

    questions = [str(q)[:250] for q in (judgement.get("clarifying_questions") or []) if isinstance(q, str)]
    questions = [q for q in questions if not _unsafe(q)][:6]
    needs_questions = bool(assessment.missing_information) or out["overlap_assessment"]["verdict"] == "unclear"
    if questions and needs_questions:
        out["clarifying_questions"] = questions

    concerns = []
    for c in judgement.get("additional_concerns") or []:
        if not isinstance(c, dict):
            continue
        flag = str(c.get("flag") or "")
        ids = [str(e) for e in (c.get("evidence_ids") or []) if str(e) in valid_ids]
        if flag not in LLM_ADDABLE_FLAGS:
            events.add("concern_rejected", f"flag '{flag[:40]}' is not in the allowed list")
        elif not ids:
            events.add("concern_rejected", f"'{flag}' cited no valid evidence")
        elif flag in assessment.risk_flags:
            continue
        else:
            concerns.append({"flag": flag, "reason": str(c.get("reason") or "")[:200], "evidence_ids": ids})
    out["additional_concerns"] = concerns
    out["injection_observed"] = bool(judgement.get("injection_observed"))
    return out


# --------------------------------------------------------------------------- #
# Merge into the final recommendation
# --------------------------------------------------------------------------- #
def merge(assessment: PolicyAssessment, judgement: dict, events: Events) -> dict:
    approvals = list(assessment.approvals)
    reasons = {k: list(v) for k, v in assessment.approval_reasons.items()}
    flags = list(assessment.risk_flags)
    actions = list(assessment.actions)
    action_reasons = {k: list(v) for k, v in assessment.action_reasons.items()}

    overlap = judgement["overlap_assessment"]
    if overlap["verdict"] == "likely_covered" and "REVIEW_EXISTING_TOOL" not in actions:
        actions.append("REVIEW_EXISTING_TOOL")
        action_reasons["REVIEW_EXISTING_TOOL"] = [f"agent judged {overlap['existing_option']} likely covers the need"]
        events.add("llm_escalation", f"existing-tool review added: {overlap['reason']}")

    for concern in judgement["additional_concerns"]:
        flags.append(concern["flag"])
        role = FLAG_TO_ROLE.get(concern["flag"])
        if role and role not in approvals:
            approvals.append(role)
            reasons[role] = [f"agent concern: {concern['reason']}"]
            if "SPECIALIST_REVIEW" not in actions:
                actions.append("SPECIALIST_REVIEW")
                action_reasons["SPECIALIST_REVIEW"] = [f"agent concern: {concern['flag']}"]
        events.add("llm_escalation", f"added {concern['flag']}: {concern['reason']}")

    if judgement["injection_observed"] and "prompt_injection_detected" not in flags:
        flags.append("prompt_injection_detected")
        events.add("llm_escalation", "agent reported embedded instructions the regex screen did not catch")

    actions = [a for a in ACTIONS if a in actions]
    return {"approvals": approvals, "approval_reasons": reasons, "flags": list(dict.fromkeys(flags)),
            "actions": actions, "action_reasons": action_reasons}


def recommendation_text(primary: str, merged: dict) -> str:
    label = ACTION_LABELS[primary]
    if primary == "SPECIALIST_REVIEW":
        roles = [r for r in ("Security", "Privacy", "Legal") if r in merged["approvals"]]
        label = f"Route for {_join(roles)} review before approval"
    elif primary == "STANDARD_APPROVAL":
        label = f"Route for standard approval ({_join(merged['approvals'])})"
    reason = merged["action_reasons"].get(primary, [""])[0]
    return f"{label}: {reason}" if reason and primary != "STANDARD_APPROVAL" else label


def next_steps(merged: dict, assessment: PolicyAssessment, judgement: dict, requester: dict | None) -> list[str]:
    who = requester["name"] if requester else "the requester"
    business = [r for r in merged["approvals"] if r not in {"Security", "Privacy", "Legal"}]
    specialists = [r for r in ("Security", "Privacy", "Legal") if r in merged["approvals"]]
    steps: list[str] = []
    for action in merged["actions"]:
        if action == "REQUEST_CLARIFICATION":
            items = "; ".join(m.split(" (")[0] for m in assessment.missing_information)
            steps.append(f"Ask {who} for: {items}. Do not route for approval until the request is complete.")
        elif action == "VERIFY_VENDOR":
            steps.append("Security to verify the vendor's assessment manually ("
                         + "; ".join(merged["action_reasons"]["VERIFY_VENDOR"]) + ").")
        elif action == "BUDGET_EXCEPTION":
            steps.append("Finance to decide on a budget exception before any approval.")
        elif action == "REVIEW_EXISTING_TOOL":
            option = judgement["overlap_assessment"].get("existing_option") or "the existing catalog tool"
            steps.append(f"Procurement to confirm with {who} whether {option} already covers the need before a new purchase.")
        elif action == "SPECIALIST_REVIEW":
            steps.append(f"Send the evidence package to {_join(specialists)} for review.")
        elif action == "STANDARD_APPROVAL" and business:
            steps.append(f"Route to {_join(business)} for approval.")
    if assessment.overlap_candidates and "REVIEW_EXISTING_TOOL" not in merged["actions"]:
        names = ", ".join(m["software_id"] for m in assessment.overlap_candidates)
        steps.insert(min(1, len(steps)), f"Procurement to confirm the stated gap versus existing option(s) {names}.")
    steps.append("A human approver makes the final decision; the copilot does not approve or purchase.")
    return steps


def default_rationale(primary: str, merged: dict, assessment: PolicyAssessment) -> str:
    reasons = []
    for action in merged["actions"]:
        if action != "STANDARD_APPROVAL":
            reasons.extend(merged["action_reasons"].get(action, [])[:1])
    approvals = _join(merged["approvals"]) or "to be determined"
    lead = ACTION_LABELS[primary]
    detail = f" Reasons: {'; '.join(reasons)}." if reasons else ""
    return f"{lead}.{detail} Required approvals: {approvals}."


def _join(items: list[str]) -> str:
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]
