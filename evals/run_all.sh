#!/usr/bin/env bash
# Reproduce every evaluation result in the README.
# Usage (from the repo root):  bash evals/run_all.sh
# Takes ~45 minutes with an LLM configured (about 140 model calls, ~6 Kiro credits).
set -u
PY="${PYTHON:-.venv/bin/python}"
[ -x "$PY" ] || PY=python

echo "== Rules-only baseline (no LLM) =="
"$PY" evals/run_extended_evals.py --architecture single --provider rules --tag rules

for arch in single staged; do
  echo "== Public cases: $arch =="
  "$PY" evals/run_public_evals.py --architecture "$arch"
  echo "== Extended cases: $arch =="
  "$PY" evals/run_extended_evals.py --architecture "$arch"
done

echo "== Repeat run (stability) =="
for arch in single staged; do
  "$PY" evals/run_extended_evals.py --architecture "$arch" --tag run2
done

echo "== Comparison =="
"$PY" evals/compare_architectures.py
