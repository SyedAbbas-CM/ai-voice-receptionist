# Deploy quickref — for any Claude session

**One line, one command.** For the full runbook see `DEPLOY-RUNBOOK.md`.

## Ship code to Lightsail

```bash
cd "/Users/az/Desktop/Receptionist Agent" && ./scripts/deploy.sh
```

That's it. Waits ~30-60s for /health to return 200, prints the new PID. Done.

## What happens under the hood

1. Rsyncs your working tree to `ubuntu@3.227.16.73:/home/ubuntu/receptionist-agent/` (excludes .env, .venv, DBs, logs, secrets)
2. Reinstalls Python deps if `apps/api/requirements.txt` changed
3. `sudo systemctl restart receptionist.service`
4. Polls `https://agent.eternalconquests.com/` up to 8x every 3s for HTTP 200
5. Prints new PID

## Flags

```bash
./scripts/deploy.sh --dry-run          # show what would happen, ship nothing
./scripts/deploy.sh --skip-rsync       # restart service only (fast retry after .env edit)
./scripts/deploy.sh --no-health-check  # fire and forget
```

## Flip a feature flag on the box (no code change)

```bash
# 1. SSH in + edit .env
ssh -i "/Users/az/Desktop/Receptionist Agent/LightsailDefaultKey-us-east-1.pem" ubuntu@3.227.16.73
sudo nano /home/ubuntu/receptionist-agent/.env    # or: sudo sed -i 's/^FLAG=.*/FLAG=true/' ...
exit

# 2. Restart without rsyncing
./scripts/deploy.sh --skip-rsync
```

The app reads `.env` via pydantic-settings at import time (NOT via systemd EnvironmentFile — see DEPLOY-RUNBOOK.md if confused).

## Verify what's live right now

```bash
# Health check
curl -sS -o /dev/null -w 'HTTP=%{http_code} time=%{time_total}s\n' https://agent.eternalconquests.com/

# Running PID + uptime
ssh -i "/Users/az/Desktop/Receptionist Agent/LightsailDefaultKey-us-east-1.pem" ubuntu@3.227.16.73 \
    'systemctl show receptionist.service -p MainPID --value; ps -o etime= -p $(systemctl show receptionist.service -p MainPID --value)'

# What flag is the app seeing (Python-level, not OS env)
ssh -i "/Users/az/Desktop/Receptionist Agent/LightsailDefaultKey-us-east-1.pem" ubuntu@3.227.16.73 \
    "cd /home/ubuntu/receptionist-agent/apps/api && ../../.venv/bin/python -c 'from app.core.config import settings; print(settings.next_action_policy_enabled)'"

# Tail live logs
ssh -i "/Users/az/Desktop/Receptionist Agent/LightsailDefaultKey-us-east-1.pem" ubuntu@3.227.16.73 \
    'sudo journalctl -u receptionist.service -n 50 --no-pager -f'
```

## Auto-deploy on git push

Push to `feat/architectural-networking` → GitHub Actions runs the same `deploy-lightsail.yml` workflow. Same rsync + restart + health check. See `.github/workflows/deploy-lightsail.yml`.

## Pull a call trace after a live call

```bash
# By CallSid (from Twilio Console)
curl -sS -H "Authorization: Bearer $ADMIN_TOKEN" \
    "https://agent.eternalconquests.com/trace/CA<sid>?f=json"

# Or humanness-events view (voice-agent's tenant-scoped view)
curl -sS "https://agent.eternalconquests.com/trace/CA<sid>?f=json"

# If you don't know the CallSid, get the most recent 5 sessions:
ssh -i "/Users/az/Desktop/Receptionist Agent/LightsailDefaultKey-us-east-1.pem" ubuntu@3.227.16.73 \
    'python3 -c "import sqlite3; c=sqlite3.connect(\"/home/ubuntu/receptionist-agent/data/voiceops.db\"); [print(r) for r in c.execute(\"SELECT id, started_at FROM sessions ORDER BY started_at DESC LIMIT 5\").fetchall()]"'
```

## When it breaks

**`SSH to ubuntu@3.227.16.73 failed`** — PEM missing or wrong. Check `LightsailDefaultKey-us-east-1.pem` in the repo root (gitignored).

**`Health check FAILED after 8 attempts`** — service is booting slowly OR crashed on the new code. Tail the journal:
```bash
ssh -i "/Users/az/Desktop/Receptionist Agent/LightsailDefaultKey-us-east-1.pem" ubuntu@3.227.16.73 \
    'sudo journalctl -u receptionist.service -n 100 --no-pager'
```

**Rolling back:** `git revert HEAD && ./scripts/deploy.sh` — ~60s to previous state.

## Files a future agent should know about

| File | What it is |
|------|------------|
| `scripts/deploy.sh` | The deploy script itself |
| `.github/workflows/deploy-lightsail.yml` | Same flow on git push |
| `docs/DEPLOY-RUNBOOK.md` | Full docs + all env var mechanics |
| `docs/DEPLOY-QUICKREF.md` | This file |
| `LightsailDefaultKey-us-east-1.pem` | SSH private key (gitignored) |
| `terraform/` | Client-provisioning module (not used for our own Lightsail; targets future ECS Fargate per-client) |

## What the deploy script deliberately does NOT do

- No git commit or push (you own that)
- No touching secrets on the box (edit .env manually)
- No test run before shipping (do `pytest apps/api/tests/` yourself)
- No blue-green (~10s downtime during systemctl restart — acceptable at pilot)
- No database migrations (Alembic runs on service startup)
