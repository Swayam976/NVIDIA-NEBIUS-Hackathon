"""Daily brief: rolls up every tracked project's status and blockers from
memory and has Nemotron turn it into a short morning brief.

Run locally:  python -m src.copilot.brief
Scheduled:    .github/workflows/daily-brief.yml runs this every morning on
GitHub Actions; the brief lands in the run log and job summary.

Read-only by design: one plain LLM call with no tools attached, so a
scheduled run can never modify RTL or reach apply_diff.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sys

from .config import settings
from .llm import get_client
from .tools.workflow import daily_brief

_BRIEF_SYSTEM_PROMPT = """\
You write a short daily engineering brief for a hardware designer. For each \
project give: a one-line status, its blockers (or "none"), and one concrete \
next step. Finish with a single "Focus today:" line naming the most \
important task across all projects. Plain markdown, under 250 words. Use \
only the facts provided; never invent progress, results or blockers.
"""


def build_brief(today: str | None = None) -> str:
    # CI runners use UTC; the workflow passes the user's local date as BRIEF_DATE.
    today = today or os.environ.get("BRIEF_DATE") or _dt.date.today().isoformat()
    rollup = daily_brief()["projects"]
    response = get_client().chat.completions.create(
        model=settings.nebius_model,
        messages=[
            {"role": "system", "content": _BRIEF_SYSTEM_PROMPT},
            {"role": "user", "content": f"Date: {today}\nProjects:\n{json.dumps(rollup, indent=2)}"},
        ],
    )
    body = (response.choices[0].message.content or "").strip()
    if not body:
        raise RuntimeError("model returned an empty brief")
    return f"# Daily brief - {today}\n\n{body}\n"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    problems = settings.validate()
    if problems:
        for p in problems:
            print(f"Config problem: {p}", file=sys.stderr)
        return 1

    try:
        print(build_brief())
    except Exception as exc:  # noqa: BLE001 - still emit the raw rollup so the run isn't wasted
        # Cause *type* only: its message can echo request headers (the API key).
        cause = f" (cause: {type(exc.__cause__).__name__})" if exc.__cause__ else ""
        print(f"Brief generation failed: {type(exc).__name__}: {exc}{cause}", file=sys.stderr)
        print(json.dumps(daily_brief()["projects"], indent=2))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
