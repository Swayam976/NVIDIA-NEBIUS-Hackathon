"""Skills: testbench_runner, lint_checker, hazard_sanity_checker,
waveform_summarizer.

These wrap real CLI tools (iverilog/vvp, verilator) where available. If a
tool isn't installed, the skill returns a clear status instead of crashing,
so the agent can tell the user what to install rather than failing silently.
"""

from __future__ import annotations

import filecmp
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Callable

from ..llm import get_client, json_completion
from .rtl_files import design_files, find_module_files, is_skipped_dir, project_files
from .vcd import format_value, read_vcd


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


_FAIL_LINE_RE = re.compile(r"\bFAIL(ED)?\b|\bFATAL\b|\bERROR\b|\bMISMATCH\b", re.IGNORECASE)
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


# Memory-init / program files a testbench may $readmemh / $readmemb by bare name.
_DATA_EXTS = (".mem", ".hex", ".dat")
_DATA_MAX_FILES = 300
_DATA_MAX_BYTES = 50 * 1024 * 1024
_DATA_SCAN_LIMIT = 20000  # files visited per search root
_VCD_MAX_BYTES = 100 * 1024 * 1024  # waveform dumps kept for debugging


def _safe_data_root(d: Path) -> bool:
    """Never scan a drive root, the home folder, or anything above it."""
    d, home = d.resolve(), Path.home().resolve()
    return d.parent != d and d != home and d not in home.parents


def _stage_data_files(roots: list[Path], dest: Path) -> tuple[dict[str, Path], dict[str, list[Path]]]:
    """Copies program/memory files from roots into dest, flattened — as
    Vivado does for its simulation directory — so `$readmemh("program.hex")`
    finds them. Roots are in priority order (the testbench's own folder
    first); within that order the first file of a given name wins. Returns
    (staged name -> source, conflicts): conflicts lists other files with the
    same name but DIFFERENT contents, which were not used."""
    staged: dict[str, Path] = {}
    conflicts: dict[str, list[Path]] = {}
    total = 0
    for root in roots:
        if not root.is_dir() or not _safe_data_root(root):
            continue
        visited = 0
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if not is_skipped_dir(d))
            for name in sorted(filenames):
                visited += 1
                if not name.lower().endswith(_DATA_EXTS):
                    continue
                src = Path(dirpath) / name
                if name in staged:
                    used = staged[name]
                    if src.resolve() != used.resolve() and not filecmp.cmp(src, used, shallow=False):
                        if src not in conflicts.setdefault(name, []) and len(conflicts[name]) < 5:
                            conflicts[name].append(src)
                    continue
                size = src.stat().st_size
                if len(staged) >= _DATA_MAX_FILES or total + size > _DATA_MAX_BYTES:
                    continue
                shutil.copy2(src, dest / name)
                staged[name] = src
                total += size
            if visited > _DATA_SCAN_LIMIT:
                break
    return staged, conflicts


def _vivado_mem_init_dirs(start: Path) -> list[Path]:
    """`<proj>.ip_user_files/mem_init_files` of the Vivado project enclosing
    `start` (the folder holding a .xpr, up to 6 levels up). Vivado keeps its
    copies of the project's memory files there and hands them to xsim, so it
    is the last place to look, matching what a Vivado simulation would see."""
    for d in [start, *list(start.parents)[:6]]:
        if not _safe_data_root(d):
            break
        try:
            if any(p.suffix.lower() == ".xpr" for p in d.iterdir() if p.is_file()):
                return sorted(p / "mem_init_files" for p in d.glob("*.ip_user_files") if (p / "mem_init_files").is_dir())
        except OSError:
            break
    return []


_UNOPENED_RE = re.compile(r"Unable to open (.+?) for reading\.?\r?$", re.MULTILINE)  # names may contain spaces


_LOG_MAX_BYTES = 8 * 1024 * 1024  # simulator output kept; a run printing more is stopped


def _run_capped(cmd: list[str], timeout_s: int, cwd: str) -> tuple[str, int | None, bool, bool]:
    """Runs cmd with stdout+stderr merged, keeping at most _LOG_MAX_BYTES:
    a testbench that prints forever is killed instead of filling memory.
    Returns (output, exit code or None, timed out, output limit hit)."""
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    chunks: list[bytes] = []
    state = {"total": 0, "over": False}

    def pump() -> None:
        for chunk in iter(lambda: proc.stdout.read(65536), b""):
            room = _LOG_MAX_BYTES - state["total"]
            if room > 0:
                chunks.append(chunk[:room])
            state["total"] += len(chunk)
            if state["total"] > _LOG_MAX_BYTES:
                state["over"] = True
                proc.kill()
                break

    reader = threading.Thread(target=pump, daemon=True)
    reader.start()
    timed_out = False
    try:
        code = proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        timed_out, code = True, None
    reader.join(timeout=5)
    proc.stdout.close()
    text = b"".join(chunks).decode(errors="replace").replace("\r\n", "\n")
    return text, (None if state["over"] else code), timed_out, state["over"]


def _head(text: str, limit: int = 2000) -> str:
    """The first lines of compiler output, up to `limit` characters, cut at a
    line end: the first errors are the cause, later ones usually cascade."""
    out, used = [], 0
    for line in text.strip().splitlines():
        if used + len(line) + 1 > limit:
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)


def _display(path: Path, bases: list[Path]) -> str:
    for base in bases:
        try:
            return path.relative_to(base).as_posix()
        except ValueError:
            continue
    return path.name


def testbench_runner(
    module_path: str = "",
    tb_path: str = "",
    rtl_dir: str = "",
    timeout_s: int = 60,
    compile_check: Callable[[list[Path]], str | None] | None = None,
    vcd_out: Path | None = None,
    data_dirs: list[Path] | None = None,
) -> dict:
    """Compiles and runs a Verilog testbench with Icarus Verilog and returns
    pass/fail plus a short summary instead of the raw simulator log.

    The design comes from module_path (one file) and/or rtl_dir (a folder):
    starting from the testbench, only the modules it actually instantiates
    are compiled, so unrelated designs in the folder don't get in the way.
    Program/memory files (.mem/.hex/.dat) near the testbench are staged into
    the run directory, so `$readmemh("program.hex")` works as in Vivado.

    compile_check (internal, not in the tool schema) is called with the
    exact compiler inputs, in order, before every compile attempt; a non-None
    reason aborts with status "blocked". The web demo uses it for its safety
    check, since `ifdef outcomes depend on file order.

    vcd_out (internal, not in the tool schema): also dump a waveform of the
    whole testbench hierarchy, by compiling in one generated module (the
    user's files are untouched), and copy it to vcd_out. The result then
    carries "vcd_path" and "_compiled_paths" (absolute source paths).

    data_dirs (internal, not in the tool schema): extra program/memory file
    folders, searched last (verify_loop passes its snapshot of the real
    project's Vivado mem_init_files, which its temp copy can't find).
    """
    if not _tool_available("iverilog") or not _tool_available("vvp"):
        return {
            "status": "unavailable",
            "message": "iverilog/vvp not found on PATH. Install Icarus Verilog to enable this skill.",
        }
    tb = Path(tb_path).resolve() if tb_path else None
    if tb is None or not tb.is_file():
        return {"status": "error", "message": f"No testbench file at '{tb_path}'."}
    if not module_path and not rtl_dir:
        return {"status": "error", "message": "Give module_path (a single design file) or rtl_dir (a folder of RTL sources)."}

    explicit: list[Path] = []
    search: list[Path] = []
    bases = [tb.parent]
    if module_path:
        module = Path(module_path).resolve()
        if not module.is_file():
            return {"status": "error", "message": f"No design file at '{module_path}'."}
        explicit.append(module)
        search.append(module)
    if rtl_dir:
        rtl_root = Path(rtl_dir).resolve()
        if not rtl_root.is_dir():
            return {"status": "error", "message": f"No RTL folder at '{rtl_dir}'."}
        bases.insert(0, rtl_root)
        search += project_files(rtl_root, (".v", ".sv"))
    # Helper modules sitting next to the testbench.
    search += sorted(p for p in tb.parent.iterdir() if p.is_file() and p.suffix.lower() in (".v", ".sv"))
    unique, seen = [], {tb}
    for p in search:
        p = p.resolve()
        if p not in seen:
            seen.add(p)
            unique.append(p)

    tops, files, duplicates = design_files(tb, unique)
    if not tops:
        return {"status": "error", "message": f"No module defined in testbench '{tb_path}'."}
    if duplicates:
        return {
            "status": "ambiguous_design",
            "message": "These modules are defined in more than one file; pass module_path or a narrower rtl_dir.",
            "duplicates": {m: [_display(f, bases) for f in fs] for m, fs in duplicates.items()},
        }
    files += [m for m in explicit if m not in files]

    # Per-call temp dir: portable (no /tmp on Windows) and safe if two runs overlap.
    with tempfile.TemporaryDirectory(prefix="copilot_tb_") as tmp:
        run_dir = Path(tmp)
        data_roots = [tb.parent, tb.parent.parent, *bases[:-1], *(m.parent for m in explicit),
                      *_vivado_mem_init_dirs(tb.parent), *(data_dirs or [])]
        staged, data_conflicts = _stage_data_files(data_roots, run_dir)
        out_bin = run_dir / "tb.out"
        dump_args: list[str] = []
        if vcd_out is not None:
            # Generated, fixed content (tops[0] is a parsed identifier), so it is
            # not part of compile_check's input; it only adds a VCD dump.
            dump_src = run_dir / "copilot_vcd_dump.v"
            dump_src.write_text(
                "module copilot_vcd_dump;\n"
                f'  initial begin $dumpfile("copilot_dump.vcd"); $dumpvars(0, {tops[0]}); end\n'
                "endmodule\n", encoding="utf-8")
            dump_args = [str(dump_src)]  # its "-s" root goes with the options below
        for _ in range(6):  # compile; add files for modules iverilog reports as unknown
            include_dirs = list(dict.fromkeys([tb.parent, *(f.parent for f in files)]))  # incl. files added by retries
            if compile_check is not None and (reason := compile_check(list(files))):
                return {"status": "blocked", "message": reason}
            # SystemVerilog mode only when SV sources are compiled: it reserves
            # words (logic, bit, int...) that plain Verilog designs may use as names.
            sv = ["-g2012"] if any(f.suffix.lower() in (".sv", ".svh") for f in files) else []
            roots = [*tops, *(["copilot_vcd_dump"] if dump_args else [])]
            cmd = ["iverilog", *sv, *[a for t in roots for a in ("-s", t)], *[f"-I{d}" for d in include_dirs],
                   "-o", str(out_bin), *map(str, files), *dump_args]
            try:
                compiled = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, cwd=tmp)
            except subprocess.TimeoutExpired:
                return {"status": "timeout", "message": f"Compilation took longer than {timeout_s}s."}
            if compiled.returncode == 0:
                break
            unknown = set(re.findall(r"Unknown module type:\s*([A-Za-z_]\w*)", compiled.stderr))
            extra = []
            for name in sorted(unknown):
                defining = find_module_files(name, unique)
                if len(defining) > 1:  # same rule as the dependency walk: never guess
                    return {
                        "status": "ambiguous_design",
                        "message": "These modules are defined in more than one file; pass module_path or a narrower rtl_dir.",
                        "duplicates": {name: [_display(f, bases) for f in defining]},
                    }
                if defining and defining[0] not in files:
                    extra.append(defining[0])
            if not extra:
                return {"status": "compile_error", "stderr": _head(compiled.stderr),
                        "compiled_files": [_display(f, bases) for f in files][:60]}
            files += extra
        else:
            return {"status": "compile_error", "stderr": _head(compiled.stderr),
                    "compiled_files": [_display(f, bases) for f in files][:60]}

        log, exit_code, timed_out, too_much = _run_capped(["vvp", str(out_bin)], timeout_s, tmp)

        vcd_copied = False
        if vcd_out is not None:
            # Our dump, or failing that the testbench's own (largest .vcd), if not huge.
            dumps = [run_dir / "copilot_dump.vcd"] + sorted(run_dir.glob("*.vcd"), key=lambda p: -p.stat().st_size)
            for dump in dumps:
                if dump.is_file() and 0 < dump.stat().st_size <= _VCD_MAX_BYTES:
                    shutil.copy2(dump, vcd_out)
                    vcd_copied = True
                    break

    summary = _summarize_log(log)
    # Same line classification as the counts, so status and counts never
    # disagree; a non-zero simulator exit ($fatal, crash) is never a pass.
    missing = sorted(set(_UNOPENED_RE.findall(log)))
    # A run that couldn't open its program/data file is never a pass, whatever it printed.
    passed = (summary["pass_lines"] > 0 and summary["fail_lines"] == 0 and not timed_out and not too_much
              and exit_code == 0 and not missing)
    if missing and not passed:
        # Without its program/data the design runs on X's: the failures say
        # nothing about the RTL, so say that first.
        summary["headline"] = (
            f"The simulation could not open {', '.join(missing)} (not found next to the testbench or in the "
            "project), so these results are not meaningful until that file exists. " + summary["headline"]
        )
    # A long raw tail drowns out the counts, so it's only sizeable when there
    # was nothing to count.
    tail_chars = 1500 if not (summary["pass_lines"] or summary["fail_lines"]) else 300
    if timed_out:
        status = "timeout"
    elif too_much:
        status = "output_limit"
    elif passed:
        status = "pass"
    else:
        status = "missing_data_file" if missing else "fail_or_unknown"
    result = {
        "status": status,
        "missing_data_files": missing,
        **summary,
        "summary": log.strip()[-tail_chars:],  # log tail
        "top": tops,
        "exit_code": exit_code,
        "compiled_files": [_display(f, bases) for f in files][:60],
        "data_files": sorted(staged)[:40],
        "note": "Pass/fail is inferred from PASS/FAIL text in the log — make sure your testbench prints one.",
    }
    if vcd_out is not None:
        result["vcd_path"] = str(vcd_out) if vcd_copied else None
        result["_compiled_paths"] = [str(f) for f in files]
    if data_conflicts:
        # Several different files share a name; the one closest to the testbench was used.
        data_bases = [tb.parent.parent, *bases]  # so two "prog.mem" paths stay distinguishable
        result["data_file_conflicts"] = {
            name: {"used": _display(staged[name], data_bases), "not_used": [_display(p, data_bases) for p in others]}
            for name, others in sorted(data_conflicts.items())
        }
        result["note"] += (" Some program/data file names exist more than once with different contents; "
                           "see data_file_conflicts for which copy was used.")
    if timed_out:
        result["message"] = f"Simulation did not finish within {timeout_s}s (missing $finish?); partial log shown."
    elif too_much:
        result["message"] = (f"The simulation printed more than {_LOG_MAX_BYTES // (1024 * 1024)} MB and was stopped; "
                             "the counts cover only the part kept.")
    elif exit_code and not passed:
        result["message"] = f"The simulator exited with code {exit_code} ($fatal or a runtime error)."
    return result


def lint_checker(file_path: str, timeout_s: int = 30, search_dirs: list[str] | None = None,
                 top: str = "", extra_files: list[str] | None = None) -> dict:
    """Runs Verilator in lint-only mode and translates warnings into a
    plain list instead of a raw log dump.

    search_dirs / top / extra_files (internal, not in the tool schema):
    folders Verilator searches for instantiated modules (-y), the top module
    (--top-module) and source files read alongside (modules the file uses),
    for linting a new module that connects to existing ones.
    """
    resolved = _resolve_verilator()
    if resolved is None:
        return {"status": "unavailable", "message": "verilator not found on PATH."}
    exe, env = resolved

    res = subprocess.run(
        [exe, "--lint-only", "-Wall", *[a for d in (search_dirs or []) for a in ("-y", d)],
         *(["--top-module", top] if top else []), *(extra_files or []), file_path],
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
        # Every error, not just those in the first 50 lines (verify_loop compares them).
        "errors": [w for w in warnings if w.startswith("%Error")][:5000],
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
        content = json_completion(get_client(), [
            {"role": "system", "content": _HAZARD_REVIEW_PROMPT},
            {"role": "user", "content": diff_text[:12000]},
        ], max_tokens=1500)
        review = json.loads(content or "{}")
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
    range, in plain terms instead of raw VCD syntax: full-width binary plus
    hex/decimal, the value at the window start, and the dump's time unit.
    """
    path = Path(vcd_path)
    if not path.exists():
        return {"status": "error", "message": f"No VCD file at {vcd_path}"}

    try:
        header, _ = read_vcd(path)
    except (OSError, ValueError) as exc:
        return {"status": "error", "message": f"Could not read VCD: {exc}"}
    # Exact leaf or full-name match first, then substring matches.
    exact = [v for v in header.vars if signal in (v.leaf, v.name)]
    partial = [v for v in header.vars if signal in v.name and v not in exact]
    candidates = exact or partial
    if not candidates:
        return {"status": "not_found", "message": f"No signal matching '{signal}' in {vcd_path}",
                "available": [v.name for v in header.vars][:40]}

    var = candidates[0]
    _, changes = read_vcd(path, want={var.code})
    tv = changes[var.code]
    before = [(t, v) for t, v in tv if t <= t_start]  # value in effect at t_start
    window = [(t, v) for t, v in tv if t >= t_start and (t_end is None or t <= t_end)]

    return {
        "status": "ok",
        "signal": var.name,
        "width_bits": var.size,
        "time_unit": header.timescale or "unknown",
        "other_matches": [v.name for v in candidates[1:10]],
        "value_at_start": format_value(before[-1][1], var.size) if before else None,
        "transitions_in_window": len(window),
        "distinct_values": len({v for _, v in window}),
        "changes": [{"time": t, **format_value(v, var.size)} for t, v in window[:100]],
        "note": "Truncated to first 100 transitions." if len(window) > 100 else None,
    }


SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "testbench_runner",
            "description": "Compile and run a Verilog testbench with Icarus Verilog; return pass/fail and a summary. "
            "Give the testbench plus module_path (a single-file design) or rtl_dir (a folder of RTL sources: only "
            "the modules the testbench instantiates are compiled). Program/memory files (.mem/.hex/.dat) near the "
            "testbench are made available to $readmemh.",
            "parameters": {
                "type": "object",
                "properties": {
                    "tb_path": {"type": "string", "description": "The testbench file."},
                    "module_path": {"type": "string", "description": "Design file, for a single-file design."},
                    "rtl_dir": {"type": "string", "description": "Folder with the design's RTL sources, for multi-file designs."},
                },
                "required": ["tb_path"],
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
