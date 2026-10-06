"""brief.py with a mocked LLM: the prompt carries every tracked project,
the output gets a dated header, and a failed LLM call still prints the raw
rollup and exits non-zero (so a scheduled job shows as failed, not silent).
"""

import io
import os
import sys
import types
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("NEBIUS_API_KEY", "test-key-not-real")

from src.copilot import brief, memory  # noqa: E402

seen = {}


def fake_create(**kwargs):
    seen["kwargs"] = kwargs
    seen["messages"] = kwargs["messages"]
    msg = types.SimpleNamespace(content="- mxint8-gemm: not started\n\nFocus today: start the GEMM RTL.")
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=msg)])


ok_client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=fake_create)))

with mock.patch.object(brief, "get_client", return_value=ok_client):
    text = brief.build_brief(today="2026-10-06")
assert text.startswith("# Daily brief - 2026-10-06"), text
assert "Focus today" in text
user_prompt = seen["messages"][1]["content"]
for project in memory.list_projects():
    assert project in user_prompt, project
assert "tools" not in seen["kwargs"]  # plain completion, no tool access
print("build_brief: header + every project in the prompt: OK")

with mock.patch.object(brief, "get_client", return_value=ok_client), mock.patch.dict(os.environ, {"BRIEF_DATE": "2030-01-02"}):
    assert brief.build_brief().startswith("# Daily brief - 2030-01-02")
print("build_brief: BRIEF_DATE overrides the (UTC) container date: OK")


def failing_create(**kwargs):
    raise ConnectionError("Nebius unreachable")


bad_client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=failing_create)))
out, err = io.StringIO(), io.StringIO()
with mock.patch.object(brief, "get_client", return_value=bad_client), redirect_stdout(out), redirect_stderr(err):
    code = brief.main()
assert code == 1
assert "Nebius unreachable" in err.getvalue()
assert "mxint8-gemm" in out.getvalue()  # raw rollup still emitted
print("main(): LLM failure -> raw rollup printed, exit code 1: OK")

# Pasted secrets often end in a newline; httpx rejects that header and the SDK
# reports "Connection error". Settings must strip it (seen in the first CI run).
from src.copilot.config import Settings  # noqa: E402

with mock.patch.dict(os.environ, {"NEBIUS_API_KEY": "  key-123\n", "NEBIUS_MODEL": " \n", "NEBIUS_BASE_URL": "https://x/v1/\r\n"}):
    s = Settings()
assert s.nebius_api_key == "key-123", repr(s.nebius_api_key)
assert s.nebius_model == "nvidia/nemotron-3-super-120b-a12b", repr(s.nebius_model)  # blank -> default
assert s.nebius_base_url == "https://x/v1/", repr(s.nebius_base_url)
print("Settings: whitespace stripped from env values, blank -> default: OK")


def cause_create(**kwargs):
    try:
        raise ValueError("Illegal header value b'Bearer secret'")
    except ValueError as inner:
        raise ConnectionError("Connection error.") from inner


cause_client = types.SimpleNamespace(chat=types.SimpleNamespace(completions=types.SimpleNamespace(create=cause_create)))
err = io.StringIO()
with mock.patch.object(brief, "get_client", return_value=cause_client), redirect_stdout(io.StringIO()), redirect_stderr(err):
    assert brief.main() == 1
assert "(cause: ValueError)" in err.getvalue(), err.getvalue()
assert "secret" not in err.getvalue(), "cause message (may hold the key) must not be printed"
print("main(): failure shows cause type but never the cause message: OK")

print("\nBRIEF TESTS PASSED")
