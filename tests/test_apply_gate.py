"""The apply_diff approval gate, end to end through the agent loop with a
mocked LLM. Proves: no confirm handler -> never writes (fail closed); a "no"
-> never writes; the CLI prompt prints the real pending diff before asking;
an unknown diff_id is declined without prompting; only a typed "y" writes.

Uses a temp target file and temp pending-diff store, no network.
"""

import io
import os
import shutil
import sys
import tempfile
import types
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("NEBIUS_API_KEY", "test-key-not-real")

from src.copilot import cli  # noqa: E402
from src.copilot import llm as llm_module  # noqa: E402
from src.copilot.tools import TOOL_IMPLS, TOOL_SCHEMAS, module_modifier  # noqa: E402

ORIGINAL = "module m;\n  assign a = 1'b0;\nendmodule\n"
PROPOSED = "module m;\n  assign a = 1'b1;\nendmodule\n"

tmp = Path(tempfile.mkdtemp(prefix="copilot_gate_"))
try:
    target = tmp / "m.v"
    module_modifier._PENDING_DIFFS_PATH = tmp / "pending.json"

    def seed_pending() -> str:
        target.write_text(ORIGINAL, encoding="utf-8")
        module_modifier._save_pending({"abc123": {"module_path": str(target), "new_content": PROPOSED}})
        return "abc123"

    def fake_client_calling_apply(diff_id: str):
        """Model asks for apply_diff once, then gives a final answer."""
        calls = {"n": 0}

        def create(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                args = f'{{"diff_id": "{diff_id}"}}'
                tc = types.SimpleNamespace(id="c1", function=types.SimpleNamespace(name="apply_diff", arguments=args))
                msg = types.SimpleNamespace(content=None, tool_calls=[tc])
                msg.model_dump = lambda exclude_none=True: {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "apply_diff", "arguments": args}}],
                }
            else:
                msg = types.SimpleNamespace(content="done", tool_calls=None)
                msg.model_dump = lambda exclude_none=True: {"role": "assistant", "content": "done"}
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])

        return types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))

    def run(diff_id: str, confirm):
        with mock.patch.object(llm_module, "get_client", return_value=fake_client_calling_apply(diff_id)):
            _, history = llm_module.run_agent_loop(
                user_message="apply it",
                history=[],
                tool_schemas=TOOL_SCHEMAS,
                tool_impls=TOOL_IMPLS,
                confirm_tool_call=confirm,
            )
        return next(m["content"] for m in history if m.get("role") == "tool")

    # 1. No confirm handler (cron job / endpoint): declined, file untouched, diff still pending.
    diff_id = seed_pending()
    tool_result = run(diff_id, confirm=None)
    assert '"declined"' in tool_result, tool_result
    assert target.read_text(encoding="utf-8") == ORIGINAL
    assert diff_id in module_modifier._load_pending()
    print("confirm_tool_call=None -> apply_diff declined, nothing written: OK")

    # 2. Handler says no: declined, file untouched.
    tool_result = run(diff_id, confirm=lambda name, args: False)
    assert '"declined"' in tool_result and target.read_text(encoding="utf-8") == ORIGINAL
    print("confirm_tool_call=False -> nothing written: OK")

    # 3. Real CLI prompt prints the actual diff before asking; "n" writes nothing.
    out = io.StringIO()
    with mock.patch("builtins.input", return_value="n"), redirect_stdout(out):
        tool_result = run(diff_id, confirm=cli.confirm_tool_call)
    shown = out.getvalue()
    assert "-  assign a = 1'b0;" in shown and "+  assign a = 1'b1;" in shown, shown
    assert '"declined"' in tool_result and target.read_text(encoding="utf-8") == ORIGINAL
    print("CLI prompt shows the real diff; 'n' writes nothing: OK")

    # 4. Unknown diff_id: CLI declines without ever prompting.
    with mock.patch("builtins.input", side_effect=AssertionError("must not prompt")), redirect_stdout(io.StringIO()):
        tool_result = run("nope0000", confirm=cli.confirm_tool_call)
    assert '"declined"' in tool_result
    print("unknown diff_id declined without prompting: OK")

    # 5. Calling apply_diff directly (skipping the loop and prompt) never writes.
    result = TOOL_IMPLS["apply_diff"](diff_id=diff_id)
    assert result["status"] == "declined" and target.read_text(encoding="utf-8") == ORIGINAL, result
    print("direct apply_diff call without approval writes nothing: OK")

    # 6. File edited while the prompt is open: the "y" covered the old state -> refused.
    def edit_file_then_yes(_prompt):
        target.write_text("module m; // hand edit\nendmodule\n", encoding="utf-8")
        return "y"

    with mock.patch("builtins.input", side_effect=edit_file_then_yes), redirect_stdout(io.StringIO()):
        tool_result = run(diff_id, confirm=cli.confirm_tool_call)
    assert '"declined"' in tool_result, tool_result
    assert "hand edit" in target.read_text(encoding="utf-8")
    assert "approved_fingerprint" not in module_modifier._load_pending()[diff_id]
    print("file changed after the diff was shown -> refused: OK")

    # 7. Pending diff swapped while the prompt is open -> refused.
    diff_id = seed_pending()

    def swap_diff_then_yes(_prompt):
        module_modifier._save_pending({diff_id: {"module_path": str(target), "new_content": "module evil;\nendmodule\n"}})
        return "y"

    with mock.patch("builtins.input", side_effect=swap_diff_then_yes), redirect_stdout(io.StringIO()):
        tool_result = run(diff_id, confirm=cli.confirm_tool_call)
    assert '"declined"' in tool_result and target.read_text(encoding="utf-8") == ORIGINAL, tool_result
    print("pending diff changed after it was shown -> refused: OK")

    # 8. Corrupt pending store: CLI declines without prompting or crashing.
    module_modifier._PENDING_DIFFS_PATH.write_text("{not json", encoding="utf-8")
    with mock.patch("builtins.input", side_effect=AssertionError("must not prompt")), redirect_stdout(io.StringIO()):
        assert cli.confirm_tool_call("apply_diff", {"diff_id": diff_id}) is False
    print("preview error -> declined without crashing: OK")

    # 9. A confirm handler that raises counts as "no".
    diff_id = seed_pending()

    def broken_handler(name, args):
        raise RuntimeError("boom")

    tool_result = run(diff_id, confirm=broken_handler)
    assert '"declined"' in tool_result and target.read_text(encoding="utf-8") == ORIGINAL, tool_result
    print("crashing confirm handler -> declined: OK")

    # 10. Approved but the write fails: proposal kept, approval cleared.
    _real_write_text = Path.write_text

    def target_is_read_only(self, *args, **kwargs):
        if self == target:
            raise PermissionError("read-only")
        return _real_write_text(self, *args, **kwargs)

    with mock.patch("builtins.input", return_value="y"), redirect_stdout(io.StringIO()), mock.patch.object(
        Path, "write_text", target_is_read_only
    ):
        tool_result = run(diff_id, confirm=cli.confirm_tool_call)
    assert '"error"' in tool_result and target.read_text(encoding="utf-8") == ORIGINAL, tool_result
    assert diff_id in module_modifier._load_pending()
    assert "approved_fingerprint" not in module_modifier._load_pending()[diff_id]
    print("write failure keeps the pending diff, clears approval: OK")

    # 11. Only an explicit typed "y" at the CLI prompt writes the file.
    with mock.patch("builtins.input", return_value="y"), redirect_stdout(io.StringIO()):
        tool_result = run(diff_id, confirm=cli.confirm_tool_call)
    assert '"ok"' in tool_result and target.read_text(encoding="utf-8") == PROPOSED, tool_result
    assert diff_id not in module_modifier._load_pending()
    print("explicit 'y' applies the diff: OK")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print("\nAPPLY GATE TESTS PASSED")
