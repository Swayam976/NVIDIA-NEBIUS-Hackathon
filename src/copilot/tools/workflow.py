"""Skills: commit_to_summary, regression_spotter, next_step_suggester,
daily_brief.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path

from .. import memory
from ..config import settings
from ..llm import get_client
from .rtl_files import HDL_EXTS, is_generated, is_testbench, project_files


def _git(repo_path: str, *args: str) -> tuple[list[str] | None, str | None]:
    """Runs git in repo_path. Returns (non-empty output lines, None) or
    (None, error message) — never hides a git failure as "no output"."""
    try:
        res = subprocess.run(["git", "-C", repo_path, *args], capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return None, "git not found on PATH."
    if res.returncode != 0:
        return None, res.stderr.strip() or f"git exited with code {res.returncode}"
    return [line for line in res.stdout.splitlines() if line.strip()], None


def _git_log(repo_path: str, since: str) -> tuple[list[str] | None, str | None]:
    return _git(repo_path, "log", f"--since={since}", "--pretty=format:%h %ad %s", "--date=short")


_SINCE_RE = re.compile(r"^\s*(\d+)[.\s_]*(minute|hour|day|week|month)s?(?:[.\s_]*ago)?\s*$", re.IGNORECASE)
_SINCE_UNITS = {"minute": 60, "hour": 3600, "day": 86400, "week": 7 * 86400, "month": 30 * 86400}


def _since_cutoff(since: str) -> float | None:
    """'3.days' / '2 weeks' / '12.hours.ago' -> epoch cutoff, else None."""
    m = _SINCE_RE.match(since)
    if not m:
        return None
    return time.time() - int(m.group(1)) * _SINCE_UNITS[m.group(2).lower()]


def commit_to_summary(repo_path: str, since: str = "1.week") -> dict:
    """Turns recent git history into a short plain-English summary of what
    actually got done, instead of a raw commit list.
    """
    if not Path(repo_path).exists():
        return {"status": "error", "message": f"No repo at {repo_path}"}

    commits, error = _git_log(repo_path, since)
    if error:
        return {"status": "error", "message": error}
    if not commits:
        return {"status": "ok", "summary": f"No commits since {since}.", "commit_count": 0}

    client = get_client()
    response = client.chat.completions.create(
        model=settings.nebius_model,
        messages=[
            {"role": "system", "content": "Summarize a git commit log into 3-5 plain-English bullet points of what actually got done. No fluff."},
            {"role": "user", "content": "\n".join(commits)},
        ],
    )
    return {"status": "ok", "summary": response.choices[0].message.content or "", "commit_count": len(commits)}


def regression_spotter(repo_path: str, since: str = "3.days") -> dict:
    """Flags recently changed RTL files that have a matching testbench on
    disk, so you know what's worth re-running before you assume it's fine.
    Uses git history; for folders that aren't git repos (e.g. Vivado
    projects) it falls back to file modification times.
    """
    root = Path(repo_path)
    if not root.exists():
        return {"status": "error", "message": f"No repo at {repo_path}"}

    lines, error = _git(repo_path, "log", f"--since={since}", "--name-only", "--pretty=format:")
    if error and "not a git repository" not in error.lower():
        return {"status": "error", "message": error}
    if error:  # not a git repo: changed = RTL sources modified within the window
        cutoff = _since_cutoff(since)
        if cutoff is None:
            return {"status": "error", "message": f"Not a git repository, and can't read since='{since}' "
                    "for the file-time fallback; use a form like '3.days' or '2.weeks'."}
        source = "file modification times (not a git repository)"
        changed = {
            p.relative_to(root).as_posix()
            for p in project_files(root, HDL_EXTS)
            if not is_testbench(p) and p.stat().st_mtime >= cutoff
        }
    else:
        source = "git history"
        changed = {
            line.strip() for line in lines
            if line.strip().endswith(HDL_EXTS) and not is_testbench(Path(line.strip())) and not is_generated(Path(line.strip()))
        }
    if not changed:
        return {"status": "ok", "flagged": [], "source": source, "message": f"No RTL changes since {since}."}

    all_files = {p.relative_to(root).as_posix() for p in project_files(root)}
    flagged = []
    for changed_file in sorted(changed):
        base = Path(changed_file).stem
        matches = sorted(
            f for f in all_files
            if base in Path(f).stem and f != changed_file and ("tb" in f.lower() or "test" in f.lower())
        )
        if matches:
            flagged.append({"changed_file": changed_file, "likely_tests": matches})

    return {"status": "ok", "flagged": flagged, "changed_count": len(changed), "source": source}


def next_step_suggester(project: str) -> dict:
    """Proposes a concrete next task for a project, based on its current
    status and blockers rather than a generic 'keep going'.
    """
    status = memory.get_status(project)
    blockers = memory.get_blockers(project)
    client = get_client()
    response = client.chat.completions.create(
        model=settings.nebius_model,
        messages=[
            {
                "role": "system",
                "content": "Suggest one concrete, specific next engineering task given a project's status "
                "and blockers. One or two sentences, no generic advice.",
            },
            {
                "role": "user",
                "content": f"Project: {project}\nStatus: {status}\nBlockers: {blockers or 'none recorded'}",
            },
        ],
    )
    return {"status": "ok", "suggestion": response.choices[0].message.content or ""}


def daily_brief(projects: list[str] | None = None) -> dict:
    """Rolls up status and blockers across tracked projects into one
    structured brief. Returns raw data for the model to narrate — kept
    LLM-free here so it still works without burning tokens if you just
    want a quick local check.
    """
    names = projects or memory.list_projects()
    brief = {}
    for name in names:
        try:
            brief[name] = {"status": memory.get_status(name), "blockers": memory.get_blockers(name)}
        except memory.ProjectNotFound:
            brief[name] = {"status": None, "blockers": None, "error": "project not found"}
    return {"status": "ok", "projects": brief}


SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "commit_to_summary",
            "description": "Summarize recent git commits into a plain-English 'what did I do' summary.",
            "parameters": {
                "type": "object",
                "properties": {"repo_path": {"type": "string"}, "since": {"type": "string"}},
                "required": ["repo_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "regression_spotter",
            "description": "Flag recently changed RTL files that have matching testbenches, worth re-running.",
            "parameters": {
                "type": "object",
                "properties": {"repo_path": {"type": "string"}, "since": {"type": "string"}},
                "required": ["repo_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "next_step_suggester",
            "description": "Suggest one concrete next task for a project based on its status and blockers.",
            "parameters": {
                "type": "object",
                "properties": {"project": {"type": "string"}},
                "required": ["project"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "daily_brief",
            "description": "Roll up status and blockers across tracked projects (or a given subset).",
            "parameters": {
                "type": "object",
                "properties": {
                    "projects": {"type": "array", "items": {"type": "string"}, "description": "Omit to cover all tracked projects."}
                },
            },
        },
    },
]

IMPLS = {
    "commit_to_summary": commit_to_summary,
    "regression_spotter": regression_spotter,
    "next_step_suggester": next_step_suggester,
    "daily_brief": daily_brief,
}
