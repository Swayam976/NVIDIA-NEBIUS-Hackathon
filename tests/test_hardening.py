"""Regression tests for the whole-copilot Codex review (offline, mocked LLM):
schema-only tool arguments, terminal-safe diff preview, project-name
confinement, web repo map, bounded simulator output, missing data never a
pass, and the per-request model-call budget. Simulator cases are skipped
when iverilog/vvp are not on PATH.
"""

import io
import json
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("NEBIUS_API_KEY", "test-key-not-real")

from src.copilot import cli, llm, memory, sandbox  # noqa: E402
from src.copilot.config import MEMORY_DIR, settings  # noqa: E402
from src.copilot.tools import module_modifier, verification  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def tool_call(name, args, i=0):
    return types.SimpleNamespace(id=f"c{i}", function=types.SimpleNamespace(name=name, arguments=json.dumps(args)))


def message(content=None, calls=None):
    m = types.SimpleNamespace(content=content, tool_calls=calls)
    m.model_dump = lambda **kw: {"role": "assistant", "content": content}
    return m


def scripted_client(turns, seen):
    turns = list(turns)

    def create(**kw):
        seen.append(kw)
        msg = turns.pop(0) if turns else message("done")
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg, finish_reason="stop")])

    return types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))


SCHEMA = [{"type": "function", "function": {"name": "probe", "parameters": {
    "type": "object", "properties": {"tb_path": {"type": "string"}}}}}]

# --- 1. only schema-declared arguments reach a tool ---
got, seen = [], []
turns = [message(calls=[tool_call("probe", {"tb_path": "t.v", "vcd_out": "rtl/design.v", "compile_check": None})])]
with mock.patch.object(llm, "get_client", return_value=scripted_client(turns, seen)):
    llm.run_agent_loop("go", [], SCHEMA, {"probe": lambda **kw: got.append(kw) or {"status": "ok"}})
assert got == [{"tb_path": "t.v"}], got
tool_msg = json.loads(next(m for m in seen[1]["messages"] if m.get("role") == "tool")["content"])
assert tool_msg["ignored_arguments"] == ["compile_check", "vcd_out"], tool_msg
print("agent loop: undeclared (internal) tool arguments dropped and reported: OK")

# --- 3. terminal control characters in a diff are shown, not interpreted ---
evil = "+ok\n\x1b[1A\x1b[2K\r+hidden‮\n"
assert cli.visible(evil) == "+ok\n\\x1b[1A\\x1b[2K\\x0d+hidden\\u202e\n", repr(cli.visible(evil))
out = io.StringIO()
with mock.patch.object(cli, "preview_pending_diff", return_value=(evil, "fp")), \
     mock.patch("builtins.input", return_value="n"), mock.patch("sys.stdout", new=out):
    assert cli.confirm_tool_call("apply_diff", {"diff_id": "x"}) is False
assert "\x1b" not in out.getvalue() and "\r" not in out.getvalue() and "\\x1b[2K" in out.getvalue()
print("CLI: ANSI/CR/bidi characters in the diff preview made visible: OK")

# --- 4. project names can't leave the memory folder ---
for bad in ("../../README", "..\\x", "riscv-core/../../x", "", "Riscv"):
    try:
        memory.append_decision(bad, "x")
        raise AssertionError(f"accepted project name {bad!r}")
    except memory.ProjectNotFound:
        pass
assert "riscv-core" in memory.list_projects() and memory.get_status("riscv-core")
print("memory: project names are slugs; paths outside the memory folder refused: OK")

# --- 5. the web demo sees only its own sample repo ---
ws = sandbox.create_workspace(MEMORY_DIR, REPO / "demo" / "sample")
try:
    before = dict(settings.project_repo_paths)
    with sandbox.activate(ws):
        assert settings.project_repo_paths == {"sample-alu": str(ws.root / "rtl")}, settings.project_repo_paths
    assert settings.project_repo_paths == before, "restored after the session"
finally:
    shutil.rmtree(ws.root, ignore_errors=True)
print("web demo: project repo map confined to the workspace, restored after: OK")

# --- 7. one request's model calls are capped, nested tool calls included ---
with llm.count_model_calls() as outer:
    with llm.count_model_calls() as inner:
        llm.note_model_call()
    llm.note_model_call()
assert (outer[0], inner[0]) == (2, 1), (outer, inner)
seen = []
loop_forever = [message(calls=[tool_call("probe", {"tb_path": "t.v"}, i)]) for i in range(10)]


def probe_that_calls_model(**kw):
    llm.note_model_call()  # a tool's own model call (e.g. modify_module)
    return {"status": "ok"}


with mock.patch.object(llm, "get_client", return_value=scripted_client(loop_forever, seen)):
    reply, _ = llm.run_agent_loop("go", [], SCHEMA, {"probe": probe_that_calls_model}, max_model_calls=5)
assert "budget" in reply and len(seen) == 3, (reply, len(seen))  # 3 agent turns + 2 tool calls = 5
assert all(kw["max_tokens"] == 16000 for kw in seen)
print("agent loop: model-call budget covers tool calls; output tokens bounded: OK")

# --- edits cut off at the output limit propose nothing ---
with tempfile.TemporaryDirectory() as d:
    f = Path(d) / "m.v"
    f.write_text("module m; endmodule\n", encoding="utf-8")
    cut = types.SimpleNamespace(content='{"new_content": "module', )
    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(
        create=lambda **kw: types.SimpleNamespace(choices=[types.SimpleNamespace(message=cut, finish_reason="length")]))))
    with mock.patch.object(module_modifier, "get_client", return_value=client):
        r = module_modifier.propose_edit(str(f), "x")
    assert r["status"] == "error" and "cut off" in r["message"], r
print("modify_module: an edit cut off by the output limit proposes nothing: OK")

# --- review round 2 ---
# A last line without a newline gets its own record + marker, never merged.
with tempfile.TemporaryDirectory() as d:
    f = Path(d) / "m.v"
    f.write_text("module m;\nendmodule", encoding="utf-8")
    module_modifier._PENDING_DIFFS_PATH = Path(d) / "pending.json"
    diff_id = module_modifier.stage_pending([(str(f), "module m;\nendmodule // edited")])
    diff_text, _ = module_modifier.preview_pending_diff(diff_id)
    assert "-endmodule\n\\ No newline at end of file\n+endmodule // edited\n\\ No newline at end of file\n" in diff_text
    module_modifier._PENDING_DIFFS_PATH = REPO / ".copilot_pending_diffs.json"
# The web approval panel escapes control/bidi characters too.
ws = sandbox.create_workspace(MEMORY_DIR, REPO / "demo" / "sample")
try:
    shown = sandbox.display_diff(ws, "--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n+b‮/* hidden */\x1b[2K\n")
    assert "‮" not in shown and "\x1b" not in shown and "+b\\u202e/* hidden */\\x1b[2K" in shown, repr(shown)
finally:
    shutil.rmtree(ws.root, ignore_errors=True)
# Every skill completion has an output bound (here: next_step_suggester).
from src.copilot.tools import workflow  # noqa: E402
seen = []
with mock.patch.object(workflow, "get_client", return_value=scripted_client([message("Do X next.")], seen)):
    workflow.next_step_suggester("riscv-core")
assert seen and seen[0]["max_tokens"] == 16000, seen
print("review round 2: no-newline markers, web panel escapes controls, skill completions bounded: OK")

# --- 6 and 8 need a simulator ---
if not (shutil.which("iverilog") and shutil.which("vvp")):
    print("simulator cases: SKIPPED (iverilog/vvp not on PATH)")
else:
    with tempfile.TemporaryDirectory() as d:
        d = Path(d)
        (d / "dut.v").write_text("module dut; endmodule\n", encoding="utf-8")
        (d / "chatty_tb.v").write_text("module chatty_tb; dut u(); initial forever begin $display(\"PASS spam spam "
                                       "spam spam spam spam spam spam\"); #1; end endmodule\n", encoding="utf-8")
        with mock.patch.object(verification, "_LOG_MAX_BYTES", 64 * 1024):
            r = verification.testbench_runner(module_path=str(d / "dut.v"), tb_path=str(d / "chatty_tb.v"), timeout_s=60)
        assert r["status"] == "output_limit" and "printed more than" in r["message"], r
        (d / "mem_tb.v").write_text("module mem_tb; dut u(); reg [7:0] m [0:1];\n  initial begin "
                                    "$readmemh(\"nope.hex\", m); $display(\"PASS\"); $finish; end\nendmodule\n",
                                    encoding="utf-8")
        r = verification.testbench_runner(module_path=str(d / "dut.v"), tb_path=str(d / "mem_tb.v"))
        assert r["status"] == "missing_data_file" and r["pass_lines"] == 1, r
        (d / "mem_tb.v").write_text((d / "mem_tb.v").read_text().replace("nope.hex", "program data.hex"),
                                    encoding="utf-8")
        r = verification.testbench_runner(module_path=str(d / "dut.v"), tb_path=str(d / "mem_tb.v"))
        assert r["status"] == "missing_data_file" and r["missing_data_files"] == ["program data.hex"], r
    print("testbench_runner: endless output stopped (output_limit); PASS with a missing data file is not a pass: OK")

print("\nHARDENING TESTS PASSED")
