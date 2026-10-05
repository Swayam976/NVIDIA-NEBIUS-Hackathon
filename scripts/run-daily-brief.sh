#!/usr/bin/env bash
# Creates one CPU-only Nebius Serverless Job that prints today's brief to the
# job logs. Nebius jobs have no built-in schedule, so Windows Task Scheduler
# runs this daily (scripts/register-brief-task.ps1). Runs in WSL because the
# nebius CLI has no native Windows build.
#
# Extra args are passed to `nebius ai job create`, e.g. --dry-run.
# Read the result with: nebius ai logs <job-id>
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [[ -n "${BRIEF_LOG:-}" ]]; then
    exec >> "$BRIEF_LOG" 2>&1
    echo "=== $(date -Is) run-daily-brief"
fi

NEBIUS="${NEBIUS_BIN:-$HOME/.nebius/bin/nebius}"
SECRET="${BRIEF_SECRET:-hw-copilot-nebius-key}"   # MysteryBox secret with payload key NEBIUS_API_KEY
PLATFORM="${BRIEF_PLATFORM:-cpu-d3}"
PRESET="${BRIEF_PRESET:-2vcpu-8gb}"
IMAGE="${BRIEF_IMAGE:-python:3.12}"

# Only the non-secret model settings are read from .env; the API key reaches
# the job from MysteryBox and never appears in the job spec.
# Handles KEY=val, KEY="val", KEY='val' and trailing " # comments".
env_value() {
    local v
    v="$(grep -E "^$1=" "$REPO_ROOT/.env" 2>/dev/null | head -1 | cut -d= -f2- | tr -d '\r')" || true
    case "$v" in
        \"*) v="${v#\"}"; v="${v%%\"*}" ;;
        \'*) v="${v#\'}"; v="${v%%\'*}" ;;
        *)   v="${v%%[[:space:]]#*}"; v="${v%"${v##*[![:space:]]}"}" ;;
    esac
    printf '%s' "$v"
}
MODEL="$(env_value NEBIUS_MODEL)"
BASE_URL="$(env_value NEBIUS_BASE_URL)"

args=(
    ai job create
    --name "hw-brief-$(date +%Y%m%d-%H%M)"
    --image "$IMAGE"
    --platform "$PLATFORM"
    --preset "$PRESET"
    --timeout 1h
    --env-secret "NEBIUS_API_KEY=$SECRET"
    --env "BRIEF_DATE=$(date +%F)"
    --inject-file "$REPO_ROOT/scripts/brief-job-entry.sh:/opt/brief/entry.sh"
    --container-command bash
    --args /opt/brief/entry.sh
    --async
)
[[ -n "$MODEL" ]] && args+=(--env "NEBIUS_MODEL=$MODEL")
[[ -n "$BASE_URL" ]] && args+=(--env "NEBIUS_BASE_URL=$BASE_URL")
[[ -n "${BRIEF_REPO_URL:-}" ]] && args+=(--env "BRIEF_REPO_URL=$BRIEF_REPO_URL")
[[ -n "${BRIEF_REPO_REF:-}" ]] && args+=(--env "BRIEF_REPO_REF=$BRIEF_REPO_REF")
for f in "$REPO_ROOT"/memory/projects/*.md; do
    args+=(--inject-file "$f:/opt/brief/memory/$(basename "$f")")
done

"$NEBIUS" "${args[@]}" "$@"
