"""Skills: isa_spec_cross_referencer, spec_drafting_assistant,
changelog_generator.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

from .. import memory
from ..config import settings
from ..llm import get_client

# Matches mnemonic-looking tokens: e.g. ADD, ADDI, LW, custom.foo
_MNEMONIC_RE = re.compile(r"\b[A-Z][A-Z0-9_.]{1,15}\b")


def _extract_mnemonics(text: str) -> set[str]:
    # Filter out common false positives (acronyms that aren't instructions)
    ignore = {"ISA", "RISC", "CPU", "ALU", "PC", "RTL", "FIFO", "DMA", "APB", "GEMM", "NPU", "TODO"}
    return {m for m in _MNEMONIC_RE.findall(text) if m not in ignore}


def isa_spec_cross_referencer(spec_path: str, rtl_dir: str) -> dict:
    """Diffs instruction mnemonics mentioned in the ISA spec against those
    that appear in the RTL, flagging spec-only and RTL-only names. This is
    a text-level heuristic — confirm anything it flags before trusting it.
    """
    spec_file = Path(spec_path)
    rtl_directory = Path(rtl_dir)
    if not spec_file.exists():
        return {"status": "error", "message": f"No spec file at {spec_path}"}
    if not rtl_directory.exists():
        return {"status": "error", "message": f"No RTL directory at {rtl_dir}"}

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
    project's current status from memory.
    """
    status = memory.get_status(project)
    client = get_client()
    response = client.chat.completions.create(
        model=settings.nebius_model,
        messages=[
            {
                "role": "system",
                "content": "You draft concise, technically precise hardware design spec/README sections. "
                "Match the terse, factual tone of an engineer's own notes, not marketing copy.",
            },
            {
                "role": "user",
                "content": f"Project: {project}\nCurrent status:\n{status}\n\n"
                f"Draft the following section: {section_hint}",
            },
        ],
    )
    return {"status": "ok", "draft": response.choices[0].message.content or ""}


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
            "description": "Diff ISA spec mnemonics against what's actually implemented in the RTL.",
            "parameters": {
                "type": "object",
                "properties": {"spec_path": {"type": "string"}, "rtl_dir": {"type": "string"}},
                "required": ["spec_path", "rtl_dir"],
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
