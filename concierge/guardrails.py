"""Guardrails for a public-facing lending chatbot.

1. Input screening: attempts to override instructions, or requests the bot
   must never handle (rate quotes, guarantees of approval), get a fixed,
   safe reply instead of a generated one.
2. Output checking: any generated wording (for example from an optional LLM
   that rephrases an approved answer) may not introduce a number, percentage,
   or dollar amount that is not in the approved answer. If it does, the
   approved answer is used verbatim instead.
3. Sensitive data: the bot never asks for SSNs, bank details, or credit
   scores, and redacts them from transcripts if a visitor types them anyway.
"""

from __future__ import annotations

import re

INJECTION_PATTERNS = [
    r"ignore (?:all |your |the )?(?:previous|prior|above) (?:instructions|rules)",
    r"disregard (?:your|the) (?:instructions|rules|guidelines)",
    r"\bsystem prompt\b", r"\byou are now\b", r"\bpretend (?:to be|you are)\b",
    r"\bdeveloper mode\b", r"\bjailbreak\b",
]
COMMITMENT_PATTERNS = [
    r"\b(guarantee|promise|commit)\b.{0,30}\b(approv|rate|loan|terms)",
    r"\bwill i (?:be|get) approved\b", r"\bpre-?approv",
]
SENSITIVE_PATTERNS = {
    "ssn": r"\b\d{3}-\d{2}-\d{4}\b",
    "account": r"\b(?:account|routing)\s*(?:number|#|no\.?)?\s*[:\-]?\s*\d{6,17}\b",
    "card": r"\b(?:\d[ -]?){13,16}\b",
}

SAFE_REPLIES = {
    "injection": "I can only help with questions about our lending programs. What kind of property are you financing?",
    "commitment": "I can't promise approval or specific terms in chat. Every loan is reviewed by a loan officer, and I'm happy to connect you with one.",
    "sensitive": "Please don't share account numbers, Social Security numbers, or card details here. I've removed them from this conversation. A loan officer will collect anything needed through a secure process.",
}


def screen_input(text: str) -> str | None:
    """Return a guardrail category if the message needs a fixed reply."""
    lower = text.lower()
    if any(re.search(p, text) for p in SENSITIVE_PATTERNS.values()):
        return "sensitive"
    if any(re.search(p, lower) for p in INJECTION_PATTERNS):
        return "injection"
    if any(re.search(p, lower) for p in COMMITMENT_PATTERNS):
        return "commitment"
    return None


def redact(text: str) -> str:
    for label, pattern in SENSITIVE_PATTERNS.items():
        text = re.sub(pattern, f"[redacted {label}]", text)
    return text


_NUMBER_RE = re.compile(r"\$?\d[\d,]*(?:\.\d+)?\s?(?:%|k|m|million)?", re.I)


def numbers_in(text: str) -> set[str]:
    return {re.sub(r"[\s,$]", "", m.group(0).lower()).rstrip(".") for m in _NUMBER_RE.finditer(text)}


def output_is_grounded(draft: str, approved: str) -> bool:
    """True when every number in the draft also appears in the approved text."""
    return numbers_in(draft) <= numbers_in(approved)
