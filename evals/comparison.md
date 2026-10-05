# Architecture comparison

| Metric | A - single agent | B - staged (2 agents) | Rules only (no LLM) |
|---|---:|---:|---:|
| public_min_checks_passed | 6/6 | 6/6 | n/a |
| strict_cases_passed | 23/23 | 22/23 | 22/23 |
| primary_action | 23/23 | 22/23 | 22/23 |
| approvals | 23/23 | 23/23 | 23/23 |
| flags | 23/23 | 23/23 | 23/23 |
| missing_info | 23/23 | 23/23 | 23/23 |
| human_review | 23/23 | 23/23 | 23/23 |
| grounding | 23/23 | 23/23 | 23/23 |
| safety | 23/23 | 23/23 | 23/23 |
| avg_latency_s | 19.5 | 30.1 | 0.1 |
| median_latency_s | 19.3 | 30.1 | 0.0 |
| p_max_latency_s | 27.6 | 33.4 | 0.8 |
| avg_llm_calls | 2.0 | 3.0 | 0.0 |
| avg_tool_calls | 9.0 | 9.0 | 8.0 |
| total_credits | 1.183 | 1.866 | 0.000 |
| tools_backfilled | 1 | 0 | 0 |
| llm_output_rejections | 4 | 4 | 0 |
| llm_escalations | 4 | 7 | 0 |
| llm_failures | 0 | 1 | 0 |
| run2_strict_cases_passed | 21/23 | 22/23 | n/a |
| same_primary_action_both_runs | 21/23 | 18/23 | n/a |

## Failed strict cases

- **A - single agent** run 1: none
- **A - single agent** run 2: EXT-06: approvals missing [] extra ['Privacy']; EXT-08: primary STANDARD_APPROVAL, expected one of ['REVIEW_EXISTING_TOOL']
- **B - staged (2 agents)** run 1: EXT-03: primary REVIEW_EXISTING_TOOL, expected one of ['SPECIALIST_REVIEW']
- **B - staged (2 agents)** run 2: EXT-08: primary STANDARD_APPROVAL, expected one of ['REVIEW_EXISTING_TOOL']
- **Rules only (no LLM)** run 1: EXT-08: primary STANDARD_APPROVAL, expected one of ['REVIEW_EXISTING_TOOL']

## Per-case primary action and overlap verdict

| Case | Single: action | Single: overlap | Staged: action | Staged: overlap |
|---|---|---|---|---|
| EXT-01 | REVIEW_EXISTING_TOOL | likely_covered | STANDARD_APPROVAL | unclear |
| EXT-02 | SPECIALIST_REVIEW | credible_gap | SPECIALIST_REVIEW | unclear |
| EXT-03 | SPECIALIST_REVIEW | unclear | REVIEW_EXISTING_TOOL | likely_covered |
| EXT-04 | SPECIALIST_REVIEW | unclear | SPECIALIST_REVIEW | unclear |
| EXT-05 | BUDGET_EXCEPTION | not_applicable | BUDGET_EXCEPTION | not_applicable |
| EXT-06 | REQUEST_CLARIFICATION | likely_covered | REQUEST_CLARIFICATION | unclear |
| EXT-07 | VERIFY_VENDOR | unclear | VERIFY_VENDOR | likely_covered |
| EXT-08 | REVIEW_EXISTING_TOOL | likely_covered | REVIEW_EXISTING_TOOL | likely_covered |
| EXT-09 | VERIFY_VENDOR | not_applicable | VERIFY_VENDOR | not_applicable |
| EXT-10 | STANDARD_APPROVAL | not_applicable | STANDARD_APPROVAL | not_applicable |
| EXT-11 | REVIEW_EXISTING_TOOL | likely_covered | STANDARD_APPROVAL | unclear |
| EXT-12 | STANDARD_APPROVAL | not_applicable | STANDARD_APPROVAL | not_applicable |
| EXT-13 | SPECIALIST_REVIEW | not_applicable | SPECIALIST_REVIEW | not_applicable |
| EXT-14 | STANDARD_APPROVAL | unclear | STANDARD_APPROVAL | unclear |
| EXT-15 | SPECIALIST_REVIEW | unclear | REVIEW_EXISTING_TOOL | likely_covered |
| EXT-16 | VERIFY_VENDOR | not_applicable | VERIFY_VENDOR | not_applicable |
| EXT-17 | SPECIALIST_REVIEW | not_applicable | SPECIALIST_REVIEW | not_applicable |
| EXT-18 | SPECIALIST_REVIEW | not_applicable | SPECIALIST_REVIEW | not_applicable |
| EXT-19 | STANDARD_APPROVAL | not_applicable | STANDARD_APPROVAL | not_applicable |
| EXT-20 | REQUEST_CLARIFICATION | not_applicable | REQUEST_CLARIFICATION | not_applicable |
| EXT-21 | SPECIALIST_REVIEW | unclear | REVIEW_EXISTING_TOOL | likely_covered |
| EXT-22 | SPECIALIST_REVIEW | unclear | SPECIALIST_REVIEW | unclear |
| EXT-23 | VERIFY_VENDOR | unclear | VERIFY_VENDOR | unclear |
