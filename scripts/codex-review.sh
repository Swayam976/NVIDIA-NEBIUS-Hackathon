#!/usr/bin/env bash
# Ask Codex for a read-only review of the uncommitted diff. Prints findings to stdout.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
if git diff --quiet HEAD; then
  echo "No uncommitted changes to review." >&2
  exit 0
fi
codex exec --sandbox read-only \
  "Follow AGENTS.md (reviewer role). Read PROJECT.md. Review the current uncommitted git diff (git diff HEAD). Do not modify files."
