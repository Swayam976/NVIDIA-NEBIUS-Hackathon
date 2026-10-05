"""Skills: project_state_tracker, decision_log, cross_project_linker."""

from __future__ import annotations

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


def cross_project_linker(project: str) -> dict:
    """Finds other tracked projects that share context with this one, by
    simple keyword overlap between their Status sections. Good enough for
    a hackathon MVP — swap for embeddings if you want it sharper later.
    """
    target_status = memory.get_status(project).lower()
    target_words = {w for w in target_status.split() if len(w) > 4}

    related = []
    for other in memory.list_projects():
        if other == project:
            continue
        other_status = memory.get_status(other).lower()
        other_words = {w for w in other_status.split() if len(w) > 4}
        overlap = target_words & other_words
        if overlap:
            related.append({"project": other, "shared_terms": sorted(overlap)})

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
