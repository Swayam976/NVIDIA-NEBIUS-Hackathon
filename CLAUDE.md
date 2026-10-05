# Instructions for Claude Code

Read PROJECT.md and TASKS.md before starting any work.

## Role
Claude is the implementer. Codex is the reviewer. Swayam sets requirements and approves.

## Workflow per task
1. Take one task from TASKS.md, mark it "in progress (Claude)".
2. Implement it. Keep the diff focused on that task.
3. Run the checks listed in PROJECT.md.
4. Stop editing and request a review:
   powershell -File scripts/codex-review.ps1   (Windows)
   ./scripts/codex-review.sh                   (WSL / Linux / macOS)
5. Judge each finding on its merits. Fix valid ones, note rejected ones with a
   one-line reason in TASKS.md handoff notes.
6. Re-run checks. At most 2 review rounds per task, then hand back to Swayam.
7. Commit with a clear message, update TASKS.md.

## Never
- Bypass, stub out or auto-approve the apply_diff confirmation gate.
- Edit files on the `codex-work` branch / `hw-copilot-codex` worktree.
- Commit `.env` or any API key.
