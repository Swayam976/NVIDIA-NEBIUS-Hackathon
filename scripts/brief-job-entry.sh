#!/usr/bin/env bash
# Entrypoint inside the Nebius Serverless Job container (python:3.12 image).
# Injected by scripts/run-daily-brief.sh; prints today's brief to the job logs.
set -euo pipefail

REPO_URL="${BRIEF_REPO_URL:-https://github.com/Swayam976/NVIDIA-NEBIUS-Hackathon.git}"
REPO_REF="${BRIEF_REPO_REF:-main}"

git clone --quiet --depth 1 --branch "$REPO_REF" "$REPO_URL" /app
cd /app
pip install --quiet --no-cache-dir --root-user-action=ignore -r requirements.txt

# Prefer the memory files injected from the local machine, so the brief
# reflects current state even when memory changes haven't been pushed yet.
if compgen -G "/opt/brief/memory/*.md" > /dev/null; then
    rm -f memory/projects/*.md
    cp /opt/brief/memory/*.md memory/projects/
fi

exec python -m src.copilot.brief
