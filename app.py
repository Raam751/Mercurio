"""Procurement Request Copilot - Streamlit UI.

Layout: request details | recommendation + approvals + next step, then the
evidence panel, a human decision panel, and the run audit (tools, guardrails,
model calls). The copilot only recommends; the human decision panel records a
reviewer's choice to a local audit log and never purchases or approves anything.
"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

from src import data_access
from src.llm import get_provider
from src.policy import ACTION_LABELS
from src.solution import handle_request

ROOT = Path(__file__).resolve().parent
AUDIT_LOG = ROOT / "runs" / "human_decisions.jsonl"

st.set_page_config(page_title="Procurement Request Copilot", layout="wide")

requests_by_id = {r["request_id"]: r for r in data_access.load_requests()}
employees = {e["employee_id"]: e for e in data_access.employee_records()}
provider, provider_note = get_provider()

# --------------------------------------------------------------------------- #
# Sidebar
# --------------------------------------------------------------------------- #
with st.sidebar:
    st.header("Request")
    request_id = st.selectbox(
        "Purchase request",
        list(requests_by_id),
        format_func=lambda rid: f"{rid} - {requests_by_id[rid].get('product_name') or 'unnamed'}",
    )
    architecture = st.radio(
        "Architecture",
        ["single", "staged"],
        format_func=lambda a: "A - single agent" if a == "single" else "B - staged (2 agents)",
    )
    run_clicked = st.button("Run analysis", type="primary", width="stretch")
    st.divider()
    st.caption(f"Model: {provider_note}")
    st.caption(f"Policy reference date: {data_access.get_reference_date()}")
    if provider.name == "rules-only":
        st.info("No LLM configured. Running deterministic rules only. Set KIRO_API_KEY in .env to enable the agents.")

key = f"{request_id}:{architecture}"
results = st.session_state.setdefault("results", {})
if run_clicked:
    with st.spinner("Gathering evidence and applying policy..."):
        try:
            results[key] = handle_request(request_id, architecture=architecture)
        except Exception as exc:  # noqa: BLE001 - surface any failure to the user
            st.error(f"Analysis failed: {type(exc).__name__}: {exc}")
decision = results.get(key)

# --------------------------------------------------------------------------- #
# Header
# --------------------------------------------------------------------------- #
st.title("Procurement Request Copilot")
st.caption("Recommends the next action with evidence. Every purchase decision stays with a human approver.")

req = requests_by_id[request_id]
requester = employees.get(req.get("requester_id"))
left, right = st.columns([0.9, 1.1], gap="large")

with left:
    st.subheader("Request details")
    rows = {
        "Product": req.get("product_name"),
        "Vendor": req.get("vendor_name"),
        "Category": req.get("category"),
        "Annual cost (USD)": f"{req['annual_cost_usd']:,}" if isinstance(req.get("annual_cost_usd"), (int, float)) else "Not provided",
        "Users / licenses": req.get("user_count") if req.get("user_count") else "Not provided",
        "Data access": req.get("data_access_level") or "Not provided",
        "Integrations": ", ".join(req.get("requested_integrations") or []) or "None",
        "Urgency": req.get("urgency"),
        "Requester": f"{requester['name']} ({requester['department']})" if requester else f"Unknown ({req.get('requester_id')})",
    }
    st.table(pd.DataFrame({"Field": list(rows), "Value": [str(v) for v in rows.values()]}).set_index("Field"))
    st.markdown("**Business justification** (requester text, treated as untrusted data)")
    st.text(req.get("business_justification") or "Not provided")
    if decision and "prompt_injection_detected" in decision.risk_flags:
        st.warning("This request contains embedded instructions. They were ignored and do not count as approval.")

with right:
    st.subheader("Copilot recommendation")
    if decision is None:
        st.info("Choose a request and architecture, then select **Run analysis**.")
    else:
        st.markdown(f"#### {ACTION_LABELS[decision.primary_action]}")
        st.write(decision.recommendation)
        st.markdown("**Human review required:** Yes. The copilot does not approve, purchase or change budgets.")

        c1, c2 = st.columns(2)
        with c1:
            st.markdown("**Approvals required**")
            for role in decision.required_approvals:
                st.markdown(f"- {role}")
            if not decision.required_approvals:
                st.markdown("- Cannot be determined yet (see missing information)")
        with c2:
            st.markdown("**Risk flags**")
            for flag in decision.risk_flags:
                st.markdown(f"- `{flag}`")
            if not decision.risk_flags:
                st.markdown("- None")

        if decision.missing_information:
            st.markdown("**Missing information**")
            for item in decision.missing_information:
                st.markdown(f"- {item}")
        st.markdown("**Next steps**")
        for i, step in enumerate(decision.next_steps, start=1):
            st.markdown(f"{i}. {step}")
        if decision.clarifying_questions:
            st.markdown("**Questions for the requester**")
            for q in decision.clarifying_questions:
                st.markdown(f"- {q}")
        with st.expander("Why these approvals?"):
            for role in decision.required_approvals:
                st.markdown(f"**{role}:** " + "; ".join(decision.approval_reasons.get(role, [])))
        with st.expander("Copilot rationale"):
            st.write(decision.rationale)

if decision is None:
    st.stop()

# --------------------------------------------------------------------------- #
# Evidence
# --------------------------------------------------------------------------- #
st.divider()
st.subheader("Evidence")
st.caption("Every item comes from a tool result, the policy engine, or a validated agent judgement that cites them.")
evidence_df = pd.DataFrame(
    [{"ID": eid, "Source": e.source, "Finding": e.finding, "Reference": e.reference or ""}
     for eid, e in zip(decision.evidence_ids, decision.evidence)]
)
st.dataframe(evidence_df, hide_index=True, width="stretch")

# --------------------------------------------------------------------------- #
# Human decision
# --------------------------------------------------------------------------- #
st.divider()
st.subheader("Human decision")
st.caption("Recorded to a local audit log only. This does not approve or buy anything in any system.")
with st.form("human_decision"):
    reviewer = st.text_input("Reviewer name")
    choice = st.radio(
        "Decision",
        ["Accept recommendation and route as proposed", "Request more information", "Escalate for exception review",
         "Decline request"],
    )
    note = st.text_area("Note (optional)")
    submitted = st.form_submit_button("Record decision")
if submitted:
    if not reviewer.strip():
        st.error("Enter the reviewer name before recording a decision.")
    else:
        AUDIT_LOG.parent.mkdir(exist_ok=True)
        entry = {"timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"), "request_id": request_id,
                 "architecture": architecture, "copilot_action": decision.primary_action, "reviewer": reviewer.strip(),
                 "decision": choice, "note": note.strip()}
        with AUDIT_LOG.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        st.success(f"Recorded: {choice} by {reviewer.strip()}.")

# --------------------------------------------------------------------------- #
# Run audit
# --------------------------------------------------------------------------- #
st.divider()
st.subheader("Run audit")
m1, m2, m3, m4 = st.columns(4)
m1.metric("LLM calls", decision.telemetry.llm_calls)
m2.metric("Tool calls", decision.telemetry.tool_calls)
m3.metric("Latency", f"{decision.internal_latency_ms / 1000:.1f} s")
m4.metric("Credits", f"{decision.credits_used:.3f}")

tab_tools, tab_guard, tab_llm, tab_compare, tab_json = st.tabs(
    ["Tool trace", "Guardrail events", "Model calls", "Architecture comparison", "Raw output"])
with tab_tools:
    st.caption("requested_by: agent = chosen by the LLM, guardrail = mandatory check the agent skipped, system = always run.")
    st.dataframe(pd.DataFrame(decision.tool_trace), hide_index=True, width="stretch")
with tab_guard:
    if decision.guardrail_events:
        st.dataframe(pd.DataFrame(decision.guardrail_events), hide_index=True, width="stretch")
    else:
        st.write("No guardrail interventions on this run.")
with tab_llm:
    if decision.llm_trace:
        st.dataframe(pd.DataFrame(decision.llm_trace), hide_index=True, width="stretch")
    else:
        st.write("No model calls (rules-only mode).")
with tab_compare:
    summary = ROOT / "evals" / "comparison_summary.csv"
    if summary.exists():
        with summary.open(encoding="utf-8") as f:
            st.dataframe(pd.DataFrame(list(csv.DictReader(f))), hide_index=True, width="stretch")
    else:
        st.write("Run `python evals/compare_architectures.py` to generate the comparison.")
with tab_json:
    st.json(decision.model_dump())
