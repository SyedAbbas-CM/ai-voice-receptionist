#!/bin/bash
# systemd wrapper for uvicorn.  Distinct from apps/api/scripts/run_server.sh
# because that one uses `nohup` + background redirection which confuses
# systemd Type=exec (systemd wants to inherit the process, not have it
# double-forked).  This one exec's uvicorn in the foreground so systemd
# tracks the real PID and its stdio.
#
# Created 2026-08-24 as part of the Lightsail systemd install (task #67).
# Deployed at /home/ubuntu/receptionist-agent/apps/api/scripts/systemd_run.sh
# and referenced by /etc/systemd/system/receptionist.service ExecStart.
#
# Log destination matches run_server.sh's convention so all existing tooling
# (tail apps/api/data/logs/uvicorn-latest.log, /tmp/uvicorn.log symlink,
# grep for CA-ids across restarts) continues working unchanged.

set -e

REPO_ROOT="/home/ubuntu/receptionist-agent"
LOG_DIR="$REPO_ROOT/apps/api/data/logs"
mkdir -p "$LOG_DIR"

STAMP="$(date +%Y-%m-%d_%H%M%S)"
LOG_FILE="$LOG_DIR/uvicorn-$STAMP.log"

# Symlinks for grep tooling continuity
ln -sfn "uvicorn-$STAMP.log" "$LOG_DIR/uvicorn-latest.log"
ln -sfn "$LOG_FILE" /tmp/uvicorn.log

cd "$REPO_ROOT/apps/api"

# exec so systemd sees uvicorn as MAINPID (not this wrapper).  Redirect
# stdio to the dated file so `journalctl -u receptionist` stays clean
# and detailed logs stay grep-friendly on disk.
exec "$REPO_ROOT/.venv/bin/uvicorn" \
    app.main:app \
    --host 0.0.0.0 --port 8000 --log-level info \
    >> "$LOG_FILE" 2>&1
