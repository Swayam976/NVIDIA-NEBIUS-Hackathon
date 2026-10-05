"""Skills: modify_module, apply_diff.

modify_module never writes to disk — it asks the model to produce a full
new version of the file, computes a unified diff against the original for
human review, and stashes both under a diff_id. apply_diff is the only
thing that writes, and the agent loop (see llm.py's _CONFIRM_REQUIRED) is
wired to always ask for confirmation before calling it.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import uuid
from pathlib import Path

from ..config import REPO_ROOT, settings
from ..llm import get_client

_PENDING_DIFFS_PATH = REPO_ROOT / ".copilot_pending_diffs.json"


def _load_pending() -> dict:
    if _PENDING_DIFFS_PATH.exists():
        return json.loads(_PENDING_DIFFS_PATH.read_text(encoding="utf-8"))
    return {}


def _save_pending(pending: dict) -> None:
    _PENDING_DIFFS_PATH.write_text(json.dumps(pending, indent=2), encoding="utf-8")


_GEN_SYSTEM_PROMPT = """\
You edit Verilog/SystemVerilog source files. You will be given the full \
current content of one file and an instruction describing the change to \
make. Respond with ONLY a JSON object of the form:
{"new_content": "<the full new file content>", "explanation": "<1-3 sentence summary of what changed and why>"}
Do not include markdown code fences. Preserve formatting and style you \
are not asked to change. If the instruction is ambiguous or you cannot \
safely make the change, set new_content to the original content unchanged \
and explain why in "explanation".
"""


def modify_module(module_path: str, instruction: str) -> dict:
    """Generates a proposed edit to a Verilog module as a reviewable diff.
    Does NOT write to disk — call apply_diff with the returned diff_id to
    actually commit it, after you've looked at the diff.
    """
    path = Path(module_path)
    if not path.exists():
        return {"status": "error", "message": f"No file at {module_path}"}

    original = path.read_text(encoding="utf-8")

    client = get_client()
    response = client.chat.completions.create(
        model=settings.nebius_model,
        messages=[
            {"role": "system", "content": _GEN_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": f"FILE: {module_path}\n\nCURRENT CONTENT:\n{original}\n\nINSTRUCTION:\n{instruction}",
            },
        ],
        response_format={"type": "json_object"},
    )
    try:
        payload = json.loads(response.choices[0].message.content or "{}")
        new_content = payload["new_content"]
        explanation = payload.get("explanation", "")
    except (json.JSONDecodeError, KeyError) as exc:
        return {"status": "error", "message": f"Model did not return valid edit JSON: {exc}"}

    diff_lines = list(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            new_content.splitlines(keepends=True),
            fromfile=f"a/{module_path}",
            tofile=f"b/{module_path}",
        )
    )
    diff_text = "".join(diff_lines) or "(no changes)"

    diff_id = uuid.uuid4().hex[:8]
    pending = _load_pending()
    pending[diff_id] = {"module_path": module_path, "new_content": new_content}
    _save_pending(pending)

    return {
        "status": "pending_review",
        "diff_id": diff_id,
        "diff": diff_text,
        "explanation": explanation,
        "note": "Nothing has been written to disk. Call apply_diff with this diff_id to commit it.",
    }


def _fingerprint(module_path: str, current: str, new_content: str) -> str:
    """Identifies exactly what an approval covers: target path, the file as
    it was when the diff was shown, and the content that would be written."""
    h = hashlib.sha256()
    for part in (module_path, current, new_content):
        h.update(part.encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


def _current_content(module_path: str) -> str:
    path = Path(module_path)
    return path.read_text(encoding="utf-8") if path.exists() else ""


def preview_pending_diff(diff_id: str) -> tuple[str, str] | None:
    """Returns (diff_text, fingerprint) for what apply_diff(diff_id) would
    write, computed against the file as it is on disk right now (so edits
    made since modify_module ran are reflected). None if there is no such
    pending diff. Read-only; used by the confirmation prompt, which passes
    the fingerprint to approve_pending_diff only after a human "y".
    """
    entry = _load_pending().get(diff_id)
    if entry is None:
        return None
    current = _current_content(entry["module_path"])
    diff = "".join(
        difflib.unified_diff(
            current.splitlines(keepends=True),
            entry["new_content"].splitlines(keepends=True),
            fromfile=f"a/{entry['module_path']}",
            tofile=f"b/{entry['module_path']}",
        )
    )
    return diff or "(no changes)", _fingerprint(entry["module_path"], current, entry["new_content"])


def approve_pending_diff(diff_id: str, fingerprint: str) -> bool:
    """Records a human approval for exactly the state shown in the preview.
    Called only by the confirmation prompt after an explicit "y" — it is not
    exposed as a tool, so the model can never approve its own diff.
    """
    pending = _load_pending()
    if diff_id not in pending:
        return False
    pending[diff_id]["approved_fingerprint"] = fingerprint
    _save_pending(pending)
    return True


def apply_diff(diff_id: str) -> dict:
    """Writes a previously generated diff to disk, but only if a human
    approved it (approve_pending_diff) and neither the target file nor the
    proposed content has changed since that approval was given.
    """
    pending = _load_pending()
    entry = pending.get(diff_id)
    if entry is None:
        return {"status": "error", "message": f"No pending diff with id '{diff_id}'. It may have already been applied."}

    approved = entry.pop("approved_fingerprint", None)
    current = _current_content(entry["module_path"])
    if approved is None or approved != _fingerprint(entry["module_path"], current, entry["new_content"]):
        _save_pending(pending)  # any stale approval is cleared; the diff stays pending for re-review
        return {
            "status": "declined",
            "message": "No valid human approval for this diff (missing, or the file/diff changed after it was "
            "shown). Nothing was written; it must be reviewed and approved again.",
        }

    try:
        Path(entry["module_path"]).write_text(entry["new_content"], encoding="utf-8")
    except OSError as exc:
        _save_pending(pending)  # keep the proposal (approval already cleared) so it can be retried
        return {"status": "error", "message": f"Write failed, nothing applied ({exc}). Diff {diff_id} is still pending."}

    del pending[diff_id]
    _save_pending(pending)
    return {"status": "ok", "message": f"Applied diff {diff_id} to {entry['module_path']}."}


SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "modify_module",
            "description": "Generate a reviewable diff for a requested change to a Verilog module. Does not write to disk.",
            "parameters": {
                "type": "object",
                "properties": {
                    "module_path": {"type": "string"},
                    "instruction": {"type": "string", "description": "Plain-language description of the desired change."},
                },
                "required": ["module_path", "instruction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_diff",
            "description": "Write a previously generated diff (by diff_id) to disk. Requires user approval.",
            "parameters": {
                "type": "object",
                "properties": {"diff_id": {"type": "string"}},
                "required": ["diff_id"],
            },
        },
    },
]

IMPLS = {
    "modify_module": modify_module,
    "apply_diff": apply_diff,
}
