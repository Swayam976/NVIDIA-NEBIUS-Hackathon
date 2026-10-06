"""Showing model-produced text to the human who approves it.

A diff or tool argument can carry characters that change how text LOOKS
(ANSI escapes, carriage returns, bidi overrides that reorder a line), so
what the reviewer sees would differ from what gets written. Both approval
surfaces (CLI prompt, web panel) show such characters as visible escapes.
"""

from __future__ import annotations

import re

# C0/C1 controls except tab and newline, line/paragraph separators, and the
# bidi embedding/override/isolate controls (U+202A-202E, U+2066-2069).
_CONTROL_RE = re.compile("[\\x00-\\x08\\x0b-\\x1f\\x7f-\\x9f\\u2028\\u2029\\u202a-\\u202e\\u2066-\\u2069\\u200e\\u200f]")


def visible(text: str) -> str:
    """Control and bidi characters shown as \\xNN / \\uNNNN instead of being
    interpreted; everything else unchanged."""
    def esc(m: re.Match) -> str:
        code = ord(m.group())
        return f"\\x{code:02x}" if code < 256 else f"\\u{code:04x}"
    return _CONTROL_RE.sub(esc, text)
