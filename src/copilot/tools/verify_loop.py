"""Skill: verify_loop, a closed verification loop for one design change.

Copies the project into a temp folder and does ALL its work there: the model
makes the change (modify_module's edit call; testbenches that instantiate the
module are updated too), every testbench that uses the module runs
(testbench_runner) plus lint (lint_checker), and a failure is debugged
(debug_failing_test's diagnosis) and fixed in the copy, for at most 3 rounds.

The result is one combined diff (temp copy vs the user's files), staged as a
single pending diff. Only apply_diff, behind the human approval gate, can
write it; passing tests are never an approval. The temp copy is deleted on
every exit path, and nothing here runs git.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Callable

from ..llm import count_model_calls
from .debugging import diagnose_failure
from .hdl_safety import new_risky_features
from .module_modifier import propose_edit, stage_pending
from .rtl_files import _DEF_RE, _instantiates, _read_code, design_files, is_testbench, project_files
from .verification import _tool_available, _vivado_mem_init_dirs, lint_checker, testbench_runner

_MAX_ROUNDS = 3
_MAX_TB_EDITS = 3  # round 1: RTL edit + at most 3 testbench edits = 4 model calls
_MAX_TESTBENCHES = 8  # testbenches run per round
_COPY_MAX_FILES = 3000
_COPY_MAX_BYTES = 50 * 1024 * 1024
_DIFF_IN_PROMPT = 6000

_RTL_INSTRUCTION = (
    "Design goal: {goal}\n"
    "Make the change this goal needs in this file. Keep every existing behaviour, port and encoding unless "
    "the goal requires changing it. Change nothing else."
)
_TB_INSTRUCTION = (
    "Design goal: {goal}\n"
    "The design file {module} was just changed for this goal:\n{diff}\n"
    "Update this testbench to match: it must still compile against the changed module (ports, parameters, "
    "encodings), and if the goal adds or changes behaviour this testbench exercises, add checks for it in "
    "the same style and with the same PASS/FAIL messages as the existing checks. Do not change existing "
    "checks unless the design change makes them wrong. If no change is needed, return the file unchanged."
)
_COMPILE_FIX_INSTRUCTION = (
    "Icarus Verilog reports these compile errors:\n{stderr}\n"
    "Fix them in this file while keeping the intended change (design goal: {goal}). Change nothing else."
)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _copy_project(root: Path, dest: Path) -> dict[Path, str]:
    """Copies the project's own files (generated/VCS trees skipped) into dest.
    Returns relative path -> sha256 of the original bytes."""
    files = project_files(root)
    if len(files) > _COPY_MAX_FILES:
        raise ValueError(f"{len(files)} files under {root.name}; pass a narrower project_dir.")
    total = sum(f.stat().st_size for f in files)
    if total > _COPY_MAX_BYTES:
        raise ValueError(f"{total // (1024 * 1024)} MB under {root.name}; pass a narrower project_dir.")
    hashes = {}
    for f in files:
        rel = f.relative_to(root)
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        data = f.read_bytes()
        target.write_bytes(data)
        hashes[rel] = _sha(data)
    return hashes


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


class _Copy:
    """The temp copy. Every write in the loop goes through write(), which
    refuses any path outside the copy."""

    def __init__(self, root: Path, base: Path | None = None):
        self.root = root
        self.base = base or root  # the whole temp folder (copy + data snapshot)
        self.edited: list[Path] = []

    def write(self, path: Path, content: str) -> None:
        path = path.resolve()
        if not _inside(path, self.root):
            raise RuntimeError(f"refusing to write outside the temp copy: {path}")
        path.write_text(content, encoding="utf-8")
        if path not in self.edited:
            self.edited.append(path)

    def rel(self, path: Path) -> str:
        return path.resolve().relative_to(self.root.resolve()).as_posix()

    def untemp(self, text: str) -> str:
        """Temp paths -> project-relative, so results never point at the copy."""
        text = str(text)
        for folder in (self.root, self.base):
            for base in {str(folder.resolve()), str(folder)}:
                for form in (base, base.replace("\\", "/")):
                    text = text.replace(form + os.sep, "").replace(form + "/", "").replace(form, ".")
        return text


def _accept(copy: _Copy, path: Path, new: str) -> str | None:
    """Writes a model edit into the copy, unless it ADDS file or process
    access ($fopen, $dumpfile, `include, ...): that would run, unreviewed, on
    this machine at the next simulation. Returns the refusal reason or None."""
    added = new_risky_features(path.read_text(encoding="utf-8"), new)
    if added:
        return (f"The proposed edit to {path.name} adds file/process access ({', '.join(added[:4])}); "
                "the loop does not simulate that unreviewed.")
    copy.write(path, new)
    return None


def _snapshot_data(dirs: list[Path], dest: Path) -> tuple[list[Path], dict[Path, str]]:
    """Copies Vivado mem_init folders (outside the copied project) into
    dest/<n>. Returns (copied folders, real file -> sha256)."""
    out, hashes = [], {}
    for i, d in enumerate(dirs):
        target = dest / str(i)
        for f in project_files(d):
            if f.stat().st_size > _COPY_MAX_BYTES:
                continue
            data = f.read_bytes()
            (target / f.relative_to(d)).parent.mkdir(parents=True, exist_ok=True)
            (target / f.relative_to(d)).write_bytes(data)
            hashes[f] = _sha(data)
        out.append(target)
    return out, hashes


def _propose(path: Path, instruction: str) -> dict:
    """propose_edit, with a model/network failure turned into an error result."""
    try:
        return propose_edit(str(path), instruction)
    except Exception as exc:  # noqa: BLE001 - one failed call must not skip cleanup or the report
        return {"status": "error", "message": f"model call failed ({type(exc).__name__})"}


def _module_names(path: Path) -> list[str]:
    return _DEF_RE.findall(_read_code(path))


def _affected_testbenches(copy: _Copy, module: Path, target_tb: Path) -> tuple[list[Path], list[Path]]:
    """(testbenches whose design includes the module, target first; the ones
    among them that instantiate it directly)."""
    hdl = project_files(copy.root, (".v", ".sv"))
    rtl = [f for f in hdl if not is_testbench(f) or f == module]
    found = [target_tb]
    for tb in hdl:
        if tb == target_tb or tb == module or not is_testbench(tb):
            continue
        siblings = [p for p in tb.parent.iterdir() if p.suffix.lower() in (".v", ".sv") and p != tb]
        try:
            _, files, _ = design_files(tb, [*rtl, *siblings])
        except OSError:
            continue
        if module in files:
            found.append(tb)
    names = _module_names(module)
    direct = [tb for tb in found if any(_instantiates(_read_code(tb), n) for n in names)]
    return found, direct


def _verdict(run: dict) -> str:
    status = run.get("status")
    if status == "pass":
        return f"pass ({run.get('pass_lines', 0)} checks)"
    if status == "fail_or_unknown":
        return f"FAIL ({run.get('fail_lines', 0)} failing, {run.get('pass_lines', 0)} passing)"
    return str(status).replace("_", " ")


def _lint(path: Path, compile_check) -> int | str:
    if compile_check is not None and (reason := compile_check([path])):
        return f"blocked ({reason})"
    res = lint_checker(str(path))
    if res.get("status") in ("clean", "issues_found"):
        return res.get("warning_count", 0)
    return str(res.get("status"))


def _lint_text(copy: _Copy, results: dict[Path, int | str], baseline: dict[Path, int | str]) -> str:
    parts = []
    for path, count in results.items():
        was = baseline.get(path)
        if isinstance(count, int):
            text = "clean" if count == 0 else f"{count} warning(s)"
            if isinstance(was, int) and was != count:
                text += f" (was {was})"
        else:
            text = count
        parts.append(f"{Path(copy.rel(path)).name}: {text}")
    return "; ".join(parts)


def _compile_error_file(stderr: str, copy: _Copy) -> Path | None:
    """The file iverilog blames, preferring files the loop edited."""
    candidates = [*copy.edited, *project_files(copy.root, (".v", ".sv", ".vh", ".svh"))]
    for p in candidates:
        forms = {str(p), str(p.resolve()), p.as_posix(), p.resolve().as_posix()}
        if any(f + ":" in stderr for f in forms):
            return p
    for p in candidates:
        if re.search(rf"(?:^|[\\/\s]){re.escape(p.name)}:\d+:", stderr, re.MULTILINE):
            return p
    return None


def _unified(old: str, new: str, label: str) -> str:
    return "".join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True),
                                        fromfile=f"a/{label}", tofile=f"b/{label}"))


def _table(attempts: list[dict]) -> str:
    def cell(text: str) -> str:
        return " ".join(str(text).split()).replace("|", "\\|") or "-"

    rows = ["| Round | Change | Test result | Root cause if failed |", "|---|---|---|---|"]
    rows += [f"| {a['round']} | {cell(a['change'])} | {cell(a['test_result'])} | {cell(a['root_cause'])} |"
             for a in attempts]
    return "\n".join(rows)


def verify_loop(
    goal: str,
    module_path: str,
    tb_path: str,
    project_dir: str = "",
    rtl_dir: str = "",
    compile_check: Callable[[list[Path]], str | None] | None = None,
) -> dict:
    """Makes a design change in a temp copy, tests and fixes it there (max 3
    rounds), and returns one combined diff as a pending diff for apply_diff.
    Never writes to the user's files; the temp copy is always deleted.

    compile_check (internal, not in the tool schema) is passed on to every
    simulation and run on every lint input (the web demo's HDL check)."""
    goal = " ".join(str(goal or "").split())
    if not goal:
        return {"status": "error", "message": "Describe the change to make (goal)."}
    module = Path(module_path).resolve() if module_path else None
    tb = Path(tb_path).resolve() if tb_path else None
    for label, p in (("module_path", module), ("tb_path", tb)):
        if p is None or not p.is_file() or p.suffix.lower() not in (".v", ".sv"):
            return {"status": "error", "message": f"{label} must be an existing .v/.sv file."}
    if project_dir:
        root = Path(project_dir).resolve()
    else:
        root = Path(os.path.commonpath([str(module.parent), str(tb.parent)]))
    if not root.is_dir() or not (_inside(module, root) and _inside(tb, root)):
        return {"status": "error", "message": "project_dir must be a folder containing both module_path and tb_path."}
    if root == Path(root.anchor) or root == Path.home().resolve():
        return {"status": "error", "message": "Pass project_dir: the module and testbench only share a drive "
                "root or home folder, which is too much to copy."}
    rtl_root = Path(rtl_dir).resolve() if rtl_dir else module.parent
    if not rtl_root.is_dir() or not _inside(rtl_root, root):
        return {"status": "error", "message": "rtl_dir must be a folder inside project_dir."}
    if not (_tool_available("iverilog") and _tool_available("vvp")):
        return {"status": "unavailable", "message": "iverilog/vvp not found on PATH; the loop needs a simulator."}

    with count_model_calls() as calls:
        tmp = Path(tempfile.mkdtemp(prefix="copilot_verify_")).resolve()
        try:
            result = _run(goal, root, tmp, module, tb, rtl_root, compile_check, calls)
        except Exception as exc:  # noqa: BLE001 - report, and still clean up below
            result = {"status": "error", "message": f"verify_loop stopped: {type(exc).__name__}: {exc}",
                      "pending_diff": None}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
        result["model_calls"] = calls[0]
    result["temp_copy_removed"] = not tmp.exists()
    return result


def _run(goal: str, root: Path, tmp: Path, module: Path, tb: Path, rtl_root: Path,
         compile_check, calls: list[int]) -> dict:
    proj = tmp / "project"
    hashes = _copy_project(root, proj)
    # Program/memory files Vivado keeps outside the project folder: snapshot them too.
    data_dirs, data_hashes = _snapshot_data(
        [d for d in _vivado_mem_init_dirs(tb.parent) if not _inside(d, root)], tmp / "data")
    copy = _Copy(proj, base=tmp)
    t_module, t_tb, t_rtl = proj / module.relative_to(root), proj / tb.relative_to(root), proj / rtl_root.relative_to(root)
    if not (t_module.is_file() and t_tb.is_file()):
        return {"status": "error", "message": "module_path or tb_path sits in a generated folder that is not copied."}

    tests, direct = _affected_testbenches(copy, t_module, t_tb)
    not_run = tests[_MAX_TESTBENCHES:]
    tests = tests[:_MAX_TESTBENCHES]

    def run_all() -> dict[Path, dict]:
        return {t: testbench_runner(tb_path=str(t), rtl_dir=str(t_rtl), compile_check=compile_check,
                                    data_dirs=data_dirs) for t in tests}

    baseline = run_all()
    if baseline[t_tb].get("status") in ("unavailable", "blocked", "ambiguous_design", "error"):
        return {"status": "not_runnable", "message": copy.untemp(baseline[t_tb].get("message", baseline[t_tb]["status"])),
                "testbench_status": baseline[t_tb]["status"], "pending_diff": None}
    # Done = the target testbench passes, nothing that passed before breaks,
    # and every testbench the loop itself edited passes.
    must_pass = [t_tb] + [t for t in tests[1:] if baseline[t].get("status") == "pass"]
    lint_base = {t_module: _lint(t_module, compile_check)}

    # Round 1's change: the RTL, then the testbenches that instantiate it.
    start = calls[0]
    original = t_module.read_text(encoding="utf-8")
    rtl = _propose(t_module, _RTL_INSTRUCTION.format(goal=goal))
    if rtl["status"] != "ok":
        return {"status": "error", "message": f"Could not get the change from the model: {rtl.get('message')}",
                "pending_diff": None}
    changes = []
    if rtl["new_content"] != original:
        if refused := _accept(copy, t_module, rtl["new_content"]):
            return {"status": "blocked", "message": refused, "pending_diff": None}
        changes.append(f"{t_module.name}: {rtl['explanation']}")
    rtl_diff = _unified(original, rtl["new_content"], copy.rel(t_module))
    tb_skipped = direct[_MAX_TB_EDITS:]
    for t in direct[:_MAX_TB_EDITS]:
        before = t.read_text(encoding="utf-8")
        edit = _propose(t, _TB_INSTRUCTION.format(goal=goal, module=copy.rel(t_module),
                                                  diff=rtl_diff[:_DIFF_IN_PROMPT] or "(no change)"))
        if edit["status"] == "ok" and edit["new_content"] != before:
            if refused := _accept(copy, t, edit["new_content"]):
                return {"status": "blocked", "message": refused, "pending_diff": None}
            changes.append(f"{t.name}: {edit['explanation']}")
    tb_changed = [t for t in copy.edited if t != t_module]
    if not copy.edited:
        return {"status": "no_change", "message": "The model made no change for this goal; nothing to test.",
                "explanation": rtl.get("explanation"), "pending_diff": None}

    attempts: list[dict] = []
    change, change_calls = "; ".join(changes), calls[0] - start
    status, stop_reason, final_diagnosis_calls = "still_failing", "", 0
    for rnd in range(1, _MAX_ROUNDS + 1):
        runs = run_all()
        lint_now = {p: _lint(p, compile_check) for p in copy.edited if not is_testbench(p) or p == t_module}
        row = {
            "round": rnd,
            "change": copy.untemp(change),
            "test_result": "; ".join(f"{t.name}: {_verdict(runs[t])}" for t in tests),
            "lint": _lint_text(copy, lint_now, lint_base),
            "root_cause": "",
            "model_calls": change_calls,
        }
        attempts.append(row)
        required = must_pass + [t for t in tests if t in copy.edited and t not in must_pass]
        failing = [t for t in required if runs[t].get("status") != "pass"]
        if not failing:
            status = "passed"
            break
        last = rnd == _MAX_ROUNDS
        bad = failing[0]
        run = runs[bad]
        start = calls[0]
        change = ""
        if run.get("status") == "compile_error":
            stderr = run.get("stderr", "")
            first = next((l for l in stderr.splitlines() if l.strip()), "compile error")
            row["root_cause"] = copy.untemp(f"{bad.name} does not compile: {first}")
            target = _compile_error_file(stderr, copy)
            if not last:
                if target is None:
                    stop_reason = "iverilog's errors don't name a file in the project, so no fix was attempted."
                else:
                    fix = _propose(target, _COMPILE_FIX_INSTRUCTION.format(stderr=stderr[-3000:], goal=goal))
                    if fix["status"] == "ok" and fix["new_content"] != target.read_text(encoding="utf-8"):
                        if refused := _accept(copy, target, fix["new_content"]):
                            stop_reason = refused
                        else:
                            change = f"{target.name}: fix compile error ({fix['explanation']})"
                    else:
                        stop_reason = "The model proposed no fix for the compile error."
        elif run.get("status") == "fail_or_unknown":
            diag, fix = diagnose_failure(str(bad), rtl_dir=str(t_rtl), compile_check=compile_check,
                                         propose=None if last else _propose, data_dirs=data_dirs)
            if diag.get("status") == "failing":
                where = Path(diag["file"]).name if diag.get("file") else "?"
                row["root_cause"] = copy.untemp(
                    f"{bad.name}: {where}:{diag.get('line') or '?'} ({diag.get('confidence')}): {diag.get('root_cause')}")
                target = Path(diag["file"]) if diag.get("file") else None
                if not last:
                    if fix is not None and fix.get("status") == "ok" and target is not None and _inside(target, proj) \
                            and fix["new_content"] != target.read_text(encoding="utf-8"):
                        if refused := _accept(copy, target, fix["new_content"]):
                            stop_reason = refused
                        else:
                            change = f"{target.name}: {fix['explanation']}"
                    else:
                        stop_reason = diag.get("_note") or "The model proposed no fix for this failure."
            else:
                row["root_cause"] = copy.untemp(f"{bad.name}: {diag.get('message', diag.get('status'))}")
                stop_reason = "The failure could not be diagnosed."
        else:
            row["root_cause"] = copy.untemp(f"{bad.name}: {_verdict(run)}. {run.get('message', '')}".strip())
            stop_reason = "This failure is not something the loop can debug."
        if last:
            final_diagnosis_calls = calls[0] - start
        change_calls = calls[0] - start
        if stop_reason:
            break

    # Every input the verdict rests on must be unchanged on disk; otherwise
    # the result is about files that no longer exist in that form.
    changed = [rel.as_posix() for rel, h in hashes.items()
               if not (root / rel).is_file() or _sha((root / rel).read_bytes()) != h]
    changed += [str(f) for f, h in data_hashes.items() if not f.is_file() or _sha(f.read_bytes()) != h]
    if changed:
        return {"status": "error", "pending_diff": None, "attempts": attempts, "attempts_table": _table(attempts),
                "message": "These files changed on disk while the loop ran, so its results are stale and no diff "
                f"was staged: {', '.join(changed[:5])}. Run it again."}

    # One combined diff: temp copy vs the user's files.
    files, diffs = [], []
    for t in copy.edited:
        rel = t.relative_to(proj)
        real = root / rel
        old, new = real.read_text(encoding="utf-8"), t.read_text(encoding="utf-8")
        if new != old:
            files.append((str(real), new))
            diffs.append(_unified(old, new, str(real)))
    diff_id = stage_pending(files) if files else None

    rounds = len(attempts)
    final = "passed" if status == "passed" else f"still failing after {rounds} round(s)"
    # Affected testbenches that never passed and weren't edited: not verified.
    unverified = {copy.rel(t): _verdict(runs[t]) for t in tests
                  if t not in must_pass and t not in copy.edited and runs[t].get("status") != "pass"} if attempts else {}
    note = ("Nothing was written to your files, and the temp copy has been deleted. The combined diff waits as "
            f"pending diff {diff_id}: apply it only with apply_diff, which shows it and needs the user's explicit "
            "yes. Passing tests are not an approval." if diff_id else
            "Nothing was written to your files, and the temp copy has been deleted. No net change to stage.")
    if status != "passed":
        note = "The tests still fail with this change; review it before deciding. " + note
    elif unverified:
        note = (f"Passed, but {len(unverified)} affected testbench(es) already failed before the change and "
                "were not chased, so they don't verify it (see unverified). " + note)
    return {
        "status": status,
        "final_status": final,
        "goal": goal,
        "testbenches": [copy.rel(t) for t in tests],
        "testbenches_updated": [copy.rel(t) for t in tb_changed],
        **({"testbenches_not_updated": [copy.rel(t) for t in tb_skipped]} if tb_skipped else {}),
        **({"testbenches_not_run": [copy.rel(t) for t in not_run]} if not_run else {}),
        "baseline": {copy.rel(t): _verdict(r) for t, r in baseline.items()},
        "verification": "partial" if unverified or not_run else "complete",
        **({"unverified": unverified} if unverified else {}),
        "attempts": attempts,
        "attempts_table": _table(attempts),
        **({"stopped_early": stop_reason} if stop_reason else {}),
        **({"final_diagnosis_calls": final_diagnosis_calls} if final_diagnosis_calls else {}),
        "diff": "".join(diffs) or "(no changes)",
        "pending_diff": {"diff_id": diff_id, "files": [str(Path(p).relative_to(root).as_posix()) for p, _ in files]}
        if diff_id else None,
        "note": note,
    }


SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "verify_loop",
            "description": (
                "Closed verification loop for one design change. Copies the project to a temp folder; there the "
                "model makes the change (RTL, plus the testbenches that instantiate the module), every testbench "
                "that uses the module runs with lint, and failures are debugged and fixed, for at most 3 rounds. "
                "Returns an attempts table, a final status and ONE combined diff staged as a pending diff for "
                "apply_diff. Never writes the user's files; show the diff and ask before apply_diff."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {"type": "string", "description": "Plain-English change, e.g. 'add a NOR op to the ALU'."},
                    "module_path": {"type": "string", "description": "The design file to change."},
                    "tb_path": {"type": "string", "description": "The testbench that must pass (gets new checks)."},
                    "project_dir": {"type": "string", "description": "Folder to copy (default: the common parent of "
                                    "module_path and tb_path)."},
                    "rtl_dir": {"type": "string", "description": "RTL folder inside project_dir (default: the "
                                "module's folder)."},
                },
                "required": ["goal", "module_path", "tb_path"],
            },
        },
    },
]

IMPLS = {"verify_loop": verify_loop}
