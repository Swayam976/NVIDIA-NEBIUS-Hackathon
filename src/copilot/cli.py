"""Simple REPL for the hardware design copilot.

Run with: python -m src.copilot.cli
"""

from __future__ import annotations

import re
import sys

from .config import settings
from .llm import run_agent_loop
from .tools import TOOL_IMPLS, TOOL_SCHEMAS
from .tools.module_modifier import approve_pending_diff, preview_pending_diff


_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f  ‪-‮⁦-⁩]")


def visible(text: str) -> str:
    """Terminal control characters (ANSI escapes, carriage returns, bidi
    overrides...) shown as \\xNN instead of interpreted, so a diff can't
    hide or rewrite its own lines on screen before the approval prompt."""
    return _CONTROL_RE.sub(lambda m: f"\\x{ord(m.group()):02x}" if ord(m.group()) < 256 else f"\\u{ord(m.group()):04x}",
                           text)


def confirm_tool_call(name: str, args: dict) -> bool:
    """Gate for sensitive tool calls. Currently only apply_diff routes
    here (see llm._CONFIRM_REQUIRED) — this is where the "show diff, wait
    for yes" safety behavior actually lives.
    """
    print(visible(f"\n--- The agent wants to call `{name}` with: {args} ---"))
    if name != "apply_diff":
        answer = input("Allow this action? [y/N] ").strip().lower()
        return answer == "y"

    # Show the exact diff that would be written, from the pending-diff store
    # — never rely on the model having shown it.
    diff_id = str(args.get("diff_id", ""))
    try:
        preview = preview_pending_diff(diff_id)
    except (OSError, ValueError) as exc:  # unreadable file / corrupt store
        print(f"Could not build a preview of diff {diff_id} ({exc}). Declining.")
        return False
    if preview is None:
        print("No pending diff with that id; nothing to apply. Declining.")
        return False
    diff_text, fingerprint = preview
    print(visible(diff_text))
    answer = input("Apply this change to disk? [y/N] ").strip().lower()
    if answer != "y":
        return False
    # The approval covers exactly what was shown; apply_diff re-checks it
    # and refuses if the file or diff changed while this prompt was open.
    return approve_pending_diff(diff_id, fingerprint)


def main() -> None:
    # Model output often contains non-ASCII punctuation (e.g. U+2011); on
    # Windows a redirected stdout defaults to cp1252 and would crash on it.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    problems = settings.validate()
    if problems:
        for p in problems:
            print(f"Config problem: {p}")
        sys.exit(1)

    print("Hardware design copilot. Type 'exit' to quit.\n")
    history: list[dict] = []

    while True:
        try:
            user_input = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit"}:
            break

        reply, history = run_agent_loop(
            user_message=user_input,
            history=history,
            tool_schemas=TOOL_SCHEMAS,
            tool_impls=TOOL_IMPLS,
            confirm_tool_call=confirm_tool_call,
        )
        print(visible(f"\ncopilot> {reply}\n"))


if __name__ == "__main__":
    main()
