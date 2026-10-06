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


def project_files(root: Path, exts: tuple[str, ...] | None = None) -> list[Path]:
    """Files under root, pruning generated/VCS directories."""
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            d for d in dirnames
            if d not in _SKIP_DIRS and not d.lower().endswith(_GENERATED_DIR_SUFFIXES)
        ]
        for name in filenames:
            if exts is None or name.lower().endswith(exts):
                found.append(Path(dirpath) / name)
    return sorted(found)


def is_generated(path: Path) -> bool:
    """True if any directory in path is a tool-generated/VCS tree."""
    return any(p in _SKIP_DIRS or p.lower().endswith(_GENERATED_DIR_SUFFIXES) for p in path.parts[:-1])


def is_testbench(path: Path) -> bool:
    stem = path.stem.lower()
    in_sim_dir = any(re.fullmatch(r"sim_\d+", part.lower()) for part in path.parts)
    return in_sim_dir or stem.endswith("_tb") or stem.startswith("tb_") or "test" in stem


_COMMENT_RE = re.compile(r"//[^\n]*|/\*.*?\*/", re.DOTALL)
_MODULE_RE = re.compile(r"\bmodule\s+(\w+)\s*(?:#\s*\((?:[^()]|\([^()]*\))*\)\s*)?\(([^;]*?)\)\s*;", re.DOTALL)
_PORT_DECL_RE = re.compile(r"^\s*(?:input|output|inout)\b[^;]*;", re.MULTILINE)


def module_interfaces(root: Path, hint: str = "", budget: int = 8000) -> tuple[str, list[str]]:
    """One line per RTL module (`name(ports)`), testbenches excluded, most
    relevant to `hint` first, cut at `budget` characters. Returns
    (text, module names included)."""
    words = {w for w in re.findall(r"[a-z0-9]+", hint.lower()) if len(w) > 2}
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
