# Instructions for Codex

Read PROJECT.md and TASKS.md before starting any work.

## Default role: reviewer
When asked to review, do not modify files. Review the current git diff for:
- correctness bugs and unhandled edge cases
- missing requirements from PROJECT.md
- anything that weakens the apply_diff approval gate (always flag as blocking)
- secrets or keys in the diff
Report findings as a numbered list, each with file:line, severity
(blocking / should-fix / nit) and a one-line suggested fix. No praise, no summary
of what the diff does.

## When given an implementation task
Only work in the `hw-copilot-codex` worktree on the `codex-work` branch, and only
on the task assigned to Codex in TASKS.md. Do not touch the agent loop
(`src/copilot/llm.py`) or the module_modifier / apply_diff path unless the task says so.

## Never
- Bypass or weaken the apply_diff confirmation gate.
- Swap the runtime model away from Nemotron on Nebius.
- Commit `.env` or any API key.
