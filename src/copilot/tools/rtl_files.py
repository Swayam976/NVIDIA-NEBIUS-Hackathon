"""Finding a project's own HDL sources, skipping tool-generated trees.

Vivado projects hold thousands of generated files (<name>.cache, .runs,
.gen, .sim, .ip_user_files, .hw, .Xil, xsim.dir); scanning or matching
against those is slow and noisy.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

HDL_EXTS = (".v", ".sv", ".vh", ".svh")
_GENERATED_DIR_SUFFIXES = (".cache", ".runs", ".gen", ".sim", ".ip_user_files", ".hw", ".xil")
_SKIP_DIRS = {".git", "xsim.dir", "__pycache__", "node_modules", "obj_dir"}


def is_skipped_dir(name: str) -> bool:
    """Tool-generated or VCS directory (pruned from every project scan)."""
    return name in _SKIP_DIRS or name.lower().endswith(_GENERATED_DIR_SUFFIXES)


def project_files(root: Path, exts: tuple[str, ...] | None = None) -> list[Path]:
    """Files under root, pruning generated/VCS directories."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not is_skipped_dir(d)]
        for name in filenames:
            if exts is None or name.lower().endswith(exts):
                found.append(Path(dirpath) / name)
    return sorted(found)


def is_generated(path: Path) -> bool:
    """True if any directory in path is a tool-generated/VCS tree."""
    return any(is_skipped_dir(p) for p in path.parts[:-1])


def is_testbench(path: Path) -> bool:
    stem = path.stem.lower()
    in_sim_dir = any(re.fullmatch(r"sim_\d+", part.lower()) for part in path.parts)
    return in_sim_dir or stem.endswith("_tb") or stem.startswith("tb_") or "test" in stem


_COMMENT_RE = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)
_DEF_RE = re.compile(r"\bmodule\s+([A-Za-z_]\w*)")


def strip_comments(text: str) -> str:
    return _COMMENT_RE.sub(" ", text)


# Strings and comments in ONE left-to-right pass, so a "//" inside a string
# can't swallow the code after it and a string can't look like code.
_CODE_NOISE_RE = re.compile(r'"(?:\\.|[^"\\\n])*"|//[^\n]*|/\*.*?\*/', re.DOTALL)


def _read_code(path: Path) -> str:
    """Code only (comments and string literals blanked), so a $display
    message can't look like a module definition or instantiation."""
    try:
        return _CODE_NOISE_RE.sub(" ", path.read_text(encoding="utf-8", errors="ignore"))
    except OSError:
        return ""


def _instance_re(module: str) -> re.Pattern:
    """`<module> [#(...)] <instance>[range] (` — a module instantiation."""
    return re.compile(
        rf"(?<![\w.$`]){re.escape(module)}\s*(?:#\s*\(|[A-Za-z_]\w*\s*(?:\[[^\]]*\]\s*)?\()"
    )


def _instantiates(body: str, module: str) -> bool:
    for m in _instance_re(module).finditer(body):
        if not re.search(r"\bmodule\s+$", body[: m.start()]):
            return True
    return False


def design_files(tb: Path, search: list[Path]) -> tuple[list[str], list[Path], dict[str, list[Path]]]:
    """Files needed to elaborate a testbench: start from the module(s) the
    testbench file defines and follow instantiations of modules defined in
    `search`. Returns (top modules, files with tb first, duplicates), where
    duplicates maps a needed module to the several files defining it."""
    texts = {tb: _read_code(tb)}
    defs: dict[str, list[Path]] = {}
    for f in [tb, *search]:
        if f not in texts:
            texts[f] = _read_code(f)
        for name in _DEF_RE.findall(texts[f]):
            if f not in defs.setdefault(name, []):
                defs[name].append(f)

    tb_modules = _DEF_RE.findall(texts[tb])
    # Roots = testbench-file modules that nothing in the testbench file instantiates.
    tops = [m for m in tb_modules if not _instantiates(texts[tb], m)] or tb_modules[:1]

    needed, queue, files = set(tb_modules), list(tb_modules), [tb]
    while queue:
        module = queue.pop(0)
        sources = defs.get(module, [])
        for f in sources:
            if f not in files:
                files.append(f)
        body = "\n".join(texts[f] for f in sources)
        for other in defs:
            if other not in needed and _instantiates(body, other):
                needed.add(other)
                queue.append(other)
    duplicates = {m: defs[m] for m in sorted(needed) if len(defs.get(m, [])) > 1}
    return tops, files, duplicates


def _balanced_end(text: str, i: int) -> int:
    """Index just past the ')' matching the '(' at text[i] (len(text) if unbalanced)."""
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "(":
            depth += 1
        elif text[j] == ")":
            depth -= 1
            if depth == 0:
                return j + 1
    return len(text)


_CONN_RE = re.compile(r"\.\s*([A-Za-z_]\w*)\s*\(")


def instance_connections(root: Path, hint: str = "", budget: int = 5000) -> tuple[str, int]:
    """One line per module instantiation in the project's RTL (testbenches
    excluded): `parent: child inst (.port(signal), ...)`, hint-relevant lines
    first, cut at `budget` characters. Returns (text, lines included)."""
    words = {w for w in re.findall(r"[a-z0-9_]+", hint.lower()) if len(w) > 2}
    texts = {f: _read_code(f) for f in project_files(root, HDL_EXTS) if not is_testbench(f)}
    modules = {name for t in texts.values() for name in _DEF_RE.findall(t)}
    entries = []
    for file_idx, text in enumerate(texts.values()):
        for dm in _DEF_RE.finditer(text):
            parent = dm.group(1)
            end = text.find("endmodule", dm.end())
            body = text[dm.end(): end if end >= 0 else len(text)]
            for child in modules - {parent}:
                for im in _instance_re(child).finditer(body):
                    i = im.start() + len(child)
                    rest = body[i:].lstrip()
                    i = len(body) - len(rest)
                    if rest.startswith("#"):  # skip the parameter override list
                        i = _balanced_end(body, body.index("(", i))
                    inst = re.match(r"\s*([A-Za-z_]\w*)\s*(?:\[[^\]]*\]\s*)?\(", body[i:])
                    if not inst:
                        continue
                    open_paren = i + inst.end() - 1
                    ports = body[open_paren + 1: _balanced_end(body, open_paren) - 1]
                    conns = []
                    for cm in _CONN_RE.finditer(ports):
                        close = _balanced_end(ports, cm.end() - 1)
                        conns.append(f".{cm.group(1)}({' '.join(ports[cm.end(): close - 1].split())})")
                    joined = ", ".join(conns) if conns else " ".join(ports.split())
                    line = f"{parent}: {child} {inst.group(1)} ({joined})"
                    entries.append((-sum(w in line.lower() for w in words), file_idx, dm.start(), im.start(), line))
    lines, used = [], 0
    for *_, line in sorted(entries):
        if used + len(line) > budget:
            continue
        lines.append(line)
        used += len(line) + 1
    return "\n".join(lines), len(lines)


def project_identifiers(root: Path) -> set[str]:
    """Every identifier in the project's HDL code, case kept (Verilog is
    case-sensitive): module, port, wire, reg, parameter and instance names.
    Comments and string literals are excluded (_read_code), so a word that
    only appears in a $display message doesn't count as a real name."""
    names: set[str] = set()
    for f in project_files(root, HDL_EXTS):
        names.update(re.findall(r"[A-Za-z_]\w*", _read_code(f)))
    return names


def find_module_files(name: str, search: list[Path]) -> list[Path]:
    """Every file in search that defines module `name`."""
    return [f for f in search if name in _DEF_RE.findall(_read_code(f))]
_MODULE_RE = re.compile(r"\bmodule\s+(\w+)\s*(?:#\s*\((?:[^()]|\([^()]*\))*\)\s*)?\(([^;]*?)\)\s*;", re.DOTALL)
_PORT_DECL_RE = re.compile(r"^\s*(?:input|output|inout)\b[^;]*;", re.MULTILINE)


def module_interfaces(root: Path, hint: str = "", budget: int = 8000) -> tuple[str, list[str]]:
    """One line per RTL module (`name(ports)`), testbenches excluded, most
    relevant to `hint` first, cut at `budget` characters. Returns
    (text, module names included)."""
    words = {w for w in re.findall(r"[a-z0-9_]+", hint.lower()) if len(w) > 2}
    entries = []
    for f in project_files(root, HDL_EXTS):
        if is_testbench(f):
            continue
        try:
            text = _COMMENT_RE.sub(" ", f.read_text(encoding="utf-8", errors="ignore"))
        except OSError:
            continue
        for m in _MODULE_RE.finditer(text):
            name, ports = m.group(1), " ".join(m.group(2).split())
            if not re.search(r"\b(input|output|inout)\b", ports):  # Verilog-1995 style ports
                # Keep only body declarations naming a header port, so task /
                # function arguments never show up as module ports.
                port_names = set(re.findall(r"[A-Za-z_]\w*", ports))
                body = text[m.end(): text.find("endmodule", m.end())]
                decls = [
                    d for d in _PORT_DECL_RE.findall(body)
                    if port_names & set(re.findall(r"[A-Za-z_]\w*", re.sub(r"\[[^\]]*\]", " ", d))[1:])
                ]
                ports = " ".join(" ".join(decls).split()) or ports
            line = f"{name}({ports})"
            score = sum(w in line.lower() for w in words)
            entries.append((-score, name, line))
    lines, names, used = [], [], 0
    for _, name, line in sorted(entries):
        if used + len(line) > budget:
            continue  # skip this one; smaller interfaces may still fit
        lines.append(line)
        names.append(name)
        used += len(line) + 1
    return "\n".join(lines), names
