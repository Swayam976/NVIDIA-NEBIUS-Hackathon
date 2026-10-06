"""Guardrails for the public web demo (demo/streamlit_app.py).

On a public demo the visitor is also the human who approves diffs, so the
apply_diff gate alone can't stop them from asking the copilot to write a
testbench that reads server files ($fopen, $readmemh, `include of
/proc/self/environ) and echoes them back — which would leak the API key.
This module adds the missing layers:

- a throwaway per-session Workspace; every tool path must resolve inside it
- project-name validation (memory paths are built from it)
- tool args filtered to each tool's schema (no smuggled timeout_s etc.)
- an HDL source check before any lint or simulation: raw-text directive
  allowlist (blocks every `include route, incl. macro-built ones) plus a
  system-task allowlist on the iverilog-preprocessed text
- a lock that swaps the memory/pending-diff globals per session, since the
  core modules keep those as module globals
"""

from __future__ import annotations

import datetime as _dt
import json
import re
import shutil
import subprocess
import tempfile
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from . import memory
from .config import settings
from .tools import TOOL_IMPLS, TOOL_SCHEMAS, module_modifier
from .tools.hdl_safety import (  # noqa: F401 - ALLOWED_SYSTEM_TASKS re-exported
    ALLOWED_DIRECTIVES, ALLOWED_SYSTEM_TASKS, DEFINE_RE, DIRECTIVE_RE, RESERVED_DIRECTIVES, SYSTASK_RE,
)

# ---------------------------------------------------------------- workspace


class SandboxError(Exception):
    pass


@dataclass(frozen=True)
class Workspace:
    root: Path

    @property
    def memory_dir(self) -> Path:
        return self.root / "memory"

    @property
    def pending_path(self) -> Path:
        return self.root / ".pending_diffs.json"

    def resolve(self, path: str) -> Path:
        """Resolves a tool-supplied path inside the workspace or raises.
        Relative paths are taken relative to the workspace root."""
        root = self.root.resolve()
        p = Path(path)
        candidate = (p if p.is_absolute() else root / p).resolve()
        if candidate != root and not candidate.is_relative_to(root):
            raise SandboxError(f"Path '{path}' is outside the demo workspace.")
        # Hidden files hold internal state (the pending-diff store); tools never touch them.
        if any(part.startswith(".") for part in candidate.relative_to(root).parts):
            raise SandboxError(f"Path '{path}' is not accessible in the demo.")
        return candidate

    def scrub(self, value):
        """Replaces the workspace's absolute path with "." in strings/dicts/lists."""
        root = str(self.root.resolve())
        needles = {root, root.replace("\\", "/"), str(self.root), str(self.root).replace("\\", "/")}
        return _scrub(value, sorted(needles, key=len, reverse=True))

    def files(self) -> list[str]:
        return sorted(
            str(p.relative_to(self.root)).replace("\\", "/")
            for p in self.root.rglob("*")
            if p.is_file() and not p.name.startswith(".")
        )


_WORKSPACE_PREFIX = "hwcopilot_ws_"


def create_workspace(memory_template: Path, sample_dir: Path, max_age_s: int = 24 * 3600) -> Workspace:
    """Fresh workspace: memory/*.md from the repo + the sample design."""
    _prune_old_workspaces(max_age_s)
    root = Path(tempfile.mkdtemp(prefix=_WORKSPACE_PREFIX))
    (root / "memory").mkdir()
    for md in memory_template.glob("*.md"):
        shutil.copy2(md, root / "memory" / md.name)
    for md in (sample_dir / "memory").glob("*.md"):
        shutil.copy2(md, root / "memory" / md.name)
    shutil.copytree(sample_dir / "rtl", root / "rtl")
    return Workspace(root)


def _prune_old_workspaces(max_age_s: int) -> None:
    cutoff = time.time() - max_age_s
    for d in Path(tempfile.gettempdir()).glob(f"{_WORKSPACE_PREFIX}*"):
        try:
            if d.is_dir() and d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            pass


# ------------------------------------------------------------ globals lock

_GLOBALS_LOCK = threading.Lock()


@contextmanager
def activate(ws: Workspace) -> Iterator[None]:
    """Points the memory store, the pending-diff store and the project repo
    map at this session's workspace for the duration of the block.
    Serialises sessions: the core modules keep these as module globals."""
    with _GLOBALS_LOCK:
        saved = (memory.MEMORY_DIR, module_modifier._PENDING_DIFFS_PATH)
        repos = settings.project_repo_paths  # a dict on a frozen dataclass: swap its contents
        saved_repos = dict(repos)
        memory.MEMORY_DIR = ws.memory_dir
        module_modifier._PENDING_DIFFS_PATH = ws.pending_path
        # Only the workspace's own sample design; never a checkout configured on the server.
        repos.clear()
        repos["sample-alu"] = str(ws.root / "rtl")
        try:
            yield
        finally:
            memory.MEMORY_DIR, module_modifier._PENDING_DIFFS_PATH = saved
            repos.clear()
            repos.update(saved_repos)


# ------------------------------------------------------------- HDL checks

# The allowlists live in tools/hdl_safety.py (shared with verify_loop).
_ALLOWED_DIRECTIVES = ALLOWED_DIRECTIVES
_RESERVED_DIRECTIVES = RESERVED_DIRECTIVES
_DEFINE_RE, _DIRECTIVE_RE, _SYSTASK_RE = DEFINE_RE, DIRECTIVE_RE, SYSTASK_RE


def check_hdl_sources(paths: list[Path], timeout_s: int = 20) -> str | None:
    """Returns None if the sources are safe to lint/simulate, else a reason.
    Fails closed: if iverilog isn't available the check refuses.

    Deliberately scans the text *without* stripping comments or strings:
    a stripping pass can be fooled (e.g. `$display("//"); $fopen(...)`).
    A task or directive merely mentioned in a comment/string is refused
    too, an acceptable false positive for the demo."""
    raw_parts = []
    for p in paths:
        try:
            raw_parts.append(p.read_text(encoding="utf-8", errors="replace"))
        except OSError as exc:
            return f"Could not read {p.name}: {exc}"
    raw = "\n".join(raw_parts).replace("\\\r\n", " ").replace("\\\n", " ")

    if "``" in raw or '`"' in raw or "`\\" in raw:
        return "Macro token pasting/stringification isn't allowed in the demo."
    defined = set(_DEFINE_RE.findall(raw))
    if shadowed := defined & _RESERVED_DIRECTIVES:
        return f"A macro can't be named after a compiler directive (`{sorted(shadowed)[0]})."
    for name in _DIRECTIVE_RE.findall(raw):
        if name not in _ALLOWED_DIRECTIVES and name not in defined:
            return f"Compiler directive `{name} isn't allowed in the demo (no `include or file access)."
    # Order-independent: a system task in the raw text is refused even inside
    # an `ifdef branch that this check's file order happens to disable, and a
    # task name can't be assembled from a macro ($`NAME).
    if re.search(r"\$\s*`", raw):
        return "System task names built from macros aren't allowed in the demo."
    for task in _SYSTASK_RE.findall(raw):
        if task not in ALLOWED_SYSTEM_TASKS:
            return f"System task ${task} isn't allowed in the demo (no file or process access)."

    if shutil.which("iverilog") is None:
        return "iverilog is not installed, so sources can't be safety-checked."
    with tempfile.TemporaryDirectory(prefix="hwcopilot_pp_") as tmp:
        out = Path(tmp) / "pp.v"
        res = subprocess.run(
            ["iverilog", "-E", "-o", str(out), *map(str, paths)],
            capture_output=True, text=True, timeout=timeout_s, cwd=tmp,
        )
        if res.returncode != 0 or not out.exists():
            return "Preprocessing failed; fix the source first."
        expanded = out.read_text(encoding="utf-8", errors="replace")
    for task in _SYSTASK_RE.findall(expanded):
        if task not in ALLOWED_SYSTEM_TASKS:
            return f"System task ${task} isn't allowed in the demo (no file or process access)."
    return None


# ------------------------------------------------------------ guarded tools

_PATH_ARGS = {"module_path", "tb_path", "file_path", "vcd_path", "spec_path", "rtl_dir", "repo_path", "project_dir"}
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_HDL_CHECKED = {"lint_checker": ("file_path",)}  # testbench_runner: whole workspace, see below
# Never offered to the model in the demo; applying happens only via the
# approval panel (approve_pending_diff + apply_diff on a human click).
_WEB_EXCLUDED = {"apply_diff"}


def _scrub(value, needles: list[str]):
    if isinstance(value, str):
        for n in needles:
            value = value.replace(n, ".")
        return value
    if isinstance(value, dict):
        return {k: _scrub(v, needles) for k, v in value.items()}
    if isinstance(value, list):
        return [_scrub(v, needles) for v in value]
    return value


def guarded_tools(ws: Workspace) -> tuple[list[dict], dict[str, Callable[..., dict]]]:
    """Tool schemas + impls for the web demo, confined to `ws`."""
    schemas =[s for s in TOOL_SCHEMAS if s["function"]["name"] not in _WEB_EXCLUDED]
    allowed_args = {s["function"]["name"]: set(s["function"]["parameters"].get("properties", {})) for s in schemas}

    def wrap(name: str, fn: Callable[..., dict]) -> Callable[..., dict]:
        def guarded(**kwargs) -> dict:
            try:
                kwargs = {k: v for k, v in kwargs.items() if k in allowed_args[name]}
                for k in _PATH_ARGS & kwargs.keys():
                    # spec_path "" means no spec file; "rv32i" selects the built-in
                    # RV32I list, but only isa_spec_cross_referencer knows that word.
                    spec_word = str(kwargs[k]).strip().lower()
                    if k == "spec_path" and (spec_word == "" or (
                            name == "isa_spec_cross_referencer" and spec_word in ("rv32i", "builtin:rv32i"))):
                        continue
                    kwargs[k] = str(ws.resolve(str(kwargs[k])))
                if "project" in kwargs and not _SLUG_RE.match(str(kwargs["project"])):
                    raise SandboxError(f"Unknown project '{kwargs['project']}'.")
                for p in kwargs.get("projects") or []:
                    if not _SLUG_RE.match(str(p)):
                        raise SandboxError(f"Unknown project '{p}'.")
                if name in ("testbench_runner", "debug_failing_test", "verify_loop"):  # all simulate
                    # The runner may compile any HDL file it can reach (rtl_dir, files next
                    # to the testbench), so every HDL file in the workspace must pass.
                    hdl = sorted(p for p in ws.root.rglob("*") if p.is_file() and p.suffix.lower() in (".v", ".sv", ".vh", ".svh"))
                    reason = check_hdl_sources(hdl) if hdl else None
                    if reason:
                        return {"status": "blocked", "message": reason}
                    # ...and the exact compiler inputs, in compile order, before every
                    # compile attempt (macro state depends on file order).
                    kwargs["compile_check"] = check_hdl_sources
                elif name in _HDL_CHECKED:
                    reason = check_hdl_sources([Path(kwargs[k]) for k in _HDL_CHECKED[name] if k in kwargs])
                    if reason:
                        return {"status": "blocked", "message": reason}
                result = fn(**kwargs)
            except SandboxError as exc:
                return {"status": "error", "message": str(exc)}
            return ws.scrub(json.loads(json.dumps(result, default=str)))

        return guarded

    impls = {name: wrap(name, fn) for name, fn in TOOL_IMPLS.items() if name not in _WEB_EXCLUDED}
    return schemas, impls


_HUNK_RE = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")


def display_diff(ws: Workspace, diff_text: str) -> str:
    """Hides the server path in each file's ---/+++ header only (a diff can
    cover several files). Hunk bodies are walked by their line counts and
    shown byte-for-byte, so a body line that merely looks like a header is
    never altered: the approval covers exactly this content."""
    lines = diff_text.splitlines(keepends=True)
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        hunk = _HUNK_RE.match(line)
        out.append(ws.scrub(line) if not hunk and line.startswith(("--- ", "+++ ")) else line)
        i += 1
        if hunk:
            old = int(hunk.group(1)) if hunk.group(1) is not None else 1
            new = int(hunk.group(2)) if hunk.group(2) is not None else 1
            while i < len(lines) and (old > 0 or new > 0):
                body = lines[i]
                out.append(body)
                i += 1
                if body.startswith("-"):
                    old -= 1
                elif body.startswith("+"):
                    new -= 1
                elif not body.startswith("\\"):
                    old, new = old - 1, new - 1
    return "".join(out)


def pending_diff_ids(ws: Workspace) -> list[str]:
    """Pending diffs whose target is inside the workspace; anything else is
    dropped unpreviewed (the panel must never read a file outside it).
    Call inside activate(ws)."""
    pending = module_modifier._load_pending()
    safe = {}
    for diff_id, entry in pending.items():
        try:
            paths = module_modifier.pending_paths(entry)
            if not paths:
                continue
            for p in paths:  # every file of a multi-file change must be inside
                ws.resolve(str(p))
        except (SandboxError, KeyError, TypeError):
            continue
        safe[diff_id] = entry
    if safe.keys() != pending.keys():
        module_modifier._save_pending(safe)
    return list(safe)


# ------------------------------------------------------------ spend limits


class DailyCounter:
    """Process-wide cap on agent turns per UTC day (protects API credits)."""

    def __init__(self, limit: int):
        self.limit = limit
        self._lock = threading.Lock()
        self._day = ""
        self._count = 0

    def try_take(self) -> bool:
        today = _dt.datetime.now(_dt.timezone.utc).date().isoformat()
        with self._lock:
            if today != self._day:
                self._day, self._count = today, 0
            if self._count >= self.limit:
                return False
            self._count += 1
            return True
