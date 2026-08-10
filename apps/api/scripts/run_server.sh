#!/bin/bash
# 2026-08-11 (task #311): persistent dated log capture.
# Replaces `nohup uvicorn ... > /tmp/uvicorn.log 2>&1 &` which lost
# every restart's history to /tmp rotation.  Logs now go to
# apps/api/data/logs/uvicorn-YYYY-MM-DD_HHMMSS.log per restart.
#
# Usage:  ./apps/api/scripts/run_server.sh
# Then:   tail -f apps/api/data/logs/uvicorn-latest.log

set -e

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
LOG_DIR="$REPO_ROOT/apps/api/data/logs"
mkdir -p "$LOG_DIR"

STAMP="$(date +%Y-%m-%d_%H%M%S)"
LOG_FILE="$LOG_DIR/uvicorn-$STAMP.log"

# Symlink 'latest' for easy tailing
ln -sfn "uvicorn-$STAMP.log" "$LOG_DIR/uvicorn-latest.log"

# Also keep /tmp copy so existing tooling that greps it still works
ln -sfn "$LOG_FILE" /tmp/uvicorn.log

# Prune anything older than 14 days so disk doesn't fill up
find "$LOG_DIR" -name 'uvicorn-*.log' -type f -mtime +14 -delete 2>/dev/null || true

cd "$REPO_ROOT/apps/api"
exec nohup "$REPO_ROOT/.venv/bin/uvicorn" \
    app.main:app \
    --host 0.0.0.0 --port 8000 --log-level info \
    > "$LOG_FILE" 2>&1
