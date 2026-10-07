"""Skill: debug_failing_test.

Runs a failing testbench (testbench_runner), finds the first failing check
in the waveform (waveform_summarizer over a VCD the runner dumps), asks
Nemotron once for the most likely root cause with file:line and confidence,
and proposes the fix as a pending diff (modify_module).

Writes nothing. The fix is a pending diff; only apply_diff, behind the
human approval gate, can write it.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Callable

from ..llm import get_client, json_completion
from .module_modifier import modify_module
from .vcd import read_vcd
from .rtl_files import _CODE_NOISE_RE, design_files, is_testbench, project_files
from .verification import _FIELD_RE, testbench_runner, waveform_summarizer

_DEBUG_PROMPT = """\
You debug failing Verilog simulations. You get the failing check from the simulation log, the \
waveform around the first failure, and the source files with line numbers. Find the single most \
likely root cause. It can be in the RTL or in the testbench's own expected-value model; decide \
which by checking both against each other. Respond with ONLY a JSON object: \
{"failing_check": "<the failing check, one line>", "root_cause": "<one or two sentences>", \
"file": "<one of the file paths given, exactly as shown>", "line": <line number in that file>, \
"confidence": "high" | "medium" | "low", "fix_instruction": "<a precise instruction for editing \
that one file to fix the root cause, or an empty string if there is no safe fix>"}
"""
_SOURCE_BUDGET = 24000  # characters of line-numbered source sent to the model
_PER_FILE = 12000
_NUMBER_RE = re.compile(r"^(\d*)'[sS]?([bBhHdDoO])([0-9a-fA-F_xXzZ]+)$")
_NOT_DEBUGGABLE = {
    "compile_error": "The testbench does not compile, so there is no simulation to debug yet.",
    "missing_data_file": "The simulation could not open its program/data file, so its failures say nothing "
                         "about the design. Provide the missing file first.",
    "timeout": "The simulation did not finish (no $finish reached), so there is no failing check to debug.",
    "ambiguous_design": "A module is defined in more than one file; narrow module_path/rtl_dir first.",
    "output_limit": "The simulation printed too much output and was stopped, so there is no reliable failing check.",
}


_FORMAT_FIELD_RE = re.compile(r"([A-Za-z_]\w*)\s*=\s*%0?\d*([dDhHxXbBoO])")
_RADIX = {"d": 10, "h": 16, "x": 16, "b": 2, "o": 8}


def _field_radixes(tb_text: str) -> dict[str, int]:
    """name -> radix from the testbench's own format strings ("a = %h" -> 16).
    A name printed with different radixes in different strings is left out:
    its logged value can't be read unambiguously."""
    seen: dict[str, set[int]] = {}
    for literal in re.findall(r'"(?:\\.|[^"\\\n])*"', tb_text):
        for name, spec in _FORMAT_FIELD_RE.findall(literal):
            seen.setdefault(name, set()).add(_RADIX[spec.lower()])
    return {name: radixes.pop() for name, radixes in seen.items() if len(radixes) == 1}


def _parse_number(text: str, radix: int | None) -> int | None:
    """A logged value -> int. Self-describing forms (8'hff, 3'b100, 0x1f) are
    read as written; a bare value needs the radix from the testbench's format
    string. Unknown radix, x/z bits or garbage -> None (never a guess)."""
    t = text.strip().rstrip(":;,.)")
    m = _NUMBER_RE.match(t)
    if m:
        if re.search(r"[xXzZ]", m.group(3)):
            return None
        try:
            return int(m.group(3).replace("_", ""), _RADIX[m.group(2).lower()])
        except ValueError:
            return None
    if re.fullmatch(r"0[xX][0-9a-fA-F]+", t):
        return int(t, 16)
    if radix is None:
        return None
    try:
        return int(t, radix)
    except ValueError:
        return None


def _as_int(value: str | None) -> int | None:
    if value is None or not value or not set(value) <= {"0", "1"}:
        return None
    return int(value, 2)


def _locate_failure(vcd: Path, line: str, top: str, radixes: dict[str, int]) -> tuple[dict | None, str]:
    """Earliest time at which EVERY name=value field of the failing log line
    matches the waveform, plus the window to the neighbouring changes.
    Returns (location, "") or (None, reason): no location is claimed from an
    incomplete or ambiguous match."""
    fields = _FIELD_RE.findall(line)
    if len(fields) < 2:
        return None, "the first failing line has fewer than two name=value fields to locate it by"
    header, _ = read_vcd(vcd)
    wanted: dict[str, tuple] = {}
    for key, raw in fields:
        value = _parse_number(raw, radixes.get(key))
        if value is None:
            return None, f"the value of '{key}' in the log can't be read unambiguously"
        candidates = [v for v in header.vars if v.leaf == key]
        if not candidates:
            return None, f"'{key}' is not a signal in the waveform"
        # Prefer the testbench's own signal (shallowest, directly under the top);
        # if several signals tie for that place, decline rather than pick one.
        rank = lambda v: (not v.name.startswith(top + "."), v.name.count("."))  # noqa: E731
        best = min(rank(v) for v in candidates)
        tied = [v for v in candidates if rank(v) == best]
        if len({v.code for v in tied}) > 1:
            return None, f"'{key}' matches several signals ({', '.join(v.name for v in tied[:3])})"
        var = tied[0]
        wanted[var.code] = (var, value % (1 << var.size) if var.size > 0 else value)
    _, changes = read_vcd(vcd, want=set(wanted))
    events = sorted({t for code in wanted for t, _ in changes[code]})
    current: dict[str, str] = {}
    pos = dict.fromkeys(wanted, 0)
    for i, t in enumerate(events):
        for code in wanted:
            while pos[code] < len(changes[code]) and changes[code][pos[code]][0] <= t:
                current[code] = changes[code][pos[code]][1]
                pos[code] += 1
        if all(_as_int(current.get(code)) == value for code, (_, value) in wanted.items()):
            return {
                "time": t,
                "time_unit": header.timescale or "unknown",
                "window": [events[i - 1] if i else t, events[i + 1] if i + 1 < len(events) else t],
                "signals": [var.name for var, _ in wanted.values()],
            }, ""
    return None, "no time in the waveform matches every value in the failing line"


def _numbered_sources(paths: list[Path]) -> tuple[str, list[Path], list[Path]]:
    """Line-numbered sources within the budget. Returns (text, files shown,
    files shown only in part)."""
    blocks, used, shown, truncated = [], 0, [], []
    for p in paths:
        try:
            lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        body = "\n".join(f"{i:4d}| {text}" for i, text in enumerate(lines, 1))
        cut = len(body) > _PER_FILE
        if cut:
            body = body[:_PER_FILE] + "\n    | ... (truncated)"
        block = f"=== FILE: {p} ===\n{body}"
        if shown and used + len(block) > _SOURCE_BUDGET:
            continue
        blocks.append(block)
        used += len(block)
        shown.append(p)
        if cut:
            truncated.append(p)
    return "\n\n".join(blocks), shown, truncated


def _resolve_file(name, shown: list[Path]) -> Path | None:
    """Only a file the model was actually shown can be the fix target."""
    text = str(name or "").strip()
    if not text:
        return None
    for p in shown:
        if os.path.normcase(str(p)) == os.path.normcase(text):
            return p
    same_name = [p for p in shown if p.name.lower() == Path(text).name.lower()]
    return same_name[0] if len(same_name) == 1 else None


def _ask_root_cause(messages: list[dict]) -> dict:
    """One reasoning call; if it is cut off or returns bad JSON, one retry
    without reasoning."""
    try:
        review = json.loads(json_completion(get_client(), messages, max_tokens=16000, thinking=True) or "{}")
    except Exception:  # noqa: BLE001 - cut off / malformed: retry once, cheaper
        review = json.loads(json_completion(get_client(), messages, max_tokens=4000) or "{}")
    if not isinstance(review, dict):
        raise ValueError("root-cause reply is not a JSON object")
    return review


def debug_failing_test(
    tb_path: str,
    module_path: str = "",
    rtl_dir: str = "",
    compile_check: Callable[[list[Path]], str | None] | None = None,
) -> dict:
    """Debugs a failing testbench and proposes a fix as a pending diff.
    Returns the failing check, root cause (file:line), confidence and the
    pending diff. Nothing is written; the diff waits for apply_diff."""
    result, proposal = diagnose_failure(tb_path, module_path, rtl_dir, compile_check, propose=modify_module)
    if result.get("status") != "failing":
        return result
    pending, note = None, result.pop("_note")
    if proposal is not None:
        if proposal.get("status") == "pending_review":
            pending = {k: proposal[k] for k in ("diff_id", "diff", "explanation")}
        else:
            note = f"modify_module could not produce a diff: {proposal.get('message', proposal.get('status'))}"
    return {
        **result,
        "pending_diff": pending,
        "note": (note + " " if note else "") + "Nothing was written. Review the diff and apply it only with "
        "apply_diff, which asks for the user's explicit yes.",
    }


def diagnose_failure(
    tb_path: str,
    module_path: str = "",
    rtl_dir: str = "",
    compile_check: Callable[[list[Path]], str | None] | None = None,
    propose: Callable[[str, str], dict] | None = None,
    data_dirs: list[Path] | None = None,
) -> tuple[dict, dict | None]:
    """debug_failing_test's core: run, locate, one root-cause call. If the
    model names a fix and `propose` is given, propose(file, instruction) is
    called once (modify_module, or verify_loop's store-free propose_edit).
    Returns (result, proposal or None); a "failing" result carries "_note"."""
    with tempfile.TemporaryDirectory(prefix="copilot_debug_") as tmp:
        vcd = Path(tmp) / "debug.vcd"
        run = testbench_runner(module_path=module_path, tb_path=tb_path, rtl_dir=rtl_dir,
                               compile_check=compile_check, vcd_out=vcd, data_dirs=data_dirs)
        status = run.get("status")
        if status == "pass":
            return {"status": "pass", "message": "The testbench passes; there is nothing to debug.",
                    "pass_lines": run.get("pass_lines")}, None
        if status != "fail_or_unknown":
            return {
                "status": "not_debuggable",
                "testbench_status": status,
                "message": _NOT_DEBUGGABLE.get(status, run.get("message", "The testbench could not be run.")),
                **{k: run[k] for k in ("headline", "missing_data_files", "stderr", "duplicates") if run.get(k)},
            }, None
        if not run.get("fail_lines") and not run.get("exit_code"):
            return {"status": "not_debuggable", "testbench_status": status,
                    "message": "The testbench prints no PASS/FAIL lines, so there is no failing check to start from."}, None

        # Always the EARLIEST failure: a later line may be a downstream symptom.
        failing = (run.get("first_failures") or [""])[0]
        paths = [Path(p) for p in run.get("_compiled_paths", [])]
        location, why_not = None, "no waveform was produced"
        if run.get("vcd_path") and failing and paths:
            tb_text = paths[0].read_text(encoding="utf-8", errors="replace")  # paths[0] is the testbench
            location, why_not = _locate_failure(vcd, failing, run["top"][0], _field_radixes(tb_text))
        waveform = []
        if location:
            for signal in location["signals"]:
                w = waveform_summarizer(str(vcd), signal, t_start=location["window"][0], t_end=location["window"][1])
                if w.get("status") == "ok":
                    waveform.append({k: w[k] for k in ("signal", "value_at_start", "changes")})

    sources, shown, _ = _numbered_sources(paths)
    evidence = {
        "headline": run.get("headline"),
        "first_failure": failing,
        "failure_time": f"{location['time']} ({location['time_unit']})" if location else None,
        "waveform": waveform,
        **({} if location else {"waveform_note": f"No waveform window: {why_not}."}),
    }
    user = (
        f"Simulation summary: {run.get('headline')}\n"
        f"First failing check: {failing}\n"
        f"Other failures: {json.dumps(run.get('first_failures', [])[1:6])}\n"
        f"Waveform around the first failure (time unit {location['time_unit'] if location else 'n/a'}): "
        f"{json.dumps(waveform) if waveform else 'not available'}\n\n"
        f"Source files:\n{sources}"
    )
    try:
        review = _ask_root_cause([{"role": "system", "content": _DEBUG_PROMPT}, {"role": "user", "content": user}])
    except Exception as exc:  # noqa: BLE001 - keep the evidence even without a diagnosis
        return {"status": "error", "message": f"Root-cause analysis unavailable ({type(exc).__name__}).",
                "evidence": evidence}, None

    target = _resolve_file(review.get("file"), shown)
    line = review.get("line") if isinstance(review.get("line"), int) else None
    if target and line is not None and not 1 <= line <= len(target.read_text(encoding="utf-8", errors="replace").splitlines()):
        line = None
    confidence = review.get("confidence") if review.get("confidence") in ("high", "medium", "low") else "low"
    instruction = str(review.get("fix_instruction") or "").strip()

    proposal, note = None, None
    if target and instruction:
        if propose is not None:
            proposal = propose(str(target), f"{instruction} Change nothing else.")
    elif not target:
        note = "The model named no file it was shown, so no fix was proposed."
    else:
        note = "The model proposed no safe fix."

    return {
        "status": "failing",
        "failing_check": str(review.get("failing_check") or failing),
        "root_cause": str(review.get("root_cause") or ""),
        "file": str(target) if target else review.get("file"),
        "line": line,
        "confidence": confidence,
        "evidence": evidence,
        "fix_instruction": instruction,
        "_note": note,
    }, proposal


# ----------------------------------------------------------- testbench_auditor

_EXPECT_NAMES = r"(?:expected|expect|exp|golden|gold|ref|reference|model|predicted|want|correct)\w*"
_EXPECT_ASSIGN_RE = re.compile(
    rf"(?:^|[\s:)])(?P<lhs>{_EXPECT_NAMES}(?:\s*\[[^\]]*\])?)\s*(?:<=|=)(?!=)\s*(?P<rhs>[^;]+);", re.IGNORECASE
)
_LABEL_RE = re.compile(r"^\s*(?P<label>[^;:=()]+?)\s*:(?!=)")
_SHIFT_RE = re.compile(r"(<<<|>>>|<<|>>)\s*(\$?[A-Za-z_]\w*)(\s*\[[^\]]*\])?")
_AUDIT_PROMPT = """\
You audit a Verilog testbench's expected-value model against the RTL it tests (and the ISA spec, \
if one is given). For each listed expected-value computation (E1, E2, ...), decide whether it computes \
what the RTL or spec actually defines for that case. Report only real mismatches, not style. Respond \
with ONLY a JSON object: {"findings": [{"id": "<the E-number from the list>", "rtl_ref": "<file:line \
in the RTL or spec that defines the behaviour>", "explanation": "<one line>", "severity": \
"high" | "medium" | "low"}]}. An empty list means every expected value matches.
"""


def _blank_noise(text: str) -> str:
    """Whole source with comments (incl. multi-line /* */) and string
    literals replaced by spaces, newlines kept: positions and line numbers
    stay valid, and text inside a $display or a comment is never code."""
    return _CODE_NOISE_RE.sub(lambda m: re.sub(r"[^\n]", " ", m.group(0)), text)


_ARM_ONLY_RE = re.compile(r"^\s*(?P<label>[^;:=()]+?)\s*:(?!=)\s*(?:begin\s*)?$")


def _line_label(code: str, pos: int) -> tuple[int, str | None]:
    """(1-based line, case label of the statement at pos): a label before it
    on the same line, or a case arm standing alone on the previous code line
    ("2'd0:" or "2'd0: begin")."""
    start = code.rfind("\n", 0, pos) + 1
    line = code.count("\n", 0, pos) + 1
    label = _LABEL_RE.match(code[start:pos])
    if label:
        return line, label.group("label").strip()
    if ";" in code[start:pos]:
        return line, None  # an earlier statement on this line: not the arm's first statement
    previous = [l for l in code[:start].splitlines() if l.strip()]
    arm = _ARM_ONLY_RE.match(previous[-1]) if previous else None
    return line, arm.group("label").strip() if arm else None


def _expected_value_lines(tb_text: str) -> list[dict]:
    """Every assignment to an expected/golden/reference-style variable,
    including several on one line and ones that span lines."""
    code = _blank_noise(tb_text)
    raw_lines = tb_text.splitlines()
    found = []
    for m in _EXPECT_ASSIGN_RE.finditer(code):
        line, label = _line_label(code, m.start("lhs"))
        end_line = code.count("\n", 0, m.end()) + 1
        text = " ".join(" ".join(raw_lines[line - 1:end_line]).split())
        found.append({"id": f"E{len(found) + 1}", "line": line, "code": text,
                      "rhs": " ".join(m.group("rhs").split()), "label": label})
    return found


def _shift_amount_findings(expected: list[dict], rtl_files: list[Path]) -> list[dict]:
    """Rule: the testbench shifts by a full operand where the RTL shifts by a
    slice of it (a << b vs a << b[4:0]) - wrong whenever the operand exceeds
    the slice. Only definitive: every RTL shift of that operand is considered
    (sliced or not); a labelled testbench line (a case arm) is compared with
    the RTL arm of the SAME label, an unlabelled one with the RTL's only such
    shift, and it is flagged only if that one RTL shift is sliced."""
    shifts: dict[tuple[str, str], list[tuple[Path, int, str | None, str]]] = {}
    for f in rtl_files:
        code = _blank_noise(f.read_text(encoding="utf-8", errors="replace"))
        for m in _SHIFT_RE.finditer(code):
            line, label = _line_label(code, m.start())
            shifts.setdefault((m.group(1), m.group(2)), []).append((f, line, label, (m.group(3) or "").strip()))
    findings = []
    for e in expected:
        for op, operand, part in _SHIFT_RE.findall(e["rhs"]):
            if part.strip():
                continue
            refs = shifts.get((op, operand)) or []
            if e["label"]:
                refs = [r for r in refs if r[2] == e["label"]]
            if len(refs) != 1 or not refs[0][3]:
                continue  # no corresponding RTL shift, ambiguous, or the RTL doesn't slice either
            f, n, _, part_rtl = refs[0]
            findings.append({
                "id": e["id"], "line": e["line"], "code": e["code"], "rtl_ref": f"{f.name}:{n}",
                "severity": "high", "source": "rule",
                "explanation": f"Expected value shifts by the full `{operand}`, but the RTL ({f.name}:{n}) shifts by "
                f"`{operand}{part_rtl}`, so any `{operand}` outside that range gives a wrong expected value.",
            })
            break
    return findings


def testbench_auditor(tb_path: str, module_path: str = "", rtl_dir: str = "", spec_path: str = "") -> dict:
    """Audits a testbench's expected-value model against the RTL (and spec):
    every expected-value computation is extracted and checked; mismatches are
    reported with file:line and a one-line explanation. Report only."""
    tb = Path(tb_path).resolve() if tb_path else None
    if tb is None or not tb.is_file():
        return {"status": "error", "message": f"No testbench file at '{tb_path}'."}
    if not module_path and not rtl_dir:
        return {"status": "error", "message": "Give module_path (a single design file) or rtl_dir (a folder of RTL sources)."}
    search: list[Path] = []
    if module_path:
        if not Path(module_path).is_file():
            return {"status": "error", "message": f"No design file at '{module_path}'."}
        search.append(Path(module_path).resolve())
    if rtl_dir:
        if not Path(rtl_dir).is_dir():
            return {"status": "error", "message": f"No RTL folder at '{rtl_dir}'."}
        search += [p.resolve() for p in project_files(Path(rtl_dir), (".v", ".sv"))]
    search = list(dict.fromkeys(p for p in search if p != tb))
    # The RTL the testbench actually instantiates (same walk as testbench_runner).
    _, files, _ = design_files(tb, search)
    rtl_files = [f for f in files if f != tb] or [p for p in search if not is_testbench(p)]

    tb_text = tb.read_text(encoding="utf-8", errors="replace")
    expected = _expected_value_lines(tb_text)
    if not expected:
        return {"status": "no_expected_values",
                "message": "No expected-value computations found (no assignments to expected/golden/ref/model-style "
                "variables), so there is nothing to audit."}
    findings = _shift_amount_findings(expected, rtl_files)

    spec = Path(spec_path) if spec_path and Path(spec_path).is_file() else None
    considered = [tb, *rtl_files, *([spec] if spec else [])]
    sources, shown, truncated = _numbered_sources(considered)
    omitted = [p for p in considered if p not in shown]
    listing = "\n".join(f"{e['id']} (L{e['line']}): {e['code']}" for e in expected)
    model_note = None
    try:
        review = _ask_root_cause([
            {"role": "system", "content": _AUDIT_PROMPT},
            {"role": "user", "content": f"Expected-value computations in {tb.name}:\n{listing}\n\nSource files:\n{sources}"},
        ])
        items = review.get("findings")
        if not isinstance(items, list):  # {} or {"findings": null} is not a review
            raise ValueError("reply has no findings list")
    except Exception as exc:  # noqa: BLE001 - the rule findings still stand
        items, model_note = [], f"model review unavailable ({type(exc).__name__}); rule checks only"
    by_id = {e["id"]: e for e in expected}
    flagged = {f["id"] for f in findings}
    shown_names = {p.name for p in shown}
    for item in items:
        if not isinstance(item, dict):
            continue
        e = by_id.get(str(item.get("id", "")).strip().upper())
        if e is None:  # fall back to a line number only when it names one computation
            same_line = [x for x in expected if x["line"] == item.get("tb_line")]
            e = same_line[0] if len(same_line) == 1 else None
        if e is None:
            continue  # only real expected-value computations
        if e["id"] in flagged:  # the model independently agrees with a rule finding
            for f in findings:
                if f["id"] == e["id"] and f["source"] == "rule":
                    f["confirmed_by_model"] = True
            continue
        ref = str(item.get("rtl_ref") or "")
        findings.append({
            "id": e["id"], "line": e["line"], "code": e["code"],
            "rtl_ref": ref if ref.split(":", 1)[0] in shown_names else None,
            "severity": item.get("severity") if item.get("severity") in ("high", "medium", "low") else "medium",
            "source": "model", "explanation": str(item.get("explanation") or "")[:300],
        })
        flagged.add(e["id"])
    findings.sort(key=lambda f: (f["line"], f["id"]))
    for f in findings:
        f["file"] = tb.name
    partial = bool(truncated or omitted) or bool(model_note)
    if model_note:
        why = "only the shift-amount rule ran, the model review was unavailable."
    else:
        why = (f"the model saw only part of the sources (truncated: {', '.join(p.name for p in truncated) or 'none'}; "
               f"omitted: {', '.join(p.name for p in omitted) or 'none'}).")
    return {
        "status": "ok",
        "testbench": str(tb),
        "expected_values_found": len(expected),
        "expected_values_checked": 0 if model_note else len(expected),  # reviewed against the RTL by the model
        "findings": findings,
        "coverage": "partial" if partial else "complete",
        "context": {"shown": [p.name for p in shown], "truncated": [p.name for p in truncated],
                    "omitted": [p.name for p in omitted]},
        "summary": f"{len(findings)} mismatch(es) in {len(expected)} expected-value computation(s)"
        + (f"; partial check: {why}" if partial else "."),
        **({"model_review": model_note} if model_note else {}),
        "note": "Report only: nothing was changed. To fix a finding, ask for it; the fix goes through "
        "modify_module and apply_diff, which asks for the user's explicit yes.",
    }


SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "debug_failing_test",
            "description": "Debug a failing Verilog testbench: runs it, finds the first failing check in the "
            "waveform, explains the most likely root cause (file, line, confidence) and proposes a fix as a "
            "pending diff. Writes nothing; the diff is applied only via apply_diff after the user approves it. "
            "Give the testbench plus module_path (single-file design) or rtl_dir (folder of RTL sources).",
            "parameters": {
                "type": "object",
                "properties": {
                    "tb_path": {"type": "string", "description": "The failing testbench file."},
                    "module_path": {"type": "string", "description": "Design file, for a single-file design."},
                    "rtl_dir": {"type": "string", "description": "Folder with the design's RTL sources."},
                },
                "required": ["tb_path"],
            },
        },
    },
]

SCHEMAS.append({
    "type": "function",
    "function": {
        "name": "testbench_auditor",
        "description": "Audit a testbench's expected-value model: extract every expected-value computation and "
        "check it against what the RTL (and the ISA spec, if given) defines; report mismatches with file:line and "
        "a one-line explanation. Report only; fixes go through modify_module if the user asks.",
        "parameters": {
            "type": "object",
            "properties": {
                "tb_path": {"type": "string", "description": "The testbench file to audit."},
                "module_path": {"type": "string", "description": "Design file, for a single-file design."},
                "rtl_dir": {"type": "string", "description": "Folder with the design's RTL sources."},
                "spec_path": {"type": "string", "description": "Optional ISA/design spec file."},
            },
            "required": ["tb_path"],
        },
    },
})

IMPLS = {"debug_failing_test": debug_failing_test, "testbench_auditor": testbench_auditor}
