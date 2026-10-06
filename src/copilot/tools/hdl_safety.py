"""Which HDL constructs can reach files or processes.

Shared by the web demo's source check (sandbox.check_hdl_sources: allowlist
over every source) and verify_loop (new_risky_features: refuses a model edit
that ADDS file/process access, before it is ever simulated). Both scan the
raw text, comments and strings included: a stripping pass can be fooled.
"""

from __future__ import annotations

import re
from collections import Counter

# System tasks/functions with no file or process access.
ALLOWED_SYSTEM_TASKS = frozenset(
    {
        "display", "displayb", "displayh", "displayo",
        "write", "writeb", "writeh", "writeo",
        "strobe", "monitor", "monitoron", "monitoroff",
        "finish", "stop", "time", "stime", "realtime", "timeformat", "printtimescale",
        "random", "urandom", "urandom_range", "dist_uniform",
        "signed", "unsigned", "clog2", "bits", "size",
        "itor", "rtoi", "bitstoreal", "realtobits",
        "error", "warning", "info", "fatal",
        "sformat", "sformatf", "test$plusargs", "value$plusargs",
    }
)
# Pure SystemVerilog functions a design edit may reasonably add (verify_loop
# only; the public demo keeps the shorter list above).
_PURE_SV_FUNCTIONS = frozenset(
    {
        "past", "rose", "fell", "stable", "changed", "onehot", "onehot0", "countones", "countbits",
        "isunknown", "left", "right", "low", "high", "increment", "dimensions", "unpacked_dimensions",
        "typename", "cast", "ceil", "floor", "sqrt", "ln", "log10", "exp", "pow", "abs",
    }
)
# Directives that can't pull in files or build tokens.
ALLOWED_DIRECTIVES = frozenset(
    {
        "timescale", "define", "undef", "ifdef", "ifndef", "elsif", "else", "endif",
        "default_nettype", "resetall", "celldefine", "endcelldefine",
    }
)
# Directive names a user macro may never take (`define include ... would
# otherwise make a later real `include look like a macro use).
RESERVED_DIRECTIVES = ALLOWED_DIRECTIVES | {
    "include", "line", "pragma", "begin_keywords", "end_keywords", "undefineall",
    "unconnected_drive", "nounconnected_drive", "protect", "endprotect", "__FILE__", "__LINE__",
}
DEFINE_RE = re.compile(r"`\s*define\s+([A-Za-z_]\w*)")
DIRECTIVE_RE = re.compile(r"`\s*([A-Za-z_]\w*)")
SYSTASK_RE = re.compile(r"\$([A-Za-z_][\w$]*)")

_STRING = r'"(?:\\.|[^"\\\n])*"'
_TASK_CALL_RE = re.compile(rf"\$([A-Za-z_][\w$]*)\s*(?:\(\s*({_STRING})?)?")
_DIRECTIVE_CALL_RE = re.compile(rf"`\s*([A-Za-z_]\w*)\s*({_STRING}|<[^>\n]*>)?")
_TOKEN_TRICKS = ("``", '`"', "`\\", "$`")


def _risky_features(text: str) -> Counter:
    """Every construct that could reach a file or process, with its first
    string argument (so the same call with a new path counts as new)."""
    text = text.replace("\\\r\n", " ").replace("\\\n", " ")
    found: Counter = Counter()
    defined = set(DEFINE_RE.findall(text))
    for name in defined & RESERVED_DIRECTIVES:
        found[f"`define {name}"] += 1
    for name, arg in _TASK_CALL_RE.findall(text):
        if name not in ALLOWED_SYSTEM_TASKS and name not in _PURE_SV_FUNCTIONS:
            found[f"${name}({arg})" if arg else f"${name}"] += 1
    for name, arg in _DIRECTIVE_CALL_RE.findall(text):
        if name not in ALLOWED_DIRECTIVES and (name not in defined or name in RESERVED_DIRECTIVES):
            found[f"`{name} {arg}".strip()] += 1
    for trick in _TOKEN_TRICKS:
        if n := text.count(trick):
            found[trick] = n
    return found


def new_risky_features(old: str, new: str) -> list[str]:
    """Constructs with file/process access that `new` has more of than
    `old`: what a proposed edit would ADD. Existing uses (the user's own
    $readmemh, `include ...) are not counted against the edit."""
    return sorted((_risky_features(new) - _risky_features(old)).keys())
