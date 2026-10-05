"""Aggregate evaluation CSVs into the architecture comparison.

Reads:  evals/results_{single,staged}.csv              (public harness)
        evals/results_extended_{single,staged}.csv     (strict set)
        evals/results_extended_single_rules.csv        (no-LLM baseline, optional)
Writes: evals/comparison_summary.csv and evals/comparison.md
"""
from __future__ import annotations

import csv
from pathlib import Path
from statistics import mean, median

ROOT = Path(__file__).resolve().parents[1]
EVALS = ROOT / "evals"
CHECKS = ["primary_action_ok", "approvals_ok", "flags_ok", "missing_info_ok", "human_review_ok", "grounding_ok",
          "safety_ok"]


def _read(name: str) -> list[dict]:
    path = EVALS / name
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _true(value: str) -> bool:
    return str(value).strip().lower() == "true"


def _num(rows: list[dict], key: str) -> list[float]:
    out = []
    for r in rows:
        try:
            out.append(float(r[key]))
        except (KeyError, TypeError, ValueError):
            pass
    return out


def summarise(label: str, public: list[dict], extended: list[dict]) -> dict:
    lat = _num(extended, "latency_ms")
    row = {
        "architecture": label,
        "public_min_checks_passed": f"{sum(_true(r['passed_minimum_checks']) for r in public)}/{len(public)}" if public else "n/a",
        "strict_cases_passed": f"{sum(_true(r['passed']) for r in extended)}/{len(extended)}" if extended else "n/a",
    }
    for check in CHECKS:
        row[check.replace("_ok", "")] = f"{sum(_true(r.get(check)) for r in extended)}/{len(extended)}" if extended else "n/a"
    row.update({
        "avg_latency_s": f"{mean(lat) / 1000:.1f}" if lat else "n/a",
        "median_latency_s": f"{median(lat) / 1000:.1f}" if lat else "n/a",
        "p_max_latency_s": f"{max(lat) / 1000:.1f}" if lat else "n/a",
        "avg_llm_calls": f"{mean(_num(extended, 'llm_calls')):.1f}" if extended else "n/a",
        "avg_tool_calls": f"{mean(_num(extended, 'tool_calls')):.1f}" if extended else "n/a",
        "total_credits": f"{sum(_num(extended, 'credits')):.3f}" if extended else "n/a",
        "tools_backfilled": str(int(sum(_num(extended, "tools_backfilled")))) if extended else "n/a",
        "llm_output_rejections": str(int(sum(_num(extended, "llm_output_rejections")))) if extended else "n/a",
        "llm_escalations": str(int(sum(_num(extended, "llm_escalations")))) if extended else "n/a",
        "llm_failures": str(int(sum(_num(extended, "llm_failures")))) if extended else "n/a",
        "failed_cases": "; ".join(f"{r['case_id']}: {r['failures']}" for r in extended if not _true(r["passed"])) or "none",
    })
    return row


def main() -> None:
    rows = [
        summarise("A - single agent", _read("results_single.csv"), _read("results_extended_single.csv")),
        summarise("B - staged (2 agents)", _read("results_staged.csv"), _read("results_extended_staged.csv")),
    ]
    rules = _read("results_extended_single_rules.csv")
    if rules:
        rows.append(summarise("Rules only (no LLM)", [], rules))

    # Optional repeat run to measure run-to-run stability of the LLM judgement.
    for row, arch in ((rows[0], "single"), (rows[1], "staged")):
        first = {r["case_id"]: r for r in _read(f"results_extended_{arch}.csv")}
        second = {r["case_id"]: r for r in _read(f"results_extended_{arch}_run2.csv")}
        if second:
            same = sum(first[c]["primary_action"] == second[c]["primary_action"] for c in second if c in first)
            row["run2_strict_cases_passed"] = f"{sum(_true(r['passed']) for r in second.values())}/{len(second)}"
            row["same_primary_action_both_runs"] = f"{same}/{len(second)}"
            row["run2_failed_cases"] = "; ".join(f"{c}: {r['failures']}" for c, r in second.items()
                                                 if not _true(r["passed"])) or "none"
    for row in rows:
        for key in ("run2_strict_cases_passed", "same_primary_action_both_runs", "run2_failed_cases"):
            row.setdefault(key, "n/a")

    # Per-case results in the starter pack's template format (templates/evaluation_results_template.csv).
    template_rows = []
    for arch in ("single", "staged"):
        for r in _read(f"results_extended_{arch}.csv"):
            template_rows.append({
                "case_id": r["case_id"], "architecture": arch,
                "correct_next_action": r.get("primary_action_ok"),
                "grounded_evidence": r.get("grounding_ok"),
                "policy_followed": str(all(_true(r.get(k)) for k in ("approvals_ok", "flags_ok", "missing_info_ok"))),
                "human_escalation_correct": str(_true(r.get("human_review_ok")) and _true(r.get("safety_ok"))),
                "latency_ms": r.get("latency_ms"), "llm_calls": r.get("llm_calls"), "tool_calls": r.get("tool_calls"),
                "notes": r.get("failures") or r.get("title", ""),
            })
    if template_rows:
        with (EVALS / "evaluation_results.csv").open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=list(template_rows[0]))
            writer.writeheader()
            writer.writerows(template_rows)

    with (EVALS / "comparison_summary.csv").open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    metrics = [k for k in rows[0] if k not in {"architecture", "failed_cases", "run2_failed_cases"}]
    lines = ["# Architecture comparison", "", "| Metric | " + " | ".join(r["architecture"] for r in rows) + " |",
             "|---|" + "---:|" * len(rows)]
    for m in metrics:
        lines.append(f"| {m} | " + " | ".join(r[m] for r in rows) + " |")
    lines += ["", "## Failed strict cases", ""]
    for r in rows:
        lines.append(f"- **{r['architecture']}** run 1: {r['failed_cases']}")
        if r["run2_failed_cases"] != "n/a":
            lines.append(f"- **{r['architecture']}** run 2: {r['run2_failed_cases']}")

    single = {r["case_id"]: r for r in _read("results_extended_single.csv")}
    staged = {r["case_id"]: r for r in _read("results_extended_staged.csv")}
    lines += ["", "## Per-case primary action and overlap verdict", "",
              "| Case | Single: action | Single: overlap | Staged: action | Staged: overlap |", "|---|---|---|---|---|"]
    for cid in sorted(set(single) | set(staged)):
        s, t = single.get(cid, {}), staged.get(cid, {})
        lines.append(f"| {cid} | {s.get('primary_action', '')} | {s.get('overlap_verdict', '')} | "
                     f"{t.get('primary_action', '')} | {t.get('overlap_verdict', '')} |")
    (EVALS / "comparison.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))


if __name__ == "__main__":
    main()
