"""Agent orchestration for both architectures.

Architecture A - single agent (2 LLM calls)
    1. plan:   the agent reads the request and chooses tools
    2. decide: the agent reads the evidence + policy engine results and gives its
               judgement (overlap gap, rationale, questions, extra concerns)

Architecture B - staged, two agents (3 LLM calls)
    1. Evidence Analyst plans the tools
    2. Evidence Analyst writes a neutral evidence brief citing evidence IDs
    3. Policy Reviewer decides from the validated brief + engine results only;
       it never sees the raw request text or raw tool output

Shared, deterministic in both:
    get_request_details -> agent-selected tools -> guardrail backfill of mandatory
    checks -> apply_policy_rules (policy engine) -> guardrail merge -> decision
"""
from __future__ import annotations

import time
from typing import Any

from src import data_access, guardrails, prompts, tools
from src.contracts import EvidenceItem, ProcurementDecision, RunTelemetry
from src.llm import LLMError, RulesOnlyProvider, get_provider, parse_json_reply
from src.policy import PolicyAssessment, assess
from src.screening import scan_text


class CopilotDecision(ProcurementDecision):
    """ProcurementDecision plus the audit detail the UI and evaluation use."""

    architecture: str
    llm_provider: str
    model: str | None = None
    primary_action: str
    actions: list[str] = []
    rationale: str = ""
    overlap_assessment: dict[str, Any] = {}
    clarifying_questions: list[str] = []
    approval_reasons: dict[str, list[str]] = {}
    next_steps: list[str] = []
    evidence_ids: list[str] = []
    guardrail_events: list[dict[str, str]] = []
    tool_trace: list[dict[str, Any]] = []
    llm_trace: list[dict[str, Any]] = []
    credits_used: float = 0.0
    internal_latency_ms: float = 0.0


class _Run:
    def __init__(self, request_id: str, architecture: str, provider) -> None:
        self.request_id = request_id
        self.architecture = architecture
        self.provider = provider
        self.ctx: dict[str, Any] = {"request_id": request_id}
        self.events = guardrails.Events()
        self.llm_trace: list[dict[str, Any]] = []
        self.started = time.perf_counter()

    @property
    def use_llm(self) -> bool:
        return not isinstance(self.provider, RulesOnlyProvider)

    def ask(self, prompt: str, purpose: str) -> dict | None:
        """One model call. Returns parsed JSON, or None after logging a failure."""
        if not self.use_llm:
            return None
        response = None
        try:
            response = self.provider.complete(prompt, purpose=purpose)
            parsed = parse_json_reply(response.text)
            self.llm_trace.append({"purpose": purpose, "ok": True, "latency_ms": response.latency_ms,
                                   "credits": response.credits, "prompt_chars": len(prompt)})
            return parsed
        except LLMError as exc:
            self.llm_trace.append({
                "purpose": purpose, "ok": False,
                "latency_ms": response.latency_ms if response else None,
                "credits": response.credits if response else None,
                "prompt_chars": len(prompt), "error": str(exc)[:300],
                "raw_reply_excerpt": response.text[:1500] if response else None,
            })
            self.events.add("llm_failed", f"{purpose}: {str(exc)[:200]} - used deterministic fallback")
            return None


# --------------------------------------------------------------------------- #
# Shared steps
# --------------------------------------------------------------------------- #
def _plan_and_gather(run: _Run, role: str) -> str:
    """Model chooses tools; code validates, backfills mandatory checks, executes."""
    tools.run_tool("get_request_details", run.ctx, requested_by="system")
    request, requester = run.ctx["request"], run.ctx["requester"]

    plan = run.ask(prompts.plan_prompt(role, tools.catalog_for_prompt(), request, requester), f"{role}: plan")
    if plan is None:
        selected = {name: {} for name in tools.MANDATORY + ["get_purchase_history"]}
        understanding = ""
        if run.use_llm:
            run.events.add("plan_fallback", "no usable plan; ran the default tool set")
    else:
        selected = guardrails.validate_plan(plan, run.events)
        understanding = str(plan.get("understanding") or "")[:300]
        if scan_text(understanding):
            understanding = ""
            run.events.add("plan_text_removed", "plan summary repeated an embedded instruction")

    backfilled = set(guardrails.backfill(selected, run.events)) if plan is not None else set()
    for name in tools.EXECUTION_ORDER:
        if name in selected:
            tools.run_tool(name, run.ctx, selected[name], requested_by="agent" if run.use_llm else "system")
        elif name in backfilled:
            tools.run_tool(name, run.ctx, requested_by="guardrail")
    return understanding


def _assess(run: _Run) -> PolicyAssessment:
    start = time.perf_counter()
    r = run.ctx["results"]

    def data(name: str):
        result = r.get(name)
        return result.data if result else None

    facts = {"request": run.ctx["request"], "requester": run.ctx["requester"],
             "budget": data("check_department_budget"), "catalog": data("search_software_catalog"),
             "history": data("get_purchase_history"), "registry": data("get_vendor_registry_record"),
             "risk": data("get_vendor_risk_assessment"), "screening": data("screen_untrusted_text")}
    assessment = assess(facts, data_access.get_reference_date())
    run.ctx["results"]["apply_policy_rules"] = tools.ToolResult(
        "apply_policy_rules", True, {"primary_action": assessment.primary_action},
        [EvidenceItem(source="apply_policy_rules", finding=f.text, reference=f.reference)
         for f in assessment.findings],
        latency_ms=round((time.perf_counter() - start) * 1000, 1), requested_by="system")
    return assessment


def _evidence(run: _Run) -> list[tuple[str, EvidenceItem]]:
    items: list[EvidenceItem] = []
    order = ["get_request_details", *tools.EXECUTION_ORDER, "apply_policy_rules"]
    for name in order:
        result = run.ctx["results"].get(name)
        if result is None or name == "lookup_policy_sections":
            continue  # policy retrieval is logged in the tool trace, not shown as evidence
        items.extend(result.evidence)
    return [(f"EV-{i}", item) for i, item in enumerate(items, start=1)]


def _structured_fields(request: dict, requester: dict | None) -> dict:
    """Non-free-text request fields for the Policy Reviewer, with injected values withheld."""
    keys = ["product_name", "vendor_name", "category", "annual_cost_usd", "user_count", "data_access_level",
            "requested_integrations", "urgency"]
    out: dict[str, Any] = {}
    for key in keys:
        value = request.get(key)
        out[key] = "[withheld: contained embedded instructions]" if isinstance(value, str) and scan_text(value) else value
    out["requester_department"] = requester["department"] if requester else None
    return out


# --------------------------------------------------------------------------- #
# Architectures
# --------------------------------------------------------------------------- #
def _single(run: _Run) -> tuple[PolicyAssessment, dict, list]:
    understanding = _plan_and_gather(run, "procurement copilot agent")
    assessment = _assess(run)
    evidence = _evidence(run)
    valid_ids = {eid for eid, _ in evidence}
    raw = run.ask(prompts.decide_prompt(understanding, run.ctx["request"], run.ctx["requester"], evidence, assessment),
                  "single agent: decide")
    judgement = (guardrails.validate_judgement(raw, assessment, valid_ids, run.events) if raw is not None
                 else guardrails.rules_judgement(assessment))
    return assessment, judgement, evidence


def _staged(run: _Run) -> tuple[PolicyAssessment, dict, list]:
    _plan_and_gather(run, "Evidence Analyst")
    assessment = _assess(run)
    evidence = _evidence(run)
    valid_ids = {eid for eid, _ in evidence}

    raw_brief = run.ask(prompts.brief_prompt(run.ctx["request"], run.ctx["requester"], evidence),
                        "Evidence Analyst: brief")
    if raw_brief is None:
        return assessment, guardrails.rules_judgement(assessment), evidence
    brief = guardrails.validate_brief(raw_brief, valid_ids, run.events)
    run.ctx["handoff_brief"] = brief

    structured = _structured_fields(run.ctx["request"], run.ctx["requester"])
    raw = run.ask(prompts.review_prompt(structured, brief, assessment), "Policy Reviewer: decide")
    judgement = (guardrails.validate_judgement(raw, assessment, valid_ids, run.events) if raw is not None
                 else guardrails.rules_judgement(assessment))
    if brief.get("injection_observed"):
        judgement["injection_observed"] = True
    return assessment, judgement, evidence


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
def run_request(request_id: str, architecture: str = "single", provider=None) -> CopilotDecision:
    if architecture not in {"single", "staged"}:
        raise ValueError(f"unknown architecture '{architecture}'")
    if provider is None:
        provider, note = get_provider()
    else:
        note = getattr(provider, "name", "custom")
    run = _Run(request_id, architecture, provider)

    assessment, judgement, evidence = (_single if architecture == "single" else _staged)(run)
    merged = guardrails.merge(assessment, judgement, run.events)
    primary = merged["actions"][0]
    rationale = judgement.get("rationale") or guardrails.default_rationale(primary, merged, assessment)
    steps = guardrails.next_steps(merged, assessment, judgement, run.ctx["requester"])

    extra_evidence = []
    if judgement["overlap_assessment"]["verdict"] in {"likely_covered", "credible_gap"}:
        o = judgement["overlap_assessment"]
        extra_evidence.append(EvidenceItem(
            source="agent_overlap_assessment",
            finding=f"Agent judged overlap as '{o['verdict']}' for {o['existing_option'] or 'the catalog option'}: "
                    f"{o['reason']}", reference=", ".join(o["evidence_ids"]) or None))
    all_evidence = [item for _, item in evidence] + extra_evidence
    ids = [eid for eid, _ in evidence] + [f"EV-{len(evidence) + i}" for i in range(1, len(extra_evidence) + 1)]

    trace = [{"tool": name, "requested_by": res.requested_by, "ok": res.ok, "latency_ms": res.latency_ms,
              "error": res.error} for name, res in run.ctx["results"].items()]
    llm_calls = len(run.llm_trace)
    return CopilotDecision(
        request_id=request_id,
        recommendation=guardrails.recommendation_text(primary, merged),
        evidence=all_evidence,
        required_approvals=merged["approvals"],
        missing_information=assessment.missing_information,
        risk_flags=merged["flags"],
        next_step=" ".join(f"{i}) {s}" for i, s in enumerate(steps, start=1)),
        human_review_required=True,
        telemetry=RunTelemetry(llm_calls=llm_calls, tool_calls=len(trace), tool_names=[t["tool"] for t in trace]),
        architecture=architecture,
        llm_provider=note,
        model=getattr(provider, "model", None),
        primary_action=primary,
        actions=merged["actions"],
        rationale=rationale,
        overlap_assessment=judgement["overlap_assessment"],
        clarifying_questions=judgement["clarifying_questions"] if assessment.missing_information
        or judgement["overlap_assessment"]["verdict"] == "unclear" else [],
        approval_reasons=merged["approval_reasons"],
        next_steps=steps,
        evidence_ids=ids,
        guardrail_events=list(run.events),
        tool_trace=trace,
        llm_trace=run.llm_trace,
        credits_used=round(sum(c["credits"] or 0 for c in run.llm_trace), 4),
        internal_latency_ms=round((time.perf_counter() - run.started) * 1000, 1),
    )
