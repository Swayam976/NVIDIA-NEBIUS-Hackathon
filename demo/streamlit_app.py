"""hw-copilot web demo, for Streamlit Community Cloud.

Deploy with main file `demo/streamlit_app.py`. Secrets (TOML, app settings):
    NEBIUS_API_KEY = "..."
    DEMO_PASSWORD  = "..."
Optional: NEBIUS_MODEL, NEBIUS_BASE_URL, DEMO_SESSION_LIMIT (default 15),
DEMO_DAILY_LIMIT (default 150).

Each visitor gets a throwaway workspace (project memory + a sample ALU).
Every tool is confined to it (src/copilot/sandbox.py). The model can only
*propose* changes; nothing is written until the visitor clicks Approve on
the exact diff shown in the Pending changes panel.

Local run: streamlit run demo/streamlit_app.py  (reads .env / env vars)
"""

from __future__ import annotations

import hmac
import os
import shutil
import sys
import time
from pathlib import Path

import streamlit as st

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _secret(name: str, default: str = "") -> str:
    try:
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:  # noqa: BLE001 - no secrets.toml (local run): fall back to env
        pass
    return os.environ.get(name, default)


# src.copilot reads its settings from the environment on first import.
for _key in ("NEBIUS_API_KEY", "NEBIUS_MODEL", "NEBIUS_BASE_URL"):
    if _value := _secret(_key):
        os.environ[_key] = _value

from src.copilot import sandbox  # noqa: E402
from src.copilot.config import MEMORY_DIR, settings  # noqa: E402
from src.copilot.llm import run_agent_loop  # noqa: E402
from src.copilot.tools import module_modifier  # noqa: E402

SAMPLE_DIR = REPO_ROOT / "demo" / "sample"
SESSION_LIMIT = int(_secret("DEMO_SESSION_LIMIT", "15"))
DAILY_LIMIT = int(_secret("DEMO_DAILY_LIMIT", "150"))
EXAMPLES = [
    "Run the testbench rtl/alu_tb.v against rtl/alu.v and tell me what fails.",
    "Fix the failing operation in rtl/alu.v.",
    "Lint rtl/alu.v.",
    "Give me a daily brief across my projects.",
]

st.set_page_config(page_title="hw-copilot demo", page_icon=":wrench:", layout="wide")


@st.cache_resource
def _daily_counter(limit: int) -> sandbox.DailyCounter:
    return sandbox.DailyCounter(limit)


def _require_password() -> None:
    expected = _secret("DEMO_PASSWORD")
    if not expected:
        st.error("This demo is not configured (DEMO_PASSWORD secret is missing).")
        st.stop()
    if st.session_state.get("authed"):
        return
    st.title("hw-copilot demo")
    with st.form("login"):
        password = st.text_input("Demo password", type="password")
        if st.form_submit_button("Enter"):
            if hmac.compare_digest(password.encode(), expected.encode()):
                st.session_state.authed = True
                st.rerun()
            time.sleep(1)  # slow down guessing
            st.error("Wrong password.")
    st.stop()


def _session():
    s = st.session_state
    if "used" not in s:
        s.used = 0  # survives "Reset workspace", so resets can't bypass the cap
    if "ws" not in s or not s.ws.root.exists():
        s.ws = sandbox.create_workspace(MEMORY_DIR, SAMPLE_DIR)
        s.history, s.chat = [], []
    return s


def _reset_workspace() -> None:
    s = st.session_state
    if "ws" in s:
        shutil.rmtree(s.ws.root, ignore_errors=True)
        del s["ws"]


def _extra_system(ws: sandbox.Workspace, projects: list[str]) -> str:
    return (
        "You are running as a public web demo inside a sandboxed workspace. Its files, with paths "
        f"relative to the workspace, are: {', '.join(ws.files())}. Tracked projects: {', '.join(projects)}. "
        "Pass these relative paths to tools. apply_diff is not available to you here: after "
        "modify_module, tell the user to review the diff in the 'Pending changes' panel and click "
        "Approve to apply it. Lint and testbenches only run on workspace files; file I/O system tasks "
        "and `include are blocked."
    )


def _compact(history: list[dict], keep: int = 20) -> list[dict]:
    """Bounds token use: once long, keep only the recent plain chat turns."""
    if len(history) <= 40:
        return history
    plain = [m for m in history if m.get("role") in ("user", "assistant") and not m.get("tool_calls")]
    return plain[-keep:]


def _note(text: str) -> None:
    s = st.session_state
    s.chat.append(("note", text))
    s.history.append({"role": "user", "content": f"[approval panel] {text}"})


def _approve(diff_id: str, fingerprint: str) -> None:
    """Runs on the visitor's click; the fingerprint is the one computed when
    this exact diff was rendered, so a changed file/diff is refused."""
    s = st.session_state
    with sandbox.activate(s.ws):
        if diff_id not in sandbox.pending_diff_ids(s.ws):
            msg = f"Diff {diff_id} is no longer pending."
        elif not module_modifier.approve_pending_diff(diff_id, fingerprint):
            msg = f"Could not record approval for diff {diff_id}."
        else:
            msg = module_modifier.apply_diff(diff_id)["message"]
    _note(f"Approved diff {diff_id}: {s.ws.scrub(msg)}")


def _discard(diff_id: str) -> None:
    s = st.session_state
    with sandbox.activate(s.ws):
        module_modifier.discard_pending_diff(diff_id)
    _note(f"Discarded diff {diff_id} without applying it.")


def _display_diff(ws: sandbox.Workspace, diff_text: str) -> str:
    return sandbox.display_diff(ws, diff_text)


def _pending_panel(ws: sandbox.Workspace) -> None:
    previews = []
    with sandbox.activate(ws):
        for diff_id in sandbox.pending_diff_ids(ws):
            try:
                preview = module_modifier.preview_pending_diff(diff_id)
            except (OSError, ValueError):
                preview = None
            if preview:
                previews.append((diff_id, *preview))
    if not previews:
        return
    st.subheader("Pending changes")
    for diff_id, diff_text, fingerprint in previews:
        with st.container(border=True):
            st.caption(f"Diff {diff_id}. Nothing is written until you click Approve.")
            st.code(_display_diff(ws, diff_text), language="diff")
            left, right = st.columns(2)
            left.button("Approve & apply", key=f"approve_{diff_id}", type="primary",
                        on_click=_approve, args=(diff_id, fingerprint))
            right.button("Discard", key=f"discard_{diff_id}", on_click=_discard, args=(diff_id,))


def _sidebar(s) -> None:
    with st.sidebar:
        st.markdown("**hw-copilot**: NVIDIA Nemotron via Nebius Token Factory, with RTL tools "
                    "(Icarus Verilog, Verilator) and an approval-gated editor.")
        st.caption(f"Messages this session: {s.used}/{SESSION_LIMIT}")
        st.markdown("**Workspace files**")
        for rel in s.ws.files():
            with st.expander(rel):
                st.code((s.ws.root / rel).read_text(encoding="utf-8", errors="replace"),
                        language="verilog" if rel.endswith(".v") else "markdown")
        st.button("Reset workspace", on_click=_reset_workspace)


def main() -> None:
    _require_password()
    problems = settings.validate()
    if problems:
        st.error("This demo is not configured: " + " ".join(problems))
        st.stop()

    s = _session()
    _sidebar(s)
    st.title("hw-copilot demo")
    st.caption("Try: " + " | ".join(f"*{e}*" for e in EXAMPLES))

    for role, text in s.chat:
        if role == "note":
            st.info(text)
        else:
            with st.chat_message(role):
                st.markdown(text)

    _pending_panel(s.ws)

    prompt = st.chat_input("Ask the copilot...", disabled=s.used >= SESSION_LIMIT)
    if s.used >= SESSION_LIMIT:
        st.warning("Session message limit reached. Reset the workspace or come back later.")
    if not prompt or s.used >= SESSION_LIMIT:
        return
    if not _daily_counter(DAILY_LIMIT).try_take():
        st.warning("The demo's daily usage limit is reached. Please try again tomorrow.")
        return

    s.used += 1
    s.chat.append(("user", prompt))
    schemas, impls = sandbox.guarded_tools(s.ws)
    with st.spinner("Thinking..."), sandbox.activate(s.ws):
        from src.copilot import memory  # active workspace's memory

        try:
            reply, history = run_agent_loop(
                user_message=prompt,
                history=s.history,
                tool_schemas=schemas,
                tool_impls=impls,
                confirm_tool_call=None,  # fail closed: approval only via the panel
                extra_system=_extra_system(s.ws, memory.list_projects()),
            )
            s.history = _compact(history)
        except Exception as exc:  # noqa: BLE001 - keep the page alive, don't leak details
            reply = f"Sorry, the model call failed ({type(exc).__name__}). Please try again."
    s.chat.append(("assistant", s.ws.scrub(reply)))
    st.rerun()


main()
