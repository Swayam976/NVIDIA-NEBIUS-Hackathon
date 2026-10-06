"""Persistent memory store: one markdown file per project under
memory/projects/<name>.md, with Status / Decisions / Blockers sections.

This is intentionally simple — plain files, no database — so you can read,
diff, and version-control your copilot's memory the same way you do your
RTL. The structure each file follows:

    # <Project Title>

    ## Status
    <freeform current-state text>

    ## Decisions
    - YYYY-MM-DD: <decision> — <rationale>

    ## Blockers
    - <blocker text>
"""

from __future__ import annotations

import datetime as _dt
import re
from pathlib import Path

from .config import MEMORY_DIR

_SECTION_RE = re.compile(r"^## (Status|Decisions|Blockers)\s*$", re.MULTILINE)


class ProjectNotFound(Exception):
    pass


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def _path_for(project: str) -> Path:
    """The project's memory file. Only plain slugs (riscv-core) are accepted,
    so a name like "../../README" can never point outside MEMORY_DIR."""
    if not isinstance(project, str) or not _SLUG_RE.match(project):
        raise ProjectNotFound(f"'{project}' is not a project name (lowercase letters, digits and dashes).")
    path = MEMORY_DIR / f"{project}.md"
    if path.resolve().parent != MEMORY_DIR.resolve():
        raise ProjectNotFound(f"'{project}' is not a project name.")
    return path


def list_projects() -> list[str]:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    return sorted(p.stem for p in MEMORY_DIR.glob("*.md"))


def read_project(project: str) -> str:
    path = _path_for(project)
    if not path.exists():
        raise ProjectNotFound(f"No memory file for project '{project}' at {path}")
    return path.read_text(encoding="utf-8")


def _split_sections(content: str) -> dict[str, str]:
    """Splits a project file into {section_name: body_text}."""
    matches = list(_SECTION_RE.finditer(content))
    sections: dict[str, str] = {}
    for i, m in enumerate(matches):
        name = m.group(1)
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        sections[name] = content[start:end].strip("\n")
    return sections


def _rejoin(title_line: str, sections: dict[str, str]) -> str:
    parts = [title_line.rstrip(), ""]
    for name in ("Status", "Decisions", "Blockers"):
        parts.append(f"## {name}")
        body = sections.get(name, "").strip("\n")
        parts.append(body if body else "(none yet)")
        parts.append("")
    return "\n".join(parts).rstrip() + "\n"


def get_status(project: str) -> str:
    sections = _split_sections(read_project(project))
    return sections.get("Status", "").strip() or "(no status recorded)"


def get_blockers(project: str) -> list[str]:
    sections = _split_sections(read_project(project))
    body = sections.get("Blockers", "")
    return [line.lstrip("- ").strip() for line in body.splitlines() if line.strip().startswith("-")]


def update_status(project: str, new_status: str) -> None:
    content = read_project(project)
    title_line = content.splitlines()[0]
    sections = _split_sections(content)
    sections["Status"] = new_status.strip()
    _path_for(project).write_text(_rejoin(title_line, sections), encoding="utf-8")


def append_decision(project: str, decision: str, rationale: str = "", date: str | None = None) -> None:
    content = read_project(project)
    title_line = content.splitlines()[0]
    sections = _split_sections(content)
    date = date or _dt.date.today().isoformat()
    line = f"- {date}: {decision}"
    if rationale:
        line += f" — {rationale}"
    existing = sections.get("Decisions", "").strip("\n")
    sections["Decisions"] = (existing + "\n" + line).strip("\n") if existing and existing != "(none yet)" else line
    _path_for(project).write_text(_rejoin(title_line, sections), encoding="utf-8")


def add_blocker(project: str, blocker: str) -> None:
    content = read_project(project)
    title_line = content.splitlines()[0]
    sections = _split_sections(content)
    existing = sections.get("Blockers", "").strip("\n")
    line = f"- {blocker}"
    sections["Blockers"] = (existing + "\n" + line).strip("\n") if existing and existing != "(none yet)" else line
    _path_for(project).write_text(_rejoin(title_line, sections), encoding="utf-8")


def resolve_blocker(project: str, blocker_substring: str) -> bool:
    """Removes the first blocker line containing blocker_substring. Returns True if removed."""
    content = read_project(project)
    title_line = content.splitlines()[0]
    sections = _split_sections(content)
    lines = [l for l in sections.get("Blockers", "").splitlines() if l.strip()]
    kept, removed = [], False
    for line in lines:
        if not removed and blocker_substring.lower() in line.lower():
            removed = True
            continue
        kept.append(line)
    sections["Blockers"] = "\n".join(kept)
    _path_for(project).write_text(_rejoin(title_line, sections), encoding="utf-8")
    return removed
