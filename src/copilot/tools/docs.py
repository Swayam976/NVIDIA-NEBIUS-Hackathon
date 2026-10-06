"""Skills: isa_spec_cross_referencer, spec_drafting_assistant,
changelog_generator.
"""

from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

from .. import memory
from ..config import settings
from ..llm import get_client, json_completion
from .rtl_files import HDL_EXTS, instance_connections, is_testbench, module_interfaces, project_files, project_identifiers

# Matches mnemonic-looking tokens: e.g. ADD, ADDI, LW, custom.foo
_MNEMONIC_RE = re.compile(r"\b[A-Z][A-Z0-9_.]{1,15}\b")


def _extract_mnemonics(text: str) -> set[str]:
    # Filter out common false positives (acronyms that aren't instructions)
    ignore = {"ISA", "RISC", "CPU", "ALU", "PC", "RTL", "FIFO", "DMA", "APB", "GEMM", "NPU", "TODO"}
    return {m for m in _MNEMONIC_RE.findall(text) if m not in ignore}


# RV32I base ISA: (mnemonic, opcode, funct3, funct7); None = not part of the encoding.
RV32I = [
    ("LUI", "0110111", None, None), ("AUIPC", "0010111", None, None),
    ("JAL", "1101111", None, None), ("JALR", "1100111", "000", None),
    ("BEQ", "1100011", "000", None), ("BNE", "1100011", "001", None), ("BLT", "1100011", "100", None),
    ("BGE", "1100011", "101", None), ("BLTU", "1100011", "110", None), ("BGEU", "1100011", "111", None),
    ("LB", "0000011", "000", None), ("LH", "0000011", "001", None), ("LW", "0000011", "010", None),
    ("LBU", "0000011", "100", None), ("LHU", "0000011", "101", None),
    ("SB", "0100011", "000", None), ("SH", "0100011", "001", None), ("SW", "0100011", "010", None),
    ("ADDI", "0010011", "000", None), ("SLTI", "0010011", "010", None), ("SLTIU", "0010011", "011", None),
    ("XORI", "0010011", "100", None), ("ORI", "0010011", "110", None), ("ANDI", "0010011", "111", None),
    ("SLLI", "0010011", "001", "0000000"), ("SRLI", "0010011", "101", "0000000"), ("SRAI", "0010011", "101", "0100000"),
    ("ADD", "0110011", "000", "0000000"), ("SUB", "0110011", "000", "0100000"), ("SLL", "0110011", "001", "0000000"),
    ("SLT", "0110011", "010", "0000000"), ("SLTU", "0110011", "011", "0000000"), ("XOR", "0110011", "100", "0000000"),
    ("SRL", "0110011", "101", "0000000"), ("SRA", "0110011", "101", "0100000"), ("OR", "0110011", "110", "0000000"),
    ("AND", "0110011", "111", "0000000"),
    ("FENCE", "0001111", "000", None), ("ECALL", "1110011", "000", None), ("EBREAK", "1110011", "000", None),
]
_RV32I_MODES = ("", "rv32i", "builtin:rv32i")
_RV32I_BUDGET = 16000  # characters of RTL sent to the model
_RV32I_PER_FILE = 7000  # cap per file, so one big file can't crowd out the decoder
_LITERAL_RE = re.compile(r"(\d+)\s*'\s*([bBhHdD])\s*([0-9a-fA-F_]+)")
_CONTEXT_FILE_RE = re.compile(r"alu|decod|control|ctrl|data_mem|load|store|lsu|imm|branch|flush", re.IGNORECASE)
_RV32I_PROMPT = """\
You check which RV32I base instructions a Verilog CPU implements, judging only from the RTL given. \
An instruction counts as implemented only if its encoding (opcode, funct3, funct7) is decoded AND the \
datapath can perform it (e.g. LB/LBU need byte selection and sign/zero extension; SRA vs SRL needs \
funct7 bit 5; BLTU needs an unsigned compare). Decoding by bit tests (opcode[5], funct3[2]) counts. \
Each instruction notes whether its opcode value appears as a literal in the RTL; no literal usually \
means it is missing, unless the RTL decodes that opcode some other way. \
Respond with ONLY a JSON object: {"implemented": ["ADD", ...], "missing": [{"name": "FENCE", \
"evidence": "<file: reason>"}], "unclear": [{"name": "...", "evidence": "..."}]}. Put every listed \
instruction in exactly one list, by its bare mnemonic. Keep each evidence under 15 words, with no \
quote marks, braces or brackets.
"""


def _opcode_literals(text: str) -> set[str]:
    """7-bit opcode values written as sized literals (7'b0110011, 7'h33, 7'd51),
    plus 5-bit opcode[6:2] literals when the RTL slices [6:2]."""
    found = set()
    slices_6_2 = bool(re.search(r"\[\s*6\s*:\s*2\s*\]", text))
    for width, base, digits in _LITERAL_RE.findall(text):
        digits = digits.replace("_", "")
        try:
            value = int(digits, {"b": 2, "h": 16, "d": 10}[base.lower()])
        except ValueError:
            continue
        if width == "7":
            found.add(f"{value:07b}")
        elif width == "5" and slices_6_2:
            found.add(f"{value:05b}11")
    return found


def _mnemonic(raw) -> str:
    """Bare mnemonic from what the model wrote: "LB", "lb", "LB: opcode=...",
    "LB (load byte)", or an object with a name/instruction/mnemonic key."""
    if isinstance(raw, dict):
        raw = raw.get("name") or raw.get("instruction") or raw.get("mnemonic") or ""
    match = re.match(r"\s*([A-Za-z]+)", str(raw))
    return match.group(1).upper() if match else ""


def _parse_rv32i_review(review, wanted: set[str]) -> dict[str, dict]:
    """{"implemented": [...], "missing": [{name, evidence}], "unclear": [...]}
    -> {NAME: {verdict, evidence}} for wanted names; first mention wins."""
    out: dict[str, dict] = {}
    if not isinstance(review, dict):
        return out
    for verdict in ("implemented", "missing", "unclear"):
        entries = review.get(verdict)
        for entry in entries if isinstance(entries, list) else []:
            name = _mnemonic(entry)
            if name in wanted and name not in out:
                evidence = str(entry.get("evidence", "")) if isinstance(entry, dict) else ""
                out[name] = {"verdict": verdict, "evidence": evidence[:200]}
    return out


def _rv32i_check(rtl_directory: Path) -> dict:
    files = [f for f in project_files(rtl_directory, HDL_EXTS) if not is_testbench(f)]
    if not files:
        return {"status": "error", "message": f"No Verilog sources under {rtl_directory}"}
    texts = {}
    for f in files:
        raw = f.read_text(encoding="utf-8", errors="ignore")
        texts[f] = "\n".join(l.rstrip() for l in re.sub(r"//[^\n]*|/\*.*?\*/", "", raw, flags=re.S).splitlines() if l.strip())
    literals = {f: _opcode_literals(t) for f, t in texts.items()}
    opcodes = set().union(*literals.values())

    # RTL context for the model: decode-relevant files first. An oversized file
    # is truncated rather than dropped, and both cases are reported.
    ranked = sorted(files, key=lambda f: (not literals[f], not _CONTEXT_FILE_RE.search(f.stem), f.name))
    context, used, truncated, omitted = [], 0, [], []
    for f in ranked:
        body = texts[f]
        if len(body) > _RV32I_PER_FILE:
            body = body[:_RV32I_PER_FILE] + "\n// ... (truncated)"
            truncated.append(f)
        block = f"// FILE: {f.name}\n{body}"
        if used + len(block) > _RV32I_BUDGET:
            omitted.append(f)
            continue
        context.append(block)
        used += len(block)
    # Any cut or omitted file could hold decode logic (bit-test decoding needs no literals).
    incomplete = sorted({f.name for f in truncated + omitted})
    opcode_of = {n: o for n, o, _, _ in RV32I}

    # Every instruction goes to the model. Whether its opcode appears as a
    # literal is a hint, not a verdict: bit-test decoding is legal Verilog.
    def ask(names: list[str]) -> dict[str, dict]:
        listing = "\n".join(
            f"{n}: opcode={o}" + (f" funct3={f3}" if f3 else "") + (f" funct7={f7}" if f7 else "")
            + (" (opcode literal found in RTL)" if o in opcodes else " (no opcode literal found in RTL)")
            for n, o, f3, f7 in RV32I if n in names
        )
        content = json_completion(get_client(), [
            {"role": "system", "content": _RV32I_PROMPT},
            {"role": "user", "content": f"Instructions to check:\n{listing}\n\nRTL:\n" + "\n\n".join(context)},
        ])
        return _parse_rv32i_review(json.loads(content or "{}"), set(names))

    order = [i[0] for i in RV32I]
    verdicts: dict[str, dict] = {}
    model_error = None
    try:
        verdicts.update(ask(order))
        # The model occasionally writes a broken string that swallows later
        # entries (valid JSON, fewer items). Ask once more for what's absent.
        if absent := [n for n in order if n not in verdicts]:
            verdicts.update(ask(absent))
    except Exception as exc:  # noqa: BLE001 - report what we know instead
        model_error = type(exc).__name__

    # The model may not have seen the code that decodes an instruction. "Missing"
    # still stands when the opcode has no literal in ANY file: the literal scan
    # reads all RTL, not just what fit in the prompt, so both signals agree.
    if incomplete:
        shown = ", ".join(incomplete[:6]) + (" ..." if len(incomplete) > 6 else "")
        for name, v in verdicts.items():
            if v["verdict"] == "missing" and opcode_of[name] in opcodes:
                verdicts[name] = {"verdict": "unclear", "evidence": f"{v['evidence']} [RTL only partly shown to the model: {shown}]"}
    for name, opcode, _, _ in RV32I:
        if name not in verdicts:
            hint = "no opcode literal found in RTL; " if opcode not in opcodes else ""
            reason = f"model review unavailable ({model_error})" if model_error else "not assessed by the model"
            verdicts[name] = {"verdict": "unclear", "evidence": hint + reason}

    by = {v: [n for n in order if verdicts[n]["verdict"] == v] for v in ("implemented", "missing", "unclear")}
    return {
        "status": "ok",
        "mode": "rv32i",
        "summary": f"{len(by['implemented'])} of {len(order)} RV32I instructions implemented, "
        f"{len(by['missing'])} missing, {len(by['unclear'])} unclear.",
        **by,
        "details": {n: verdicts[n] for n in order if verdicts[n]["verdict"] != "implemented"},
        "rtl_files_read": [f.name for f in files][:40],
        "context_truncated": [f.name for f in truncated],
        "context_omitted": [f.name for f in omitted][:40],
        "note": "Verdicts are a model judgment from the RTL, guided by a scan for opcode literals. "
        "Confirm with a testbench.",
    }


def isa_spec_cross_referencer(rtl_dir: str, spec_path: str = "") -> dict:
    """Checks an ISA against the RTL. With no spec_path (or 'rv32i') it
    checks the RTL against the built-in RV32I base instruction list: one
    model call judges each instruction from the decoder/datapath RTL, told
    which opcodes appear as literals. With a spec file it diffs mnemonics
    mentioned in the spec against those in the RTL (text heuristic).
    """
    rtl_directory = Path(rtl_dir)
    if not rtl_directory.is_dir():
        return {"status": "error", "message": f"No RTL directory at {rtl_dir}"}
    if spec_path.strip().lower() in _RV32I_MODES:
        return _rv32i_check(rtl_directory)

    spec_file = Path(spec_path)
    if not spec_file.is_file():
        return {"status": "error", "message": f"No spec file at {spec_path}"}

    spec_mnemonics = _extract_mnemonics(spec_file.read_text(encoding="utf-8"))

    rtl_text = ""
    for ext in ("*.v", "*.sv", "*.vh"):
        for f in rtl_directory.rglob(ext):
            rtl_text += f.read_text(encoding="utf-8", errors="ignore") + "\n"
    rtl_mnemonics = _extract_mnemonics(rtl_text)

    spec_only = sorted(spec_mnemonics - rtl_mnemonics)
    rtl_only = sorted(rtl_mnemonics - spec_mnemonics)

    return {
        "status": "ok",
        "defined_but_not_implemented": spec_only,
        "implemented_but_not_in_spec": rtl_only,
        "note": "Heuristic text match — review before treating as ground truth.",
    }


def spec_drafting_assistant(project: str, section_hint: str) -> dict:
    """Drafts spec/README text for a project section, grounded in that
    project's status from memory, the real module interfaces from its RTL and
    the instance connections between modules. Afterwards every module/signal
    name the draft uses is checked against the RTL; names that don't exist
    there are returned as unverified_names.
    """
    status = memory.get_status(project)
    repo = settings.project_repo_paths.get(project)
    root = Path(repo) if repo and Path(repo).is_dir() else None
    interfaces, modules, wiring, wiring_lines = "", [], "", 0
    if root:
        interfaces, modules = module_interfaces(root, hint=section_hint)
        wiring, wiring_lines = instance_connections(root, hint=section_hint)
    if interfaces:
        rtl_block = f"RTL module interfaces (from the project's source files):\n{interfaces}"
        if wiring:
            rtl_block += f"\n\nRTL instance connections (parent: module instance (.port(signal), ...)):\n{wiring}"
    else:
        rtl_block = "No RTL source is available for this project, so do not name any modules or signals."
    client = get_client()
    response = client.chat.completions.create(
        model=settings.nebius_model,
        messages=[
            {
                "role": "system",
                "content": "You draft concise, technically precise hardware design spec/README sections. "
                "Match the terse, factual tone of an engineer's own notes, not marketing copy. "
                "Name only modules and signals that appear in the RTL you are given, and describe connections "
                "between modules only as the instance connections show them. Put module and signal names in "
                "backticks. If a detail is not supported by the status or the RTL, write 'TBD' instead of inventing it.",
            },
            {
                "role": "user",
                "content": f"Project: {project}\nCurrent status:\n{status}\n\n{rtl_block}\n\n"
                f"Draft the following section: {section_hint}",
            },
        ],
    )
    draft = response.choices[0].message.content or ""
    if not draft.strip():
        return {"status": "error", "message": "The model returned no draft text (output cut off); try again."}

    names = _draft_names(draft)  # every name is checked; only the display is capped
    if root and modules:
        known = project_identifiers(root)  # case-sensitive, like Verilog
        unverified = [n for n in names if n not in known]
        shown = ", ".join(unverified[:25]) + (f" (+{len(unverified) - 25} more)" if len(unverified) > 25 else "")
        note = (
            f"These names in the draft don't appear in the project's RTL (case-sensitive): {shown}. "
            "Correct them or mark them TBD before using the draft."
            if unverified
            else f"All {len(names)} module/signal names in the draft exist in the RTL; still check anything marked TBD."
        )
    else:
        unverified = names
        shown = ", ".join(names[:25]) + (f" (+{len(names) - 25} more)" if len(names) > 25 else "")
        note = "No RTL found for this project; draft is based on the memory status only" + (
            f", and its names could not be checked: {shown}." if names else "."
        )
    return {
        "status": "ok",
        "draft": draft,
        "grounded_on_modules": modules,
        "wiring_lines_given": wiring_lines,
        "names_checked": len(names),
        "unverified_names": unverified[:25],
        "unverified_count": len(unverified),
        "note": note,
    }


_VERILOG_WORDS = frozenset(
    """always and assign begin buf case casex casez default defparam disable else end endcase endfunction
    endgenerate endmodule endtask event for force forever function generate genvar if initial inout input
    integer localparam logic module nand negedge nor not or output parameter posedge real realtime reg
    release repeat signed supply0 supply1 task time tri unsigned wand while wire wor xor""".split()
)
_CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")
_SIZED_LITERAL_RE = re.compile(r"\d*\s*'\s*[sS]?[bBoOdDhH]\s*[0-9a-fA-FxXzZ_?]+")
_SNAKE_RE = re.compile(r"(?<![\w.])[a-z][a-z0-9]*(?:_[a-z0-9]+)+(?![\w])")


def _draft_names(draft: str) -> list[str]:
    """Identifier-like names a draft uses: everything inside `code spans`
    (sized literals and file extensions stripped) plus snake_case words in
    the prose. Verilog keywords and TBD are ignored; single-letter names
    (`a`, `q`) are checked too, since file extensions and sized literals are
    stripped before splitting."""
    names: set[str] = set()
    for span in _CODE_SPAN_RE.findall(draft):
        span = _SIZED_LITERAL_RE.sub(" ", span)
        span = re.sub(r"\.(?:s?v|vh|svh|xpr|mem|hex)\b", " ", span)
        names.update(re.findall(r"[A-Za-z_]\w*", span))
    names.update(_SNAKE_RE.findall(_CODE_SPAN_RE.sub(" ", draft)))
    return sorted(
        n for n in names
        if n.lower() not in _VERILOG_WORDS and n.lower() not in ("tbd", "todo")
    )


def changelog_generator(repo_path: str, since: str = "1.week") -> dict:
    """Turns recent git history into a human-readable changelog entry."""
    try:
        res = subprocess.run(
            ["git", "-C", repo_path, "log", f"--since={since}", "--pretty=format:%h %ad %s", "--date=short"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
    except FileNotFoundError:
        return {"status": "unavailable", "message": "git not found on PATH."}
    except subprocess.CalledProcessError as exc:
        return {"status": "error", "message": exc.stderr.strip()}

    commits = [line for line in res.stdout.splitlines() if line.strip()]
    if not commits:
        return {"status": "ok", "entries": [], "message": f"No commits since {since}."}

    return {"status": "ok", "entries": commits, "count": len(commits)}


SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "isa_spec_cross_referencer",
            "description": "Check which ISA instructions the RTL in rtl_dir implements. Omit spec_path (or pass "
            "'rv32i') to check against the built-in RV32I base instruction list; pass a spec file path to diff "
            "that spec's mnemonics against the RTL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "rtl_dir": {"type": "string", "description": "Directory containing the CPU's Verilog sources."},
                    "spec_path": {"type": "string", "description": "Optional ISA spec file; omit for RV32I."},
                },
                "required": ["rtl_dir"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spec_drafting_assistant",
            "description": "Draft a spec/README section for a project, grounded in its current status.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "section_hint": {"type": "string", "description": "What section/topic to draft, e.g. 'overview of the accumulator design'."},
                },
                "required": ["project", "section_hint"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "changelog_generator",
            "description": "Summarize recent git history for a repo into a changelog.",
            "parameters": {
                "type": "object",
                "properties": {
                    "repo_path": {"type": "string"},
                    "since": {"type": "string", "description": "git --since value, e.g. '1.week', '3.days'."},
                },
                "required": ["repo_path"],
            },
        },
    },
]

IMPLS = {
    "isa_spec_cross_referencer": isa_spec_cross_referencer,
    "spec_drafting_assistant": spec_drafting_assistant,
    "changelog_generator": changelog_generator,
}
