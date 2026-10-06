"""Skills: project_state_tracker, decision_log, cross_project_linker."""

from __future__ import annotations

import re

from .. import memory


def project_state_tracker(project: str) -> dict:
    """Reports the current status and open blockers for a project."""
    status = memory.get_status(project)
    blockers = memory.get_blockers(project)
    return {"status": "ok", "project": project, "current_status": status, "blockers": blockers}


def decision_log(project: str, decision: str, rationale: str = "") -> dict:
    """Records a design decision with its rationale for later recall."""
    memory.append_decision(project, decision, rationale)
    return {"status": "ok", "message": f"Logged decision for '{project}'."}


# Words that carry no technical meaning in a status note.
_STOPWORDS = frozenset(
    """about after also available been before being built current currently does done each edit exist exists
    expected fail failed failure failures fill fixed from have here into just keep later local locally more most
    none note notes once only over plan planned plans project projects ready since some start started status
    still such than that their them then there these they this those under until update updated very were what
    when where which while will with work working yet github swayam""".split()
)
_WORD_RE = re.compile(r"[a-z][a-z0-9_]*(?:-[a-z0-9_]+)*")


def _terms(text: str) -> set[str]:
    """Identifier-aware words (keeps forward_unit, mxint8-gemm), no stopwords."""
    return {w for w in _WORD_RE.findall(text.lower()) if len(w) > 3 and w not in _STOPWORDS}


def cross_project_linker(project: str) -> dict:
    """Finds other tracked projects that share context with this one: an
    explicit mention of one project in the other's status (strongest), plus
    technical words both statuses use. Words every project's status uses
    are ignored, and a single shared word alone isn't treated as a link.
    """
    statuses = {name: memory.get_status(name) for name in memory.list_projects()}
    target = statuses[project] if project in statuses else memory.get_status(project)
    terms = {name: _terms(text) for name, text in statuses.items()}
    common = set.intersection(*terms.values()) if len(terms) >= 3 else set()
    target_terms = _terms(target) - common

    related = []
    for other, other_status in statuses.items():
        if other == project:
            continue
        mentions = other in target.lower() or project in other_status.lower()
        shared = sorted(target_terms & (terms[other] - common) - {project, other})
        if mentions or len(shared) >= 2:
            related.append({
                "project": other,
                "mentions": mentions,
                "shared_terms": shared[:12],
                "score": (3 if mentions else 0) + len(shared),
            })
    related.sort(key=lambda r: -r["score"])
    return {"status": "ok", "project": project, "related_projects": related}


SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "project_state_tracker",
            "description": "Get the current status and open blockers for a hardware project.",
            "parameters": {
                "type": "object",
                "properties": {"project": {"type": "string", "description": "Project slug, e.g. 'mxint8-gemm'."}},
                "required": ["project"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "decision_log",
            "description": "Record a design decision and why it was made, for a hardware project.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string"},
                    "decision": {"type": "string"},
                    "rationale": {"type": "string"},
                },
                "required": ["project", "decision"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cross_project_linker",
            "description": "Find other tracked projects related to the given one.",
            "parameters": {
                "type": "object",
                "properties": {"project": {"type": "string"}},
                "required": ["project"],
            },
        },
    },
]

IMPLS = {
    "project_state_tracker": project_state_tracker,
    "decision_log": decision_log,
    "cross_project_linker": cross_project_linker,
}
