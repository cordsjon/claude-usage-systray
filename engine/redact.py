"""Redaction sweep for unmatched user-message excerpts (US-TB-01 AC-04).

Strips credentials, paths, emails, and bearer tokens before persisting
prompt_unmatched.text_excerpt. Truncates to 200 chars.
"""

import re

_PATTERNS = [
    (re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "<email>"),
    (re.compile(r"(?:/Users/|/home/|C:\\\\)[^\s'\"]+"), "<path>"),
    (re.compile(r"[Bb]earer\s+[A-Za-z0-9._\-]+"), "Bearer <token>"),
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{10,}"), "<token>"),
]


def redact_for_unmatched(text):
    """Return a redacted, length-capped version of ``text`` safe to persist."""
    out = text
    for pat, repl in _PATTERNS:
        out = pat.sub(repl, out)
    if len(out) > 200:
        out = out[:197] + "..."
    return out
