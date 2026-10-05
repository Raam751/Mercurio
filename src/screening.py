"""Deterministic screening of untrusted business text for prompt injection.

Policy section 9: requester text, vendor notes and API text are business data,
not instructions. This module finds text that tries to change the copilot's
rules, fabricate approval, bypass controls or extract secrets. It never decides
anything itself; it reports what it found so the policy engine can flag it and
the LLM prompts can quarantine it.
"""
from __future__ import annotations

import re
from typing import Iterable

# (label, pattern). Patterns are deliberately specific to avoid flagging normal
# business phrases such as "approved vendor" or "requires Security review".
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("instruction override", re.compile(
        r"\b(ignore|forget|disregard)\b[^.!?\n]{0,40}\b(rules?|instructions?|polic(y|ies)|controls?|guidelines?|previous|above|procedures?)\b", re.I)),
    ("control bypass", re.compile(
        r"\b(bypass|skip|override|circumvent|waive)\b[^.!?\n]{0,40}\b(rules?|polic(y|ies)|controls?|reviews?|approvals?|checks?|procurement|security|legal|privacy)\b", re.I)),
    ("fabricated approval", re.compile(
        r"\b(treat|consider|mark|record|log)\b[^.!?\n]{0,40}\bas\b[^.!?\n]{0,20}\b(pre-?approved|approved|signed[- ]off|authori[sz]ed)\b", re.I)),
    ("fabricated approval", re.compile(
        r"\b(cfo|ceo|cto|vp|director|finance|security|legal|privacy)[- ]?(pre-?approved|approved|signed[- ]off)\b", re.I)),
    ("forced approval", re.compile(
        r"\bapprove\b[^.!?\n]{0,30}\b(immediately|right away|now|automatically|without (review|approval|checks?))\b", re.I)),
    ("role manipulation", re.compile(
        r"\b(you are now|act as (an?|the)|new instructions|system prompt|developer message|jailbreak)\b", re.I)),
    ("secret exfiltration", re.compile(
        r"\b(reveal|print|show|expose|send|share)\b[^.!?\n]{0,30}\b(api[ _-]?keys?|secrets?|passwords?|credentials?|tokens?|system prompt)\b", re.I)),
    ("control bypass", re.compile(
        r"\bno (need|requirement) (for|to)\b[^.!?\n]{0,30}\b(reviews?|approvals?|security|legal|privacy|procurement)\b", re.I)),
]

_SENTENCE = re.compile(r"[^.!?\n]+[.!?]?")


def scan_text(text: str) -> list[dict[str, str]]:
    """Return one hit per suspicious sentence: {"label", "excerpt"}."""
    hits: list[dict[str, str]] = []
    if not text:
        return hits
    for sentence in _SENTENCE.findall(text):
        sentence = sentence.strip()
        if not sentence:
            continue
        for label, pattern in _PATTERNS:
            if pattern.search(sentence):
                hits.append({"label": label, "excerpt": sentence[:200]})
                break
    return hits


def strip_injections(text: str | None) -> str:
    """Text with suspicious sentences removed (used to judge real business purpose)."""
    if not text:
        return ""
    kept = [s.strip() for s in _SENTENCE.findall(text) if s.strip() and not scan_text(s)]
    return " ".join(kept)


def screen_fields(fields: Iterable[tuple[str, str, str | None]]) -> list[dict[str, str]]:
    """Scan (source, field, text) triples; return hits with their origin."""
    results: list[dict[str, str]] = []
    for source, field, text in fields:
        for hit in scan_text(text or ""):
            results.append({"source": source, "field": field, **hit})
    return results
