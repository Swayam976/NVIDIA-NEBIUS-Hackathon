"""Not a hackathon deliverable — just a local check that the plumbing
holds together before relying on it. Mocks the Nebius client so it runs
with no network access and no API key.
"""

import atexit
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("NEBIUS_API_KEY", "test-key-not-real")

from src.copilot import memory  # noqa: E402
from src.copilot.tools import TOOL_IMPLS, TOOL_SCHEMAS  # noqa: E402

# Run against a throwaway copy of memory/projects/ so the write round-trips
# below never touch the real project files. atexit also fires on a failed
# assert, so the copy is cleaned up either way.
_tmp_root = Path(tempfile.mkdtemp(prefix="copilot_smoke_"))
atexit.register(shutil.rmtree, _tmp_root, ignore_errors=True)
_tmp_memory = _tmp_root / "projects"
shutil.copytree(memory.MEMORY_DIR, _tmp_memory)
memory.MEMORY_DIR = _tmp_memory

print(f"Loaded {len(TOOL_SCHEMAS)} tool schemas, {len(TOOL_IMPLS)} implementations.")
assert len(TOOL_SCHEMAS) == 18, f"expected 18 skills, found {len(TOOL_SCHEMAS)}"

# --- memory.py round-trip ---
projects = memory.list_projects()
print(f"Tracked projects: {projects}")
assert "mxint8-gemm" in projects

status_before = memory.get_status("mxint8-gemm")
memory.append_decision("mxint8-gemm", "Use 2-level accumulator", "keeps partial sums in local SRAM banks")
content_after = memory.read_project("mxint8-gemm")
assert "Use 2-level accumulator" in content_after
print("decision_log round-trip: OK")

memory.add_blocker("mxint8-gemm", "Accumulator width still unverified")
assert "Accumulator width still unverified" in memory.get_blockers("mxint8-gemm")
removed = memory.resolve_blocker("mxint8-gemm", "Accumulator width")
assert removed and "Accumulator width still unverified" not in memory.get_blockers("mxint8-gemm")
print("add_blocker / resolve_blocker round-trip: OK")

# --- project_state_tracker / cross_project_linker tool calls ---
result = TOOL_IMPLS["project_state_tracker"](project="mxint8-gemm")
assert result["status"] == "ok"
print("project_state_tracker: OK ->", result["current_status"][:60], "...")

linked = TOOL_IMPLS["cross_project_linker"](project="mxint8-gemm")
print("cross_project_linker: OK ->", linked["related_projects"])

# --- daily_brief (pure local, no LLM) ---
brief = TOOL_IMPLS["daily_brief"]()
assert set(brief["projects"].keys()) == set(projects)
print("daily_brief: OK, covers", list(brief["projects"].keys()))

# --- hazard_sanity_checker heuristic ---
fake_diff = "+  PC <= PC + 4;\n+  // no stall check here\n"
hazard_result = TOOL_IMPLS["hazard_sanity_checker"](diff_text=fake_diff, llm_review=False)  # no network here
print("hazard_sanity_checker: OK ->", hazard_result["status"], hazard_result["flags"])

# --- agent loop with a mocked LLM client: simulate one tool call then a final answer ---
from src.copilot import llm as llm_module  # noqa: E402

call_count = {"n": 0}


def fake_create(**kwargs):
    call_count["n"] += 1
    if call_count["n"] == 1:
        # First turn: model decides to call project_state_tracker
        tool_call = types.SimpleNamespace(
            id="call_1",
            function=types.SimpleNamespace(name="project_state_tracker", arguments='{"project": "mxint8-gemm"}'),
        )
        msg = types.SimpleNamespace(content=None, tool_calls=[tool_call])
        msg.model_dump = lambda exclude_none=True: {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "project_state_tracker", "arguments": '{"project": "mxint8-gemm"}'}}],
        }
    else:
        # Second turn: model gives a final text answer
        msg = types.SimpleNamespace(content="You're mid-way on the GEMM accumulator.", tool_calls=None)
        msg.model_dump = lambda exclude_none=True: {"role": "assistant", "content": msg.content}
    choice = types.SimpleNamespace(message=msg)
    return types.SimpleNamespace(choices=[choice])


fake_client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=fake_create)))

with mock.patch.object(llm_module, "get_client", return_value=fake_client):
    reply, history = llm_module.run_agent_loop(
        user_message="Where am I on the GEMM accelerator?",
        history=[],
        tool_schemas=TOOL_SCHEMAS,
        tool_impls=TOOL_IMPLS,
        confirm_tool_call=lambda name, args: True,
    )

assert "accumulator" in reply.lower()
assert call_count["n"] == 2
print("agent loop (mocked LLM) end-to-end: OK ->", reply)

# --- confirm_tool_call gate: declining apply_diff should short-circuit the write ---
call_count["n"] = 0


def fake_create_apply_diff(**kwargs):
    call_count["n"] += 1
    if call_count["n"] == 1:
        tool_call = types.SimpleNamespace(
            id="call_1",
            function=types.SimpleNamespace(name="apply_diff", arguments='{"diff_id": "deadbeef"}'),
        )
        msg = types.SimpleNamespace(content=None, tool_calls=[tool_call])
        msg.model_dump = lambda exclude_none=True: {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "call_1", "type": "function", "function": {"name": "apply_diff", "arguments": '{"diff_id": "deadbeef"}'}}],
        }
    else:
        msg = types.SimpleNamespace(content="Okay, I did not apply the change.", tool_calls=None)
        msg.model_dump = lambda exclude_none=True: {"role": "assistant", "content": msg.content}
    choice = types.SimpleNamespace(message=msg)
    return types.SimpleNamespace(choices=[choice])


fake_client2 = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=fake_create_apply_diff)))

with mock.patch.object(llm_module, "get_client", return_value=fake_client2):
    reply2, _ = llm_module.run_agent_loop(
        user_message="Apply that change.",
        history=[],
        tool_schemas=TOOL_SCHEMAS,
        tool_impls=TOOL_IMPLS,
        confirm_tool_call=lambda name, args: False,  # simulate user saying "no"
    )

print("confirm_tool_call=False correctly short-circuits apply_diff: OK ->", reply2)

print("\nALL SMOKE TESTS PASSED")
