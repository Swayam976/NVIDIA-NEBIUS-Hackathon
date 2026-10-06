"""Skills: testbench_runner, lint_checker, hazard_sanity_checker,
waveform_summarizer.

These wrap real CLI tools (iverilog/vvp, verilator) where available. If a
tool isn't installed, the skill returns a clear status instead of crashing,
so the agent can tell the user what to install rather than failing silently.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from ..config import settings
from ..llm import get_client


_IS_WINDOWS = os.name == "nt"
_LINT_SUMMARY_RE = re.compile(r"Exiting due to \d+ (warning|error)\(s\)")


def _tool_available(name: str) -> bool:
    return shutil.which(name) is not None


def _resolve_verilator() -> tuple[str, dict | None] | None:
    """Returns (executable, env) for running Verilator, or None.

    On Linux/macOS `verilator` is the normal entry point. On native Windows
    (MSYS2 ucrt64 build) `verilator` is an extensionless Perl script that
    can't be exec'd, so fall back to `verilator_bin.exe`. That binary has an
    MSYS-style VERILATOR_ROOT compiled in, which native Windows can't
    resolve, so point it at <prefix>/share/verilator unless already set.
    """
    exe = shutil.which("verilator")
    # Depending on PATHEXT, which() on Windows can return the Perl script;
    # exec'ing that fails with WinError 193, so only accept a real .exe there.
    if exe is not None and (not _IS_WINDOWS or exe.lower().endswith(".exe")):
        return exe, None

    exe = shutil.which("verilator_bin")
    if exe is None:
        return None
    env = None
    if "VERILATOR_ROOT" not in os.environ:
        root = Path(exe).resolve().parent.parent / "share" / "verilator"
        if root.is_dir():
            env = {**os.environ, "VERILATOR_ROOT": str(root)}
    return exe, env


_FAIL_LINE_RE = re.compile(r"\bFAIL(ED)?\b|\bERROR\b|\bMISMATCH\b", re.IGNORECASE)
_PASS_LINE_RE = re.compile(r"\bPASS(ED)?\b", re.IGNORECASE)
_FIELD_RE = re.compile(r"([A-Za-z_]\w*)\s*=\s*([^,\s]+)")


def _summarize_log(log: str, max_examples: int = 8) -> dict:
    """Counts and groups pass/fail lines so the model sees the pattern, not
    a cut-off tail. A `name = value` field that takes only a few distinct
    values across the failures (an opcode, an expected constant) is reported
    with its histogram, e.g. ctrl {5: 5, 6: 5, 7: 5}; operand-like fields
    with many distinct values are left out as noise."""
    lines = [line.strip() for line in log.splitlines() if line.strip()]
    fails = [line for line in lines if _FAIL_LINE_RE.search(line)]
    passes = [line for line in lines if _PASS_LINE_RE.search(line) and not _FAIL_LINE_RE.search(line)]

    # Group only across failure lines that carry fields (a "FAIL: 3 checks"
    # summary line has none), and only when there are several to compare.
    field_lines = [
        {key: value.rstrip(":;.)") for key, value in _FIELD_RE.findall(line)} for line in fails
    ]
    field_lines = [f for f in field_lines if f]
    counts: dict[str, dict[str, int]] = {}
    for fields in field_lines:
        for key, value in fields.items():
            counts.setdefault(key, {})
            counts[key][value] = counts[key].get(value, 0) + 1
    grouped = {
        key: dict(sorted(vals.items(), key=lambda kv: -kv[1]))
        for key, vals in counts.items()
        if len(field_lines) > 1 and len(vals) <= 8 and sum(vals.values()) * 2 >= len(field_lines)
    }
    if fails:
        pattern = "; ".join(
            f"failures by {key}: {', '.join(f'{v} ({n}x)' for v, n in vals.items())}"
            for key, vals in grouped.items()
        )
        headline = (
            f"{len(fails)} failing line(s), {len(passes)} passing line(s)."
            + (f" Pattern: {pattern}." if pattern else "")
            + " The bug can be in the RTL or in the testbench's expected values; check both for these cases."
        )
    elif passes:
        headline = f"All {len(passes)} checked line(s) passed."
    else:
        headline = "No PASS/FAIL lines found; see the log tail."
    return {
        "headline": headline,
        "pass_lines": len(passes),
        "fail_lines": len(fails),
        "failure_fields": grouped,
        "first_failures": fails[:max_examples],
    }


def testbench_runner(module_path: str, tb_path: str, timeout_s: int = 60) -> dict:
    """Compiles and runs a Verilog testbench with Icarus Verilog, returns
    pass/fail plus a short summary instead of the raw simulator log.
    """
    if not _tool_available("iverilog") or not _tool_available("vvp"):
        return {
            "status": "unavailable",
            "message": "iverilog/vvp not found on PATH. Install Icarus Verilog to enable this skill.",
        }

    # Per-call temp dir: portable (no /tmp on Windows) and safe if two runs overlap.
    with tempfile.TemporaryDirectory(prefix="copilot_tb_") as tmp:
        out_bin = str(Path(tmp) / "tb.out")
        compile_cmd = ["iverilog", "-o", out_bin, tb_path, module_path]
        compile_res = subprocess.run(compile_cmd, capture_output=True, text=True, timeout=timeout_s)
        if compile_res.returncode != 0:
            return {"status": "compile_error", "stderr": compile_res.stderr.strip()[-2000:]}

        run_res = subprocess.run(["vvp", out_bin], capture_output=True, text=True, timeout=timeout_s)
    log = run_res.stdout + run_res.stderr
    summary = _summarize_log(log)
    # Same line classification as the counts, so status and counts never disagree.
    passed = summary["pass_lines"] > 0 and summary["fail_lines"] == 0
    # A long raw tail drowns out the counts, so it's only sizeable when there
    # was nothing to count.
    tail_chars = 1500 if not (summary["pass_lines"] or summary["fail_lines"]) else 300
    return {
        "status": "pass" if passed else "fail_or_unknown",
        **summary,
        "summary": log.strip()[-tail_chars:],  # log tail
        "note": "Pass/fail is inferred from PASS/FAIL text in the log — make sure your testbench prints one.",
    }


def lint_checker(file_path: str, timeout_s: int = 30) -> dict:
    """Runs Verilator in lint-only mode and translates warnings into a
    plain list instead of a raw log dump.
    """
    resolved = _resolve_verilator()
    if resolved is None:
        return {"status": "unavailable", "message": "verilator not found on PATH."}
    exe, env = resolved

    res = subprocess.run(
        [exe, "--lint-only", "-Wall", file_path],
        capture_output=True,
        text=True,
        timeout=timeout_s,
        env=env,
    )
    # Drop only the trailing "%Error: Exiting due to N warning(s)" summary; it
    # restates the count. Other "Exiting due to ..." lines (e.g. internal fault) stay.
    warnings = [
        line.strip()
        for line in res.stderr.splitlines()
        if ("%Warning" in line or "%Error" in line) and not _LINT_SUMMARY_RE.search(line)
    ]
    if res.returncode != 0 and not warnings:
        return {"status": "error", "returncode": res.returncode, "stderr": res.stderr.strip()[-2000:]}
    return {
        "status": "clean" if not warnings else "issues_found",
        "warning_count": len(warnings),
        "warnings": warnings[:50],
    }


# Lightweight heuristics for common pipeline hazard red flags. This is a
# first-pass filter, not a substitute for actually simulating — it exists
# to catch obvious stuff before you spend a sim cycle on it.
_HAZARD_PATTERNS = [
    (re.compile(r"\bregfile\[.*\]\s*<=.*\bregfile\[.*\]\b"), "possible same-cycle register read/write without forwarding"),
    (re.compile(r"if\s*\(.*==.*\)\s*//\s*TODO.*hazard", re.IGNORECASE), "hazard check left as a TODO"),
    (re.compile(r"\bPC\s*<=.*\bPC\b.*\+.*\n(?!.*stall)", re.IGNORECASE), "PC update with no visible stall/flush handling nearby"),
]


# Lines touching forwarding / stall / flush / pipeline registers.
_HAZARD_LOGIC_RE = re.compile(
    r"\b(forward\w*|fwd\w*|bypass\w*|stall\w*|flush\w*|hazard\w*|bubble\w*|ex_mem\w*|mem_wb\w*|id_ex\w*|if_id\w*)\b",
    re.IGNORECASE,
)

_HAZARD_REVIEW_PROMPT = """\
You review diffs to pipelined CPU RTL for data and control hazards: forwarding \
priority (the newest producer, EX/MEM, must win over MEM/WB), x0 never \
forwarded, load-use stalls, branch/jump flushes, PC updates while stalled. \
Report only problems visible in the diff. Respond with ONLY a JSON object: \
{"verdict": "likely_hazard" | "no_hazard_found" | "unclear", \
"findings": [{"line": "<diff line>", "issue": "<one sentence>", "severity": "high" | "medium" | "low"}]}
"""


def _norm(line: str) -> str:
    line = re.sub(r"^\s*else\b", "", line.strip())
    return " ".join(line.split())


def _removed_hazard_logic(diff_text: str) -> list[str]:
    """Removed lines touching hazard logic that don't come back among the
    added lines of the same file (so pure re-indents or else-if reshuffles
    aren't flagged, and logic re-added in another file doesn't hide it)."""
    files: dict[str, tuple[list[str], set[str]]] = {}
    current = ""
    for l in diff_text.splitlines():
        if l.startswith("+++ "):
            current = l[4:].strip()
            continue
        if l.startswith("--- ") or l.startswith("diff --git"):
            continue
        removed, added = files.setdefault(current, ([], set()))
        if l.startswith("-"):
            removed.append(l[1:])
        elif l.startswith("+"):
            added.add(_norm(l[1:]))
    flags = []
    for removed, added in files.values():
        for line in removed:
            code = line.split("//", 1)[0]
            if _HAZARD_LOGIC_RE.search(code) and _norm(code) and _norm(code) not in added:
                flags.append(f"removed hazard-related logic: `{line.strip()}`")
    return flags[:6]


def _llm_hazard_review(diff_text: str) -> dict:
    try:
        response = get_client().chat.completions.create(
            model=settings.nebius_model,
            messages=[
                {"role": "system", "content": _HAZARD_REVIEW_PROMPT},
                {"role": "user", "content": diff_text[:12000]},
            ],
            response_format={"type": "json_object"},
        )
        review = json.loads(response.choices[0].message.content or "{}")
        if not isinstance(review, dict):
            raise ValueError("review is not a JSON object")
    except Exception as exc:  # noqa: BLE001 - the rule-based result still stands
        return {"status": "unavailable", "message": f"{type(exc).__name__}"}
    findings = review.get("findings") if isinstance(review.get("findings"), list) else []
    verdict = review.get("verdict") if review.get("verdict") in ("likely_hazard", "no_hazard_found", "unclear") else "unclear"
    return {"status": "ok", "verdict": verdict, "findings": findings[:8]}


def hazard_sanity_checker(diff_text: str, llm_review: bool = True) -> dict:
    """Checks a diff for unhandled data/control hazards in a pipelined core:
    pattern rules on added lines, a rule for removed forwarding/stall/flush
    logic, and (unless llm_review=False) one hazard-focused Nemotron review.
    Not proof either way — "no flags" means "simulate it", not "safe".
    """
    added_text = "\n".join(
        l[1:] for l in diff_text.splitlines() if l.startswith("+") and not l.startswith("+++")
    )
    flags = [desc for pattern, desc in _HAZARD_PATTERNS if pattern.search(added_text)]
    flags += _removed_hazard_logic(diff_text)

    review = _llm_hazard_review(diff_text) if llm_review else {"status": "skipped"}
    model_flagged = review.get("verdict") == "likely_hazard" or any(
        isinstance(f, dict) and f.get("severity") in ("high", "medium") for f in review.get("findings", [])
    )
    if flags or model_flagged:
        status = "flagged"
    elif review.get("verdict") == "unclear":
        status = "unclear_needs_simulation"
    else:
        status = "no_flags_needs_simulation"
    return {
        "status": status,
        "flags": flags,
        "model_review": review,
        "note": "Rules plus a model review, not proof. No flags does NOT mean hazard-free: "
        "confirm with testbench_runner.",
    }


def waveform_summarizer(vcd_path: str, signal: str, t_start: int = 0, t_end: int | None = None) -> dict:
    """Summarizes value changes for one signal in a VCD dump over a time
    range, in plain terms instead of raw VCD syntax.
    """
    path = Path(vcd_path)
    if not path.exists():
        return {"status": "error", "message": f"No VCD file at {vcd_path}"}

    try:
        import vcdvcd  # type: ignore
    except ImportError:
        return {
            "status": "unavailable",
            "message": "The 'vcdvcd' package isn't installed. Run: pip install vcdvcd",
        }

    vcd = vcdvcd.VCDVCD(str(path))
    matches = [name for name in vcd.references_to_ids if signal in name]
    if not matches:
        return {"status": "not_found", "message": f"No signal matching '{signal}' in {vcd_path}"}

    sig_name = matches[0]
    sig_id = vcd.references_to_ids[sig_name]
    tv = vcd.data[sig_id].tv  # list of (time, value)
    window = [(t, v) for t, v in tv if t >= t_start and (t_end is None or t <= t_end)]

    return {
        "status": "ok",
        "signal": sig_name,
        "transitions_in_window": len(window),
        "changes": [{"time": t, "value": v} for t, v in window[:100]],
        "note": "Truncated to first 100 transitions." if len(window) > 100 else None,
    }


SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "testbench_runner",
            "description": "Compile and run a Verilog testbench, return pass/fail and a summary.",
            "parameters": {
                "type": "object",
                "properties": {
                    "module_path": {"type": "string"},
                    "tb_path": {"type": "string"},
                },
                "required": ["module_path", "tb_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "lint_checker",
            "description": "Run Verilator lint on a Verilog file and return a translated warning list.",
            "parameters": {
                "type": "object",
                "properties": {"file_path": {"type": "string"}},
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hazard_sanity_checker",
            "description": "Heuristically scan a diff for common pipeline hazard red flags.",
            "parameters": {
                "type": "object",
                "properties": {"diff_text": {"type": "string"}},
                "required": ["diff_text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "waveform_summarizer",
            "description": "Summarize a signal's value changes in a VCD waveform dump over a time range.",
            "parameters": {
                "type": "object",
                "properties": {
                    "vcd_path": {"type": "string"},
                    "signal": {"type": "string"},
                    "t_start": {"type": "integer"},
                    "t_end": {"type": "integer"},
                },
                "required": ["vcd_path", "signal"],
            },
        },
    },
]

IMPLS = {
    "testbench_runner": testbench_runner,
    "lint_checker": lint_checker,
    "hazard_sanity_checker": hazard_sanity_checker,
    "waveform_summarizer": waveform_summarizer,
}
