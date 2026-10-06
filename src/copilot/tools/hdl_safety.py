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
_DIRECTIVE_CALL_RE = re.compile(rf"`\s*([A-Za-z_]\w*)\s*({_STRING}|<[^>\n]*>)?")
_TASK_RE = re.compile(r"\$([A-Za-z_][\w$]*)\s*(\()?")
_TOKEN_TRICKS = ("``", '`"', "`\\", "$`")
_COMMENT_OR_STRING_RE = re.compile(rf'{_STRING}|//[^\n]*|/\*.*?\*/', re.DOTALL)
_STRING_FULL_RE = re.compile(rf"\s*{_STRING}\s*")
# System tasks that take a file path; the first group can also create/overwrite one.
_WRITE_PATH_TASKS = frozenset({"fopen", "writememh", "writememb", "dumpfile", "dumpports", "fdumpports"})
_PATH_TASKS = _WRITE_PATH_TASKS | {"readmemh", "readmemb", "sdf_annotate"}


def strip_comments(text: str) -> str:
    """Comments blanked (newlines kept), string literals kept, in one
    left-to-right pass so a "//" inside a string is not a comment."""
    def blank(m: re.Match) -> str:
        return m.group(0) if m.group(0).startswith('"') else re.sub(r"[^\n]", " ", m.group(0))
    return _COMMENT_OR_STRING_RE.sub(blank, text.replace("\\\r\n", " ").replace("\\\n", " "))


def _call_args(code: str, start: int) -> str:
    """The text between the "(" just before `start` and its matching ")"
    (string literals skipped), or the rest of the code if unbalanced."""
    depth, j = 1, start
    while j < len(code) and depth:
        c = code[j]
        if c == '"':
            m = re.compile(_STRING).match(code, j)
            j = m.end() if m else j + 1
            continue
        depth += {"(": 1, ")": -1}.get(c, 0)
        j += 1
    return code[start: j - 1 if depth == 0 else j]


def _split_top(args: str) -> list[str]:
    """Top-level comma split (nested (), {}, [] and strings kept whole)."""
    parts, depth, cur, i = [], 0, "", 0
    while i < len(args):
        c = args[i]
        if c == '"':
            m = re.compile(_STRING).match(args, i)
            end = m.end() if m else i + 1
            cur += args[i:end]
            i = end
            continue
        if c in "({[":
            depth += 1
        elif c in ")}]":
            depth -= 1
        if c == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += c
        i += 1
    return [*parts, cur]


def access_features(text: str) -> Counter:
    """Every construct in the code (comments ignored) that could reach a
    file or process, keyed with its WHOLE argument list (whitespace
    normalised), so changing a path, a mode ("r" -> "w") or a concatenation
    makes it a new feature. Plain macro uses are not counted here: check the
    preprocessed text (iverilog -E) to see what they expand to."""
    code = strip_comments(text)
    found: Counter = Counter()
    for name in set(DEFINE_RE.findall(code)) & RESERVED_DIRECTIVES:
        found[f"`define {name}"] += 1
    for m in _TASK_RE.finditer(code):
        name = m.group(1)
        if name in ALLOWED_SYSTEM_TASKS or name in _PURE_SV_FUNCTIONS:
            continue
        if m.group(2):
            found[f"${name}({' '.join(_call_args(code, m.end()).split())})"] += 1
        else:
            found[f"${name}"] += 1
    for name, arg in _DIRECTIVE_CALL_RE.findall(code):
        if name in RESERVED_DIRECTIVES - ALLOWED_DIRECTIVES:
            found[f"`{name} {arg}".strip()] += 1
    for trick in _TOKEN_TRICKS:
        if n := code.count(trick):
            found[trick] = n
    return found


def new_risky_features(old: str, new: str) -> list[str]:
    """Constructs with file/process access that `new` has more of than
    `old`: what a proposed edit would ADD (uncommenting one, or changing its
    path or mode, counts). The user's own unchanged uses are not counted."""
    return sorted((access_features(new) - access_features(old)).keys())


def _path_is_literal(key: str) -> bool:
    if "(" not in key:
        return False  # bare task name, e.g. inside a macro body
    args = key[key.index("(") + 1: -1]
    return bool(_STRING_FULL_RE.fullmatch(_split_top(args)[0]))


def _task(key: str) -> str:
    return key[1:].split("(")[0] if key.startswith("$") else ""


def nonliteral_writes(features: Counter) -> list[str]:
    """Calls that can create/overwrite a file whose path is not one literal."""
    return sorted(k for k in features if _task(k) in _WRITE_PATH_TASKS and not _path_is_literal(k))


def nonliteral_paths(features: Counter) -> list[str]:
    """Any file-path task whose path is not one literal ($readmemh(MEM_FILE))."""
    return sorted(k for k in features if _task(k) in _PATH_TASKS and not _path_is_literal(k))
