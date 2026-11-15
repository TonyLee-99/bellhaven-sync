#!/usr/bin/env bash
# Daily pipeline run: scrape the website, read the CRM, queue proposals for review.
# Propose-only - nothing is written to the CRM until a reviewer approves it in the review app.
# Safe to re-run: decided proposals are never re-queued (see src/bellhaven_sync/state.py).
# cron has a minimal PATH, so this uses the checkout's own virtualenv by absolute path.
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
mkdir -p logs
LOG="logs/daily_$(date +%Y%m%d).log"
if ! PYTHONPATH="$ROOT/src" "$ROOT/.venv/bin/python" -m bellhaven_sync.cli run >> "$LOG" 2>&1; then
  echo "$(date -u +%FT%TZ) daily run FAILED - see $LOG" >> logs/ALERTS.log
  exit 1
fi
