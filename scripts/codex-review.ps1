# Ask Codex for a read-only review of the uncommitted diff. Prints findings to stdout.
$ErrorActionPreference = "Stop"
Set-Location (git rev-parse --show-toplevel)
git diff --quiet HEAD
if ($LASTEXITCODE -eq 0) { Write-Host "No uncommitted changes to review."; exit 0 }
codex exec --sandbox read-only "Follow AGENTS.md (reviewer role). Read PROJECT.md. Review the current uncommitted git diff (git diff HEAD). Do not modify files."
