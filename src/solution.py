from __future__ import annotations

from src.contracts import Architecture, ProcurementDecision


def handle_request(request_id: str, architecture: Architecture = "single") -> ProcurementDecision:
    """Assessment adapter used by the evaluation harness and the UI.

    ``architecture="single"`` runs Architecture A (one agent, 2 LLM calls).
    ``architecture="staged"`` runs Architecture B (Evidence Analyst + Policy Reviewer, 3 LLM calls).

    Returns a ``CopilotDecision``, a subclass of ``ProcurementDecision`` with
    extra audit fields (tool trace, guardrail events, LLM trace).
    """
    from src.agents import run_request  # local import keeps module import cheap for the harness

    return run_request(request_id, architecture)
