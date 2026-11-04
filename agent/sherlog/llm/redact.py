"""Scrub secrets and personal data from text before it reaches a model.

Logs, exceptions and tool results are copied into prompts, and they can carry
tokens, passwords, emails or card numbers. Every message goes through
`redact()` on its way out, whichever model it is for.
"""

import re

_PATTERNS: list[tuple[re.Pattern, str | None]] = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "[PRIVATE KEY]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"), "[JWT]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{8,}"), "Bearer [REDACTED]"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "[AWS KEY]"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"), "[GITHUB TOKEN]"),
    (re.compile(r"\b(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{16,}\b"), "[API KEY]"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"), "[API KEY]"),
    (re.compile(r"\bslg_[A-Za-z0-9_-]{20,}\b"), "[SHERLOG KEY]"),
    # key=value / "key": "value" pairs whose name says secret
    (
        re.compile(
            r"(?i)((?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|authorization|cookie)"
            r"[\"']?\s*[:=]\s*[\"']?)([^\s\"',;&}]{4,})"
        ),
        None,
    ),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[EMAIL]"),
]

_CARD = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")


def _luhn(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        n = int(ch)
        if i % 2:
            n = n * 2 - 9 if n > 4 else n * 2
        total += n
    return total % 10 == 0


def _card(match: re.Match) -> str:
    digits = re.sub(r"[ -]", "", match.group(0))
    return "[CARD]" if 13 <= len(digits) <= 19 and _luhn(digits) else match.group(0)


def redact(text: str) -> str:
    for pattern, replacement in _PATTERNS:
        if replacement is None:
            text = pattern.sub(lambda m: m.group(1) + "[REDACTED]", text)
        else:
            text = pattern.sub(replacement, text)
    return _CARD.sub(_card, text)


def redact_message(message: dict) -> dict:
    if isinstance(message.get("content"), str):
        return {**message, "content": redact(message["content"])}
    return message


# Text in telemetry that reads like instructions to an AI. It is still passed
# to the model as data, but the incident is flagged and nothing runs on its own.
INJECTION = re.compile(
    r"(?i)(ignore (all |any )?(previous|prior|above|earlier) (instructions|prompts?)"
    r"|disregard (all |any )?(previous|prior|above|your) (instructions|rules)"
    r"|you are now\b|new instructions:|system prompt|<\|im_start\|>|\[/?INST\]|recommended_action)"
)


def looks_like_injection(text: str) -> str | None:
    match = INJECTION.search(text)
    return match.group(0) if match else None


def scan_evidence(evidence: dict) -> str | None:
    """The first instruction-like text in the free-text parts of an evidence pack."""
    texts = []
    for t in evidence.get("log_templates") or []:
        texts.append(t.get("template") or "")
        texts += [e.get("line") or "" for e in t.get("examples") or []]
    for e in evidence.get("exceptions") or []:
        texts += [e.get("message") or "", e.get("stack") or ""]
    texts += [c.get("commit_message") or "" for c in evidence.get("changes") or []]
    for text in texts:
        hit = looks_like_injection(text)
        if hit:
            return hit
    return None
