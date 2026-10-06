"""demo/streamlit_app.py end to end with Streamlit's AppTest and a mocked
LLM: password gate, a chat turn where the model proposes a fix via
modify_module, the diff shown in the Pending changes panel, nothing written
until Approve is clicked, then the file updated; plus the session cap.

Needs streamlit installed (it is a demo-only dependency); skipped otherwise.
"""

import json
import os
import shutil
import sys
import types
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("NEBIUS_API_KEY", "test-key-not-real")

try:
    from streamlit.testing.v1 import AppTest
except ImportError:
    print("web demo tests: SKIPPED (streamlit not installed; pip install -r demo/requirements.txt)")
    sys.exit(0)

from src.copilot import llm  # noqa: E402
from src.copilot.tools import module_modifier  # noqa: E402

APP = str(Path(__file__).resolve().parents[1] / "demo" / "streamlit_app.py")
FIXED_ALU = (Path(__file__).resolve().parents[1] / "demo" / "sample" / "rtl" / "alu.v").read_text(encoding="utf-8").replace(
    "            default: y = 8'h00;", "            3'b100:  y = a ^ b;\n            default: y = 8'h00;"
)


def _msg(content=None, tool_calls=None):
    m = types.SimpleNamespace(content=content, tool_calls=tool_calls)
    dumped = {"role": "assistant", "content": content}
    if tool_calls:
        dumped["tool_calls"] = [
            {"id": c.id, "type": "function", "function": {"name": c.function.name, "arguments": c.function.arguments}}
            for c in tool_calls
        ]
    m.model_dump = lambda exclude_none=True: dumped
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=m)])


def make_client():
    state = {"agent_calls": 0, "systems": [], "content_suffix": ""}

    def create(**kwargs):
        if kwargs.get("response_format"):  # modify_module's edit request
            content = FIXED_ALU + state["content_suffix"]
            return _msg(json.dumps({"new_content": content, "explanation": "Implement XOR for op 3'b100."}))
        state["agent_calls"] += 1
        state["systems"].append(kwargs["messages"][0]["content"])
        if state["agent_calls"] == 1:
            args = json.dumps({"module_path": "rtl/alu.v", "instruction": "implement XOR for op 3'b100"})
            call = types.SimpleNamespace(id="c1", function=types.SimpleNamespace(name="modify_module", arguments=args))
            return _msg(tool_calls=[call])
        return _msg("I proposed a fix; review it in the Pending changes panel and click Approve.")

    client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=create)))
    return client, state


client, state = make_client()
with mock.patch.object(llm, "get_client", return_value=client), mock.patch.object(
    module_modifier, "get_client", return_value=client
):
    at = AppTest.from_file(APP, default_timeout=60)
    at.secrets["DEMO_PASSWORD"] = "letmein"
    at.secrets["DEMO_SESSION_LIMIT"] = "2"
    at.run()
    assert not at.exception, at.exception
    assert len(at.chat_input) == 0, "chat must be hidden behind the password"

    at.text_input[0].input("wrong")
    at.button[0].click()
    at.run()
    assert any("Wrong password" in e.value for e in at.error)
    at.text_input[0].input("letmein")
    at.button[0].click()
    at.run()
    assert not at.exception, at.exception
    assert len(at.chat_input) == 1
    print("password gate: wrong rejected, right accepted: OK")

    ws = at.session_state["ws"]
    alu = ws.root / "rtl" / "alu.v"
    original = alu.read_text(encoding="utf-8")
    # Proposed content that happens to contain the server path: the panel must
    # show it verbatim (approval covers exactly what is shown), header scrubbed.
    state["content_suffix"] = f"// built in {ws.root}\n"
    try:
        at.chat_input[0].set_value("Fix the failing operation in rtl/alu.v.")
        at.run()
        assert not at.exception, at.exception
        assert "rtl/alu.v" in state["systems"][0] and "Pending changes" in state["systems"][0]
        codes = [c.value for c in at.code if c.language == "diff"]
        assert codes and "+            3'b100:  y = a ^ b;" in codes[0], codes
        header, body = codes[0].splitlines()[:2], codes[0].splitlines()[2:]
        assert not any(str(ws.root) in h for h in header), "server path shown in diff header"
        assert f"+// built in {ws.root}" in body, "diff body altered: shown != what Approve writes"
        assert alu.read_text(encoding="utf-8") == original, "written before approval!"
        print("chat turn: model proposed a diff, shown in panel, file untouched: OK")

        approve = next(b for b in at.button if b.label == "Approve & apply")
        approve.click()
        at.run()
        assert not at.exception, at.exception
        assert alu.read_text(encoding="utf-8") == FIXED_ALU + state["content_suffix"]
        assert any("Approved diff" in i.value for i in at.info)
        assert not [c for c in at.code if c.language == "diff"], "applied diff still pending"
        print("Approve click: diff applied to the session workspace: OK")

        at.chat_input[0].set_value("Thanks.")
        at.run()
        assert at.chat_input[0].disabled, "session cap (2) not enforced"
        assert any("limit" in w.value for w in at.warning)
        next(b for b in at.button if b.label == "Reset workspace").click()
        at.run()
        assert at.chat_input[0].disabled, "Reset workspace must not reset the message cap"
        ws = at.session_state["ws"]  # fresh workspace; cleaned up below
        print("session message cap enforced, survives Reset workspace: OK")
    finally:
        shutil.rmtree(ws.root, ignore_errors=True)

print("\nWEB DEMO TESTS PASSED")
