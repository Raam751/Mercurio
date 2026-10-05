"""LLM provider layer.

Two providers sit behind one small interface so the rest of the copilot does not
care which is active:

* ``KiroCLIProvider`` - calls a model through Kiro CLI in headless mode, using
  ``KIRO_API_KEY``. The CLI runs the tool-free agent defined in
  ``.kiro/agents/procurement-llm.md``: it cannot read files, run commands or
  browse, so every fact the model sees comes from our own tools via the prompt.
* ``RulesOnlyProvider`` - no model. The orchestrator uses deterministic
  defaults instead. This keeps the product runnable without credentials and is
  the fallback if the model fails mid-run.

Selection (``LLM_PROVIDER``): ``auto`` (default) uses Kiro CLI when the binary
and ``KIRO_API_KEY`` are available, otherwise rules-only. ``kiro`` or ``rules``
force one. ``MODEL_NAME`` picks the model (default ``claude-haiku-4.5``).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
AGENT_NAME = "procurement-llm"
DEFAULT_MODEL = "claude-haiku-4.5"


class LLMError(RuntimeError):
    """The model call failed or returned something unusable."""


@dataclass
class LLMResponse:
    text: str
    latency_ms: float
    credits: float | None
    model: str
    purpose: str


def _redact(text: str) -> str:
    return re.sub(r"ksk_[A-Za-z0-9_\-]+", "ksk_[REDACTED]", text or "")


def _repair(text: str) -> str:
    """Fix the JSON slips small models make most often. Only applied after a strict parse fails."""
    text = re.sub(r'(?<!["\w-])(EV-\d+)(?!["\w])', r'"\1"', text)          # bare evidence IDs
    text = re.sub(r"<\s*true\s*\|\s*false\s*>", "false", text)              # echoed schema placeholder
    text = re.sub(r",\s*([}\]])", r"\1", text)                               # trailing commas
    return text


def parse_json_reply(text: str) -> dict[str, Any]:
    """Extract the first JSON object from a reply (tolerates ```json fences and prose).

    Strict parsing first; if that fails, one repair pass for common slips (bare
    EV-n IDs, trailing commas). Anything still invalid raises ``LLMError`` and
    the caller falls back to deterministic output.
    """
    cleaned = re.sub(r"```(?:json)?", "", text or "").strip()
    start = cleaned.find("{")
    if start < 0:
        raise LLMError("reply contained no JSON object")
    decoder = json.JSONDecoder()
    try:
        obj, _end = decoder.raw_decode(cleaned[start:])
    except json.JSONDecodeError as exc:
        try:
            obj, _end = decoder.raw_decode(_repair(cleaned[start:]))
        except json.JSONDecodeError:
            raise LLMError(f"reply JSON could not be parsed: {exc}") from exc
    if not isinstance(obj, dict):
        raise LLMError("reply JSON was not an object")
    return obj


class KiroCLIProvider:
    name = "kiro-cli"

    def __init__(self, model: str | None = None, timeout_seconds: float = 120.0) -> None:
        self.model = model or os.getenv("MODEL_NAME") or DEFAULT_MODEL
        self.timeout_seconds = timeout_seconds
        self.binary = shutil.which("kiro-cli") or str(Path.home() / ".local" / "bin" / "kiro-cli")

    def availability(self) -> tuple[bool, str]:
        if not Path(self.binary).exists():
            return False, "kiro-cli is not installed"
        if not os.getenv("KIRO_API_KEY"):
            return False, "KIRO_API_KEY is not set"
        if not (ROOT / ".kiro" / "agents" / f"{AGENT_NAME}.md").exists():
            return False, f"agent file .kiro/agents/{AGENT_NAME}.md is missing"
        return True, "ok"

    def complete(self, prompt: str, *, purpose: str) -> LLMResponse:
        cmd = [self.binary, "chat", "--no-interactive", "--v3", "--agent", AGENT_NAME, "--model", self.model,
               "--output-format", "stream-json", "--trust-tools="]
        start = time.perf_counter()
        try:
            proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, cwd=ROOT,
                                  timeout=self.timeout_seconds, env=os.environ.copy())
        except subprocess.TimeoutExpired as exc:
            raise LLMError(f"model call timed out after {self.timeout_seconds:.0f}s") from exc
        except OSError as exc:
            raise LLMError(f"could not start kiro-cli: {exc}") from exc
        latency_ms = (time.perf_counter() - start) * 1000

        final_text, status, credits = None, None, None
        for line in proc.stdout.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            data = event.get("data") or {}
            if event.get("type") == "runFinished":
                final_text, status = data.get("finalText"), data.get("status")
            meta = ((data.get("update") or {}).get("_meta") or {}).get("kiro") or {}
            if meta.get("kind") == "turn_completion":
                credits = sum(s.get("usage", 0) for s in meta.get("promptTurnSummaries", []))
        if proc.returncode != 0 or status != "success" or not final_text:
            tail = _redact(proc.stderr)[-400:]
            raise LLMError(f"kiro-cli failed (exit {proc.returncode}, status {status}): {tail}")
        return LLMResponse(final_text, round(latency_ms, 1), credits, self.model, purpose)


class RulesOnlyProvider:
    name = "rules-only"
    model = None

    def availability(self) -> tuple[bool, str]:
        return True, "ok"

    def complete(self, prompt: str, *, purpose: str) -> LLMResponse:  # pragma: no cover - never called
        raise LLMError("rules-only mode has no model")


def get_provider() -> tuple[KiroCLIProvider | RulesOnlyProvider, str]:
    """Return (provider, note explaining the choice)."""
    choice = (os.getenv("LLM_PROVIDER") or "auto").strip().lower()
    if choice == "rules":
        return RulesOnlyProvider(), "LLM_PROVIDER=rules"
    kiro = KiroCLIProvider()
    ok, reason = kiro.availability()
    if ok:
        return kiro, f"Kiro CLI, model {kiro.model}"
    note = f"rules-only fallback ({reason})"
    return RulesOnlyProvider(), note
