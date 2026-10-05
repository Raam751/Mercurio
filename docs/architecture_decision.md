# Architecture Decision Memo

## Decision

Ship **Architecture A, the single agent**. It matched the staged two-agent design on accuracy and was 37% faster and 39% cheaper per request.

## Evidence

Same 23 strict cases run twice per architecture (46 runs each), plus the 6 public cases. Model: `claude-haiku-4.5` via Kiro CLI.

| Metric | Single agent | Staged / 2-agent |
|---|---:|---:|
| Public cases (minimum checks) | 6/6 | 6/6 |
| Strict cases passed | 44/46 | 44/46 |
| Correct primary action | 45/46 | 44/46 |
| Exact approval set | 45/46 | 46/46 |
| Flags, grounding, safety, human review | 46/46 | 46/46 |
| Same primary action in both runs | 21/23 | 18/23 |
| Avg latency | 19.5 s | 30.9 s |
| Avg LLM / tool calls | 2.0 / 9.0 | 3.0 / 9.0 |
| Credits per request | 0.051 | 0.084 |
| Unparseable model replies (safe fallback) | 2 | 1 |

A rules-only baseline (no LLM) passed 22/23.

**Failures.** Single: once rated the existing TaskFlow licence "unclear" instead of "likely covers the need". Once added a Privacy review for a vendor storing data abroad when the data class was unknown, which is conservative and arguably correct. Staged: once sent a CodeMate seat expansion to existing-tool review, and once made the same TaskFlow miss. Neither ever dropped a required approval, missed a review or claimed approval.

## Trade-offs

Code owns the policy: budget, thresholds, the 365-day review window, vendor conflicts, Security/Privacy/Legal triggers and injection screening. So both architectures score the same on every rule. The LLM's only decision-changing job is judging overlap with existing tools. The second agent did not improve it and was less consistent between runs.

The staged design adds a model call (+11 s), a handoff to validate, and a reviewer working from a summary. Its intended benefit, keeping raw request text from the decision-maker, adds little: deterministic screening and escalate-only guardrails already stopped every injection case in both.

## Risks and limitations

- **Overlap judgement varies.** Its weakest output, "unclear", still routes to Procurement, so it fails safe. Before production, measure agreement with Procurement on 50+ labelled real requests.
- **Synthetic cases.** I wrote them from my reading of the policy, not a procurement team's.
- **Regex injection screening** can miss novel phrasing. Model escalation is a second layer, not a guarantee.
- **Latency is mostly Kiro CLI overhead** (7–10 s per call, about 2 s of model time). A direct model API would bring requests near 5 s.
- **No authentication** on the local UI and API.

## Why this is the right MVP

The client needs consistent, evidence-backed routing with humans in control. The engine provides consistency; one agent adds the judgement code can't. A second agent added cost and latency without improving any measured outcome. The next improvement is better context (seat usage, licence scope), not more agents.
