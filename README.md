# AI Procurement Request Copilot

An internal copilot that reviews software purchase requests. It gathers evidence with tools, applies the
procurement policy in deterministic code, and recommends the next action. **Every approval stays with a human.**

FDE Assessment 3. Two architectures were built and evaluated on the same cases:

- **A - single agent** (shipped): one agent plans the tools, then judges the evidence. 2 LLM calls per request.
- **B - staged, two agents**: an Evidence Analyst gathers and summarises; a Policy Reviewer decides from the validated brief. 3 LLM calls.

**Ship decision: Architecture A.** Same accuracy as B, 37% faster, 39% cheaper. See
[docs/architecture_decision.md](docs/architecture_decision.md).

---

## Quick start

Requires Python 3.11+ (tested on 3.13).

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python verify_setup.py          # expect PRE-FLIGHT PASSED
cp .env.example .env
python run_local.py             # one command: vendor-risk API on :8001 + UI on http://127.0.0.1:8501
```

**The app works without any API key.** With no LLM configured it runs in *rules-only* mode: the same tools,
policy engine and UI, with deterministic defaults in place of the agent's judgement.

### Enabling the LLM agents (Kiro CLI)

The agents call `claude-haiku-4.5` through Kiro CLI headless mode:

```bash
curl -fsSL https://cli.kiro.dev/install | bash     # installs kiro-cli
# in .env:
KIRO_API_KEY=ksk_...                                # Kiro portal -> API Keys (Pro+ subscription)
```

`LLM_PROVIDER=auto` (default) uses Kiro CLI when it is installed and the key is set; `LLM_PROVIDER=rules` forces
rules-only. The CLI runs the tool-free agent in [.kiro/agents/procurement-llm.md](.kiro/agents/procurement-llm.md),
so the model cannot read files, run commands or browse. Every fact it sees comes from this app's tools.

### Deployment (Docker / Hugging Face Spaces)

The [Dockerfile](Dockerfile) builds one container: Streamlit on `$PORT` (default 8501), Kiro CLI installed, and the
mock vendor-risk API started inside the same process. Pass `KIRO_API_KEY` at runtime as a secret. Without it, the
deployed app runs rules-only.

```bash
docker build -t mercurio .
docker run -p 8501:8501 --env-file .env mercurio      # http://127.0.0.1:8501
```

Any host that builds a Dockerfile and sets `$PORT` works (Railway, Google Cloud Run, Render, Koyeb, a VM). Set
`KIRO_API_KEY` as a secret environment variable in the host's dashboard.

**Sizing, measured locally with Docker limits:**

| Limits | Result |
|---|---|
| 1+ vCPU, 512 MB | Works: single-agent request in about 25 s, no out-of-memory |
| 0.1 vCPU, 512 MB (typical free tier) | Each Kiro CLI call times out after 120 s; the app falls back to rules-only after about 5 min |

A staged request peaked at about 490 MiB of process memory. Use at least **0.5–1 vCPU and 1 GB RAM** for the LLM
agents. On a 0.1-vCPU free tier, deploy with `LLM_PROVIDER=rules` instead.

`deploy/deploy_hf_space.py` deploys to a Hugging Face Docker Space, which now requires a Hugging Face PRO plan.

**The hosted app has no login, and every run uses the key owner's Kiro credits** (about 0.05 per single-agent request).

### Tests and evaluations

```bash
python -m unittest discover -s tests                          # 32 tests, no LLM needed
python evals/run_public_evals.py --architecture single        # starter harness, 6 public cases
python evals/run_public_evals.py --architecture staged
python evals/run_extended_evals.py --architecture single      # 23 strict cases
python evals/run_extended_evals.py --architecture single --provider rules --tag rules   # no-LLM baseline
bash evals/run_all.sh                                         # everything below (~45 min with an LLM)
```

---

## Product workflow

1. Pick a request in the sidebar, choose architecture A or B, and select **Run analysis**.
2. **Request details**: the structured fields, plus the requester's justification shown as untrusted text.
3. **Recommendation**: the primary action, approvals required (with the reason for each), risk flags, missing
   information, numbered next steps and questions for the requester.
4. **Evidence panel**: every finding with its source tool and the record it came from (`vendors.csv:V005`,
   `vendor-risk API GET /vendor-risk/SignalWatch`, `Policy §5`, ...).
5. **Human decision**: the reviewer records accept / request info / escalate / decline. This is written to a
   local audit log (`runs/human_decisions.jsonl`). Nothing is purchased or approved.
6. **Run audit**: LLM and tool call counts, latency, credits, the tool trace (agent-chosen vs guardrail-backfilled),
   guardrail interventions, model calls and the architecture comparison.

Output contract: `src.solution.handle_request(request_id, architecture)` returns a `CopilotDecision`, a subclass of
the starter's `ProcurementDecision` (recommendation, evidence, required_approvals, missing_information,
risk_flags, next_step, human_review_required, telemetry) with extra audit fields.

---

## Architecture

Full diagrams: [docs/architecture.md](docs/architecture.md).

| Layer | Responsible for |
|---|---|
| **Code** | Required fields, budget, approval thresholds, 365-day review currency, registry-vs-service conflicts, Security/Privacy/Legal triggers, injection screening |
| **AI** | Choosing tools, judging whether an existing tool already covers the need, explaining the decision, drafting questions, raising extra concerns |
| **Human** | Every approval, budget exception, vendor verification and policy exception |

```
request -> get_request_details -> agent plans tools -> guardrail backfills skipped mandatory checks
        -> tools run -> screen_untrusted_text -> apply_policy_rules (deterministic)
        -> agent judgement -> guardrail merge (escalate-only) -> decision -> human
```

### Tools (8 + policy engine; 7 deterministic, 1 external API)

| Tool | Kind | Source |
|---|---|---|
| `get_request_details` | deterministic | requests.json, employees.csv |
| `check_department_budget` | deterministic | department_budgets.csv |
| `search_software_catalog` | deterministic (agent can add keywords) | software_catalog.csv |
| `get_purchase_history` | deterministic | purchase_history.csv |
| `get_vendor_registry_record` | deterministic | vendors.csv |
| `get_vendor_risk_assessment` | external API | mock vendor-risk service |
| `lookup_policy_sections` | retrieval | procurement_policy.md |
| `screen_untrusted_text` | deterministic | request + vendor + catalog free text |
| `apply_policy_rules` | deterministic engine | `src/policy.py` |

### Agents

| | Architecture A | Architecture B |
|---|---|---|
| Call 1 | Agent: choose tools | Evidence Analyst: choose tools |
| Call 2 | Agent: judge evidence + engine results | Evidence Analyst: neutral brief, each fact cites an evidence ID |
| Call 3 | - | Policy Reviewer: decide from the validated brief + structured fields only |

### Human controls and reliability

- `human_review_required` is always `true`. The copilot cannot approve, purchase, change budgets or accept terms.
- The agent can only **escalate**: add an existing-tool review, a risk flag or a review. It can never remove an
  approval, flag or missing-information item computed by the engine.
- Each evidence ID the agent cites must exist. Text claiming an approval or repeating an embedded instruction is
  discarded and replaced with a deterministic rationale.
- Record identifiers in tool arguments (vendor, requester) are bound by code, never taken from the model.
- Mandatory checks the agent skips are run anyway and logged.
- A vendor-risk outage (503/timeout) gets one retry and is then flagged `vendor_risk_unavailable`. Security is
  added and the request is held for manual verification. A favourable status is never assumed.
- If a model call fails or returns invalid JSON, that step falls back to deterministic output and the run completes.

### Starter-pack issues found and fixed

| Issue | Fix |
|---|---|
| Blank CSV cells loaded as `NaN`, which is truthy, so a missing review date looked present | `data_access.py` converts to `None` and strips strings |
| Eval runner never started the mock API, so vendor lookups failed unless `run_local.py` was also running | `src/mock_service.py` starts it in-process on localhost if nothing is listening (`AUTO_START_MOCK_API=0` disables) |
| `vendor_client` raised the same error for an outage, an unknown vendor and a down service | `fetch_vendor_risk()` returns `ok` / `not_found` / `unavailable` with a retry |
| Mock API URL-decoded vendor names twice | decode once, double-decode only as a fallback |
| `.gitignore` excluded `evals/results_*.csv`, which the submission must include | removed |
| Policy boundary trap: Finance starts at $10,000.01 but new-vendor Legal at $10,000 | separate rules, tested at the boundaries |

---

## Assumptions

- Dates are checked against the policy's reference date **2026-09-30**, read from `procurement_policy.md`, not the clock.
- A review is current for 365 days: exactly 365 days old is current, 366 is expired.
- The department and budget come from the requester's employee record, not from request text.
- "New vendor" means the registry's procurement status is not `Approved` (or the vendor is not in the registry).
- Legal terms count as approved only if the registry says `Approved` or `Standard`. Missing terms trigger Legal.
- A catalog match on vendor *and* category, or on category alone, is surfaced as overlap. Vendor-only matches
  (for example a training pack from the same vendor) are not.
- `monthly_cost_usd` is annualised x12 if no annual cost is given. `data_access_level: unknown` counts as missing.
- A justification is treated as missing if fewer than 4 real words remain after removing embedded instructions.
- Approvals use the role names suggested in `contracts.py`.

---

## Evaluation results

Model `claude-haiku-4.5` via Kiro CLI. The **strict set** ([evals/extended_cases.json](evals/extended_cases.json))
has 23 cases: all 10 shipped requests plus 13 synthetic hidden-style cases. They cover the $1,000 / $10,000 /
$25,000.01 boundaries, a review exactly 365 days old, an unknown vendor, injection in vendor-API notes,
monthly-only cost, an unknown requester, and the whole vendor service down. Each case checks the primary action,
the **exact** approval set, required and forbidden flags, missing information, grounding (every evidence item
comes from a tool that ran and points to a real record) and safety. Each architecture ran the set **twice**.

| Metric | A - single agent | B - staged (2 agents) | Rules only |
|---|---:|---:|---:|
| Public cases (starter harness) | 6/6 | 6/6 | 6/6 |
| Strict cases passed (2 runs) | **44/46** | **44/46** | 22/23 |
| Correct primary action | 45/46 | 44/46 | 22/23 |
| Exact approval set | 45/46 | 46/46 | 23/23 |
| Flags / grounding / safety / human review | 46/46 | 46/46 | 23/23 |
| Same primary action in both runs | 21/23 | 18/23 | - |
| Avg latency per request | **19.5 s** | 30.9 s | 0.1 s |
| Avg LLM calls | 2.0 | 3.0 | 0 |
| Avg tool calls | 9.0 | 9.0 | 8.0 |
| Kiro credits per request | **0.051** | 0.084 | 0 |
| Unparseable model replies (safe fallback) | 2 | 1 | - |
| Mandatory tools the agent skipped (backfilled) | 2 | 0 | - |

Raw results: `evals/results_*.csv`, per-case outputs in `evals/runs/`, the template-format table in
[evals/evaluation_results.csv](evals/evaluation_results.csv), and the summary in [evals/comparison.md](evals/comparison.md).

**Failures, by hand-review:**
- A, run 2, EXT-08: rated TaskFlow overlap "unclear" rather than "likely covered". It still told Procurement to confirm the gap.
- A, run 2, EXT-06: added a Privacy review for a vendor that stores data abroad when the data class is unknown. Conservative and defensible, but outside the expected set.
- B, run 1, EXT-03: sent a CodeMate seat expansion to existing-tool review (over-escalation).
- B, run 2, EXT-08: same as A.
- Rules only, EXT-08: cannot judge overlap, so it misses the existing-tool review. This is the main value the LLM adds.

**What this shows:** because code owns the policy, both architectures are perfect on approvals, flags, grounding
and safety. The only LLM decision that changes the outcome is the overlap judgement, and the second agent did not
improve it. It was slower, more expensive and less consistent between runs.

Note: a JSON-repair step for common model slips (bare `EV-3` IDs, trailing commas) was added *after* these runs to
address the 3 unparseable replies. The numbers above predate it.

---

## Known limitations

- The overlap judgement varies between runs. Its weakest output still routes to Procurement, so it fails safe.
- The strict cases were written by me from the policy text. They are not labelled by a procurement team.
- Injection screening is pattern-based and can miss novel phrasing. The model's escalation is a second, weaker layer.
- About 20 s per request, mostly Kiro CLI startup (7–10 s per call against about 2 s of model time). A direct model API would be much faster.
- Reproducing the LLM results needs Kiro CLI and a Kiro subscription. Everything else runs in rules-only mode.
- The local UI and API have **no authentication**. Production would need SSO and role-based access.
- No seat-utilisation data, so "unused existing capacity" (policy §3) can only be raised as a question.

## Repository layout

```
src/
  agents.py        orchestration for A and B, CopilotDecision
  policy.py        deterministic policy engine
  tools.py         tool implementations + registry
  guardrails.py    plan/brief/judgement validation and escalate-only merge
  prompts.py       prompt templates (untrusted-data fencing)
  llm.py           Kiro CLI provider + rules-only fallback
  screening.py     prompt-injection screening
  data_access.py   NaN-safe loaders, policy reference date
  vendor_client.py structured vendor-risk client
  mock_service.py  on-demand local mock API
  solution.py      handle_request adapter
app.py             Streamlit UI
evals/             public + strict cases, runners, results, comparison
docs/              architecture diagrams, decision memo
tests/             starter integrity tests + 22 engine/guardrail tests
.kiro/agents/      tool-free Kiro agent definition
```
