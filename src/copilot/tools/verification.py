"""Skills: testbench_runner, lint_checker, hazard_sanity_checker,
waveform_summarizer.

These wrap real CLI tools (iverilog/vvp, verilator) where available. If a
tool isn't installed, the skill returns a clear status instead of crashing,
so the agent can tell the user what to install rather than failing silently.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path


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
    passed = bool(re.search(r"\bPASS(ED)?\b", log, re.IGNORECASE)) and not re.search(
        r"\bFAIL(ED)?\b|\bERROR\b", log, re.IGNORECASE
    )
    return {
        "status": "pass" if passed else "fail_or_unknown",
        "summary": log.strip()[-2000:],
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


def hazard_sanity_checker(diff_text: str) -> dict:
    """Scans a diff (added lines) for patterns that commonly indicate
    unhandled data/control hazards in a pipelined core. Heuristic, not
    exhaustive — flags things worth a closer look, not definitive bugs.
    """
    added_lines = [l[1:] for l in diff_text.splitlines() if l.startswith("+") and not l.startswith("+++")]
    added_text = "\n".join(added_lines)

    hits = []
    for pattern, description in _HAZARD_PATTERNS:
        if pattern.search(added_text):
            hits.append(description)

    return {
        "status": "flagged" if hits else "no_obvious_issues",
        "flags": hits,
        "note": "Heuristic scan only — always confirm with the testbench_runner skill.",
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
