"""Skills that create NEW files: generate_rtl.

Requirements -> interface spec + assumptions (docs.draft_interface_spec) ->
module (Nemotron, given the spec and a style sample read from the repo) ->
lint (Verilator) + compile (iverilog) in a temp copy of the repo, errors fed
back, at most 3 rounds -> the new file staged as a pending diff against
/dev/null. Only apply_diff, behind the human approval gate, creates it;
it refuses to overwrite and stays inside the repo root. No git anywhere.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
from collections import Counter
from pathlib import Path
from typing import Callable

from ..llm import count_model_calls, get_client, json_completion
from .docs import draft_interface_spec
from .hdl_safety import access_features
from .module_modifier import NewFilePathError, check_new_file_path, stage_pending, unified_diff_text
from .rtl_files import is_testbench, project_files
from .verification import _tool_available, lint_checker
from .verify_loop import _copy_project, _inside

_MAX_ROUNDS = 3
_SPEC_MAX_CHARS = 20000
_STYLE_EXCERPT_LINES = 60
_IDENT_RE = re.compile(r"^[A-Za-z_]\w*$")

_RTL_PROMPT = """\
You write synthesizable {language} RTL for ONE new module. Respond with ONLY a JSON object:
{{"verilog": "<the complete file>", "notes": "<one or two sentences>"}}
Rules: exactly one module named {module}; parameters and ports exactly as in the interface spec (names, \
directions, widths); implement every behaviour item; follow the repository style facts and sample (clock and \
reset names, reset polarity, indentation, naming); {language_rule} no system tasks that touch files or \
processes, no `include, no simulation-only constructs (no #delays, no initial blocks driving logic). Keep \
comments brief."""
_LANGUAGE = {
    ".v": ("Verilog-2005", "plain Verilog-2005 only (no logic, always_ff, always_comb, typedef, interfaces);"),
    ".sv": ("SystemVerilog", "synthesizable SystemVerilog;"),
}


# ------------------------------------------------------------------ style

def style_sample(root: Path, near: Path, testbench: bool) -> dict:
    """Style facts from 1-2 existing files of the same kind (RTL or
    testbench), preferring ones next to the target: clock name, reset name
    and polarity, indentation. Defaults when the repo has none."""
    files = [f for f in project_files(root, (".v", ".sv")) if is_testbench(f) == testbench]
    texts = {}
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if 15 <= text.count("\n") <= 600:
            texts[f] = text
    ranked = sorted(texts, key=lambda f: (f.parent != near.parent, len(f.parts), texts[f].count("\n")))[:2]
    code = "\n".join(texts[f] for f in ranked)
    clocks = Counter(re.findall(r"posedge\s+(\w+)", code))
    resets = Counter(re.findall(r"(?:posedge|negedge)\s+(\w*(?:rst|reset)\w*)", code, re.IGNORECASE)
                     + re.findall(r"if\s*\(\s*!?\s*(\w*(?:rst|reset)\w*)\s*\)", code, re.IGNORECASE))
    clock = next((c for c, _ in clocks.most_common() if not re.search("rst|reset", c, re.IGNORECASE)), None)
    reset = resets.most_common(1)[0][0] if resets else None
    low = None
    if reset:
        low = bool(re.search(rf"negedge\s+{reset}\b|if\s*\(\s*!\s*{reset}\b|{reset}\s*==\s*1'b0", code)
                   or re.search(r"(_n|_b|n)$", reset))
    indents = Counter(len(m) for m in re.findall(r"^( +)\S", code, re.MULTILINE))
    unit = min((n for n in (2, 3, 4, 8) if indents[n]), default=4)
    facts = {
        "clock": clock or "clk",
        "reset": reset or "rst_n",
        "reset_active": ("low" if low else "high") if reset else "low",
        "indent": "tabs" if re.search(r"^\t", code, re.MULTILINE) else f"{unit} spaces",
        "sample_files": [f.relative_to(root).as_posix() for f in ranked],
        "defaults_used": not ranked,
    }
    excerpt = "\n".join(texts[ranked[0]].splitlines()[:_STYLE_EXCERPT_LINES]) if ranked else ""
    return {"facts": facts, "excerpt": excerpt}


# ------------------------------------------------------ interface vs spec

_STRIP_RE = re.compile(r'"(?:\\.|[^"\\\n])*"|//[^\n]*|/\*.*?\*/', re.DOTALL)
_DIRS = ("input", "output", "inout")


def _balanced(text: str, i: int) -> int:
    """Index just past the bracket closing the one at text[i]."""
    depth = 0
    for j in range(i, len(text)):
        depth += {"(": 1, ")": -1}.get(text[j], 0)
        if depth == 0:
            return j + 1
    return len(text)


def _split(text: str) -> list[str]:
    parts, depth, cur = [], 0, ""
    for c in text:
        depth += {"(": 1, "[": 1, "{": 1, ")": -1, "]": -1, "}": -1}.get(c, 0)
        if c == "," and depth == 0:
            parts.append(cur)
            cur = ""
        else:
            cur += c
    return [p.strip() for p in [*parts, cur] if p.strip()]


def _width(rng: str | None) -> str:
    """'[WIDTH-1:0]' -> 'WIDTH', '[7:0]' -> '8', None -> '1' (spaces removed)."""
    if not rng:
        return "1"
    m = re.fullmatch(r"\[(.+):(.+)\]", rng.replace(" ", ""))
    if not m:
        return rng.replace(" ", "")
    hi, lo = m.groups()
    if lo != "0":
        return f"{hi}-{lo}+1"
    if hi.isdigit():
        return str(int(hi) + 1)
    return hi[:-2] if hi.endswith("-1") else f"{hi}+1"


_PARAM_RE = re.compile(r"^(?:parameter\s+)?(?:(?:integer|real|signed|unsigned|logic|bit)\s+)?(?:\[[^\]]*\]\s*)?"
                       r"([A-Za-z_]\w*)\s*=\s*(.+)$", re.DOTALL)


def _params(decl: str) -> dict[str, str]:
    """name -> default (spaces removed) from a parameter list or declaration."""
    found = {}
    for item in _split(decl):
        m = _PARAM_RE.match(item.strip())
        if m:
            found[m.group(1)] = re.sub(r"\s+", "", m.group(2))
    return found


def module_interface(code: str, module: str) -> dict | None:
    """{"params": {name: default}, "ports": {name: (direction, width)}} of
    `module` as written (ANSI or Verilog-1995 style), or None if absent.
    localparams are internal and not part of the interface."""
    text = _STRIP_RE.sub(" ", code)
    m = re.search(rf"\bmodule\s+{re.escape(module)}\b", text)
    if not m:
        return None
    i, params = m.end(), {}
    rest = text[i:].lstrip()
    i = len(text) - len(rest)
    if rest.startswith("#"):
        open_at = text.index("(", i)
        close = _balanced(text, open_at)
        params.update(_params(text[open_at + 1: close - 1]))
        i = close
    open_at = text.find("(", i)
    header_end = text.find(";", i)
    ports: dict[str, tuple[str, str]] = {}
    body_start = header_end + 1
    if open_at != -1 and (header_end == -1 or open_at < header_end):
        close = _balanced(text, open_at)
        body_start = text.find(";", close) + 1
        direction, rng = None, None
        for item in _split(text[open_at + 1: close - 1]):
            words = re.match(r"^(input|output|inout)?\s*(?:wire|reg|logic|signed|unsigned|\s)*(\[[^\]]*\])?\s*([A-Za-z_]\w*)\s*$", item)
            if not words:
                continue
            if words.group(1):
                direction, rng = words.group(1), words.group(2)
            ports[words.group(3)] = (direction, _width(rng)) if direction else (None, None)
    end = text.find("endmodule", body_start)
    body = text[body_start: end if end != -1 else len(text)]
    for decl in re.finditer(r"\b(input|output|inout)\b\s*(?:wire|reg|logic|signed|unsigned|\s)*(\[[^\]]*\])?\s*([^;]+);", body):
        for name in re.findall(r"[A-Za-z_]\w*", decl.group(3)):
            if name in ports:
                ports[name] = (decl.group(1), _width(decl.group(2)))
    for decl in re.finditer(r"\bparameter\b([^;]*);", body):
        params.update(_params(decl.group(1)))
    return {"params": params, "ports": ports}


def interface_mismatches(code: str, module: str, spec: dict) -> list[str]:
    """Differences between the generated module's interface and the spec
    shown to the user: missing/extra ports, directions, widths, parameters."""
    found = module_interface(code, module)
    if found is None:
        return [f"No module named {module} in the file."]
    problems = []
    want = {p["name"]: p for p in spec.get("ports", [])}
    for name, port in want.items():
        if name not in found["ports"]:
            problems.append(f"Port {name} from the interface spec is missing.")
            continue
        direction, width = found["ports"][name]
        if direction and direction != port.get("direction"):
            problems.append(f"Port {name} is {direction}, the spec says {port.get('direction')}.")
        spec_width = str(port.get("width", "1")).replace(" ", "")
        spec_width = _width(spec_width) if spec_width.startswith("[") else spec_width
        if width and width != spec_width:
            problems.append(f"Port {name} is {width} bit(s) wide, the spec says {spec_width}.")
    for extra in sorted(set(found["ports"]) - set(want)):
        problems.append(f"Port {extra} is not in the interface spec.")
    for param in spec.get("parameters", []):
        name = param["name"]
        if name not in found["params"]:
            problems.append(f"Parameter {name} from the interface spec is missing.")
            continue
        default = re.sub(r"\s+", "", str(param.get("default", "")))
        if default and found["params"][name] != default:
            problems.append(f"Parameter {name} defaults to {found['params'][name]}, the spec says {default}.")
    for extra in sorted(set(found["params"]) - {p["name"] for p in spec.get("parameters", [])}):
        problems.append(f"Parameter {extra} is not in the interface spec (use a localparam for internal constants).")
    return problems


# ----------------------------------------------------------------- checks

def check_new_rtl(path: Path, module: str, connect: list[Path], compile_check, spec: dict | None = None) -> dict:
    """Interface vs spec, lint (Verilator) and compile (iverilog) of a
    generated file in the temp copy. Nothing is simulated. Returns {"ok",
    "compile", "lint", "interface", "errors"}: errors are fed back to the
    model; ok needs a clean compile, a lint run without errors and the
    interface the spec promised."""
    code = path.read_text(encoding="utf-8")
    if risky := sorted(access_features(code)):
        # No generated RTL may reach files/processes, nor `include anything.
        return {"ok": False, "compile": "not run", "lint": "not run", "lint_ran": False, "interface": "not checked",
                "unsafe": risky,
                "errors": [f"The module must not use {', '.join(risky[:4])} (no file/process access, no `include)."]}
    if compile_check is not None and (reason := compile_check([*connect, path])):
        return {"ok": False, "blocked": reason, "compile": "blocked", "lint": "blocked", "lint_ran": False,
                "interface": "not checked", "errors": [reason]}
    errors: list[str] = []
    std = "-g2012" if path.suffix.lower() == ".sv" else "-g2005"
    with tempfile.TemporaryDirectory(prefix="copilot_gen_") as d:
        try:
            res = subprocess.run(["iverilog", std, "-s", module, "-o", str(Path(d) / "out"),
                                  *[f"-I{p.parent}" for p in connect], *map(str, connect), str(path)],
                                 capture_output=True, text=True, timeout=60, cwd=d)
            compiled = res.returncode == 0
            errors += [l.strip() for l in (res.stdout + res.stderr).splitlines() if l.strip()][:30]
        except subprocess.TimeoutExpired:
            compiled = False
            errors.append("iverilog: compilation timed out")
    # The connected modules' own files are read explicitly: their file names
    # need not match the module names, and one file may define several.
    lint = lint_checker(str(path), top=module, extra_files=[str(p) for p in connect])
    if lint.get("status") in ("clean", "issues_found"):
        lint_errors = lint.get("errors", [])
        lint_view = {"status": lint["status"], "errors": lint_errors[:20],
                     "warnings": [w for w in lint.get("warnings", []) if not w.startswith("%Error")][:20]}
        errors += lint_errors[:30]
    else:
        lint_view = {"status": lint.get("status", "error"), "message": lint.get("message") or lint.get("stderr", "")[:300]}
    mismatches = interface_mismatches(code, module, spec) if spec else []
    errors += mismatches
    lint_ran = lint_view["status"] in ("clean", "issues_found")
    return {"ok": compiled and lint_ran and not lint_view.get("errors") and not mismatches,
            "compile": "ok" if compiled else "failed", "lint": lint_view, "lint_ran": lint_ran,
            "interface": "matches the spec" if spec and not mismatches else ("mismatch" if mismatches else "not checked"),
            "errors": errors}


def _ask_code(messages: list[dict], key: str) -> tuple[str, str]:
    try:
        reply = json.loads(json_completion(get_client(), messages, max_tokens=24000, thinking=True) or "{}")
    except Exception:  # noqa: BLE001 - cut off / malformed: one cheaper retry
        reply = json.loads(json_completion(get_client(), messages, max_tokens=12000) or "{}")
    code = reply.get(key) if isinstance(reply, dict) else None
    if not isinstance(code, str) or not code.strip():
        raise ValueError(f"the reply has no {key!r} text")
    return code.replace("\r\n", "\n").rstrip() + "\n", str(reply.get("notes") or "")


def _table(rounds: list[dict]) -> str:
    rows = ["| Round | Change | Compile | Lint | Errors fed back |", "|---|---|---|---|---|"]
    for r in rounds:
        lint = r["lint"] if isinstance(r["lint"], str) else (
            f"{len(r['lint'].get('errors', []))} error(s), {len(r['lint'].get('warnings', []))} warning(s)"
            if "errors" in r["lint"] else r["lint"].get("status"))
        errs = "; ".join(r["errors"][:2]).replace("|", "\\|") or "-"
        rows.append(f"| {r['round']} | {r['change']} | {r['compile']} | {lint} | {errs[:160]} |")
    return "\n".join(rows)


# ------------------------------------------------------------------ skill

def _inputs(requirements: str, spec_path: str, module_name: str, target_path: str, repo_root: str,
            connect_to) -> tuple[str, Path, Path, list[Path]]:
    """Validated (requirements text, repo root, target path, connect files);
    raises ValueError / NewFilePathError with a user-facing message."""
    if not _IDENT_RE.match(str(module_name or "")):
        raise ValueError("module_name must be a Verilog identifier.")
    root = Path(repo_root).resolve() if repo_root else None
    if root is None or not root.is_dir():
        raise ValueError("repo_root must be an existing folder (the project repo the file goes into).")
    target = check_new_file_path(str(root), target_path)
    if target.suffix.lower() not in (".v", ".sv"):
        raise ValueError("The new file must end in .v (Verilog-2005) or .sv.")
    text = str(requirements or "").strip()
    if spec_path:
        spec_file = Path(spec_path).resolve()
        if not spec_file.is_file():
            raise ValueError(f"No spec file at '{spec_path}'.")
        text += "\n\nSpec file " + spec_file.name + ":\n" + spec_file.read_text(encoding="utf-8", errors="replace")[:_SPEC_MAX_CHARS]
    if not text.strip():
        raise ValueError("Give the requirements (text or spec_path).")
    connect = []
    for c in connect_to or []:
        p = (Path(c) if Path(c).is_absolute() else root / c).resolve()
        if not p.is_file() or not _inside(p, root):
            raise ValueError(f"connect_to file '{c}' is not a file inside the repo root.")
        connect.append(p)
    return text, root, target, connect


def rtl_rounds(text: str, module: str, root: Path, target: Path, connect: list[Path], spec: dict,
               style: dict, proj: Path, compile_check) -> tuple[str, list[dict], dict]:
    """Generate the module and check it in the temp copy `proj` (a copy of
    root), feeding errors back, at most _MAX_ROUNDS model calls. Returns
    (code, rounds, last check)."""
    language, rule = _LANGUAGE[target.suffix.lower()]
    t_target = proj / target.relative_to(root)
    t_connect = [proj / c.relative_to(root) for c in connect]
    messages = [
        {"role": "system", "content": _RTL_PROMPT.format(language=language, language_rule=rule, module=module)},
        {"role": "user", "content": (f"Interface spec:\n{json.dumps(spec, indent=1)}\n\nRequirements:\n{text}\n\n"
                                     f"Repository style facts: {json.dumps(style['facts'])}\n"
                                     + (f"Style sample ({style['facts']['sample_files'][0]}):\n{style['excerpt']}\n"
                                        if style["excerpt"] else ""))},
    ]
    rounds, code, check = [], "", {}
    for rnd in range(1, _MAX_ROUNDS + 1):
        code, notes = _ask_code(messages, "verilog")
        t_target.parent.mkdir(parents=True, exist_ok=True)
        if not _inside(t_target, proj):
            raise RuntimeError("refusing to write outside the temp copy")
        t_target.write_text(code, encoding="utf-8")
        check = check_new_rtl(t_target, module, t_connect, compile_check, spec)
        rounds.append({"round": rnd, "change": "first version" if rnd == 1 else "fixed the reported errors",
                       "compile": check["compile"], "lint": check["lint"], "interface": check.get("interface"),
                       "errors": [e.replace(str(proj), ".") for e in check["errors"]], "notes": notes})
        # Done when it checks out, or when only lint is missing (can't run here;
        # retrying wouldn't change that, so it is reported instead).
        if check["ok"] or check.get("blocked") or (
                check["compile"] == "ok" and not check["lint_ran"] and check["interface"] != "mismatch"):
            break
        messages += [{"role": "assistant", "content": json.dumps({"verilog": code})},
                     {"role": "user", "content": "Your file failed these checks (iverilog compile, Verilator "
                      "lint, interface vs the spec):\n" + "\n".join(rounds[-1]["errors"])
                      + "\nReturn the complete corrected file."}]
    return code, rounds, check


def generate_rtl(
    requirements: str,
    module_name: str,
    target_path: str,
    repo_root: str,
    spec_path: str = "",
    connect_to: list[str] | None = None,
    compile_check: Callable[[list[Path]], str | None] | None = None,
) -> dict:
    """Creates a NEW RTL module from requirements: interface spec +
    assumptions, then the module, linted and compiled in a temp copy of the
    repo (errors fed back, max 3 rounds), staged as a new-file diff for
    apply_diff. Writes nothing to the repo; the temp copy is always deleted."""
    try:
        text, root, target, connect = _inputs(requirements, spec_path, module_name, target_path, repo_root, connect_to)
    except (ValueError, NewFilePathError, OSError) as exc:
        return {"status": "error", "message": str(exc)}
    if not (_tool_available("iverilog")):
        return {"status": "unavailable", "message": "iverilog not found on PATH; the generated module can't be checked."}
    style = style_sample(root, target, testbench=False)

    with count_model_calls() as calls:
        tmp = Path(tempfile.mkdtemp(prefix="copilot_generate_")).resolve()
        try:
            try:
                spec = draft_interface_spec(text, module_name, style["facts"], connect)
            except Exception as exc:  # noqa: BLE001 - no spec, no module
                return {"status": "error", "message": f"Could not draft the interface spec ({type(exc).__name__}: {exc})."}
            _copy_project(root, tmp / "project")
            try:
                code, rounds, check = rtl_rounds(text, module_name, root, target, connect, spec, style,
                                                 tmp / "project", compile_check)
            except Exception as exc:  # noqa: BLE001 - report, clean up below
                return {"status": "error", "spec": spec, "assumptions": spec["assumptions"],
                        "message": f"Generation stopped ({type(exc).__name__}: {exc})."}
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
            removed = not tmp.exists()
        n_calls = calls[0]

    if check.get("blocked") or check.get("unsafe"):
        # Never stage code that reaches files/processes, nor code the check refused.
        return {"status": "blocked", "message": check.get("blocked") or check["errors"][0], "spec": spec,
                "assumptions": spec["assumptions"], "rounds": rounds, "rounds_table": _table(rounds),
                "model_calls": n_calls, "temp_copy_removed": removed, "pending_diff": None}
    rel = target.relative_to(root).as_posix()
    diff_id = stage_pending([(str(target), code)], creates={str(target)}, repo_root=str(root))
    if check["ok"]:
        status = "passed"
    elif check["compile"] == "ok" and not check["lint_ran"] and check["interface"] != "mismatch":
        status = "compiled_not_linted"
    else:
        status = "still_failing"
    note = (f"Nothing was written: {rel} is a proposed NEW file, pending diff {diff_id}. Only apply_diff creates it, "
            "after showing it and getting the user's explicit yes.")
    if status == "compiled_not_linted":
        note = ("It compiles and matches the spec, but lint could not run (verification incomplete: "
                f"{check['lint'].get('status')}). " + note)
    elif status != "passed":
        note = f"The module still fails its checks after {len(rounds)} round(s); review before deciding. " + note
    return {
        "status": status,
        "module": module_name,
        "file": rel,
        "spec": {k: spec.get(k) for k in ("summary", "parameters", "ports", "clock", "reset", "latency", "behavior")},
        "assumptions": spec["assumptions"],
        "style": style["facts"],
        "rounds": rounds,
        "rounds_table": _table(rounds),
        "lint": check["lint"],
        "compile": check["compile"],
        "interface": check["interface"],
        "verification": "complete" if check["ok"] else "partial",
        "diff": unified_diff_text("", code, str(target), new_file=True),
        "pending_diff": {"diff_id": diff_id, "files": [rel]},
        "model_calls": n_calls,
        "temp_copy_removed": removed,
        "note": note,
    }


SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "generate_rtl",
            "description": (
                "Create a NEW Verilog module from plain-English requirements: drafts an interface spec (ports, "
                "widths, parameters, reset, latency) and lists its assumptions, writes the module in the repo's "
                "style, lints and compiles it in a temp copy (errors fed back, max 3 rounds) and stages it as a "
                "new-file diff for apply_diff. Never overwrites a file (use modify_module) and writes nothing "
                "itself; show the spec, assumptions and diff and ask before apply_diff."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "requirements": {"type": "string", "description": "What the module must do, in plain English."},
                    "module_name": {"type": "string"},
                    "target_path": {"type": "string", "description": "New file path, relative to repo_root (e.g. rtl/fifo.v)."},
                    "repo_root": {"type": "string", "description": "The project repo the file goes into; nothing is written outside it."},
                    "spec_path": {"type": "string", "description": "Optional spec file with more requirements."},
                    "connect_to": {"type": "array", "items": {"type": "string"},
                                   "description": "Optional existing RTL files whose modules it must connect to."},
                },
                "required": ["requirements", "module_name", "target_path", "repo_root"],
            },
        },
    },
]

IMPLS = {"generate_rtl": generate_rtl}
