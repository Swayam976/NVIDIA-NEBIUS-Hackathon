"""Simple REPL for the hardware design copilot.

Run with: python -m src.copilot.cli
"""

from __future__ import annotations

import sys

from .config import settings
from .llm import run_agent_loop
from .tools import TOOL_IMPLS, TOOL_SCHEMAS


def confirm_tool_call(name: str, args: dict) -> bool:
    """Gate for sensitive tool calls. Currently only apply_diff routes
    here (see llm._CONFIRM_REQUIRED) — this is where the "show diff, wait
    for yes" safety behavior actually lives.
    """
    print(f"\n--- The agent wants to call `{name}` with: {args} ---")
    answer = input("Apply this change to disk? [y/N] ").strip().lower()
    return answer == "y"


def main() -> None:
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
        print(f"\ncopilot> {reply}\n")


if __name__ == "__main__":
    main()
