# Deploy runbook

**One command to ship code to prod.** Any human or agent can use this.

## The command

```bash
./scripts/deploy.sh
```

That's it. It:

1. Confirms SSH works
2. Rsyncs the working tree to Lightsail (excludes secrets, .venv, DBs, logs)
3. Reinstalls Python deps if `requirements.txt` changed
4. `sudo systemctl restart receptionist.service`
5. Polls `https://agent.eternalconquests.com/` until it returns HTTP 200 (up to ~24s)
6. Prints the new PID

**Total wall-clock: 30-60s** for a code-only change. Longer if `requirements.txt` changed.

## When you'd use each path

| Situation | Command |
|-----------|---------|
| Manual deploy, any state | `./scripts/deploy.sh` |
| Preview what would happen | `./scripts/deploy.sh --dry-run` |
| Just restart the service, no code change | `./scripts/deploy.sh --skip-rsync` |
| Fire and forget, skip health poll | `./scripts/deploy.sh --no-health-check` |
| Push to main auto-deploys | `git push origin feat/architectural-networking` |

## What auto-deploys via GitHub Actions

`.github/workflows/deploy-lightsail.yml` runs on every push to `feat/architectural-networking`. Same rsync + restart + health-check flow as the local script. Deploy status shows up in the GitHub commit UI.

## What breaks + how to fix it

## Env vars on the box — how they actually load

The systemd unit at `/etc/systemd/system/receptionist.service` has a comment
saying `.env` is NOT loaded via `EnvironmentFile`. That comment is technically
true but misleading. Here's the real picture:

- **Location of the box's .env:** `/home/ubuntu/receptionist-agent/.env`
- **Loader:** `pydantic-settings` inside `app.core.config.Settings`, at Python
  import time, from `WorkingDirectory` (which the systemd unit sets to
  `/home/ubuntu/receptionist-agent/apps/api`, and pydantic-settings walks up
  looking for `.env`).
- **Consequence:** editing `.env` on the box + restarting the service DOES
  update settings. But the new values are NOT visible in `/proc/PID/environ`
  because they never enter the OS process env — they live inside the
  `settings` Python object.
- **Verifying a flag:** SSH to the box, then:
  ```bash
  cd /home/ubuntu/receptionist-agent/apps/api
  ../../.venv/bin/python -c 'from app.core.config import settings; print(settings.next_action_policy_enabled)'
  ```
  This reads what the app WILL see on next restart.
- **Flipping a flag** (e.g. `NEXT_ACTION_POLICY_ENABLED=true`):
  ```bash
  ssh -i LightsailDefaultKey-us-east-1.pem ubuntu@3.227.16.73
  # inline edit + verify:
  grep -n NEXT_ACTION_POLICY_ENABLED /home/ubuntu/receptionist-agent/.env
  # then either nano the line, or:
  sudo sed -i 's/^NEXT_ACTION_POLICY_ENABLED=.*/NEXT_ACTION_POLICY_ENABLED=true/' /home/ubuntu/receptionist-agent/.env
  exit
  # from your Mac:
  ./scripts/deploy.sh --skip-rsync
  ```
- **Why NOT via EnvironmentFile:** the systemd unit comment explains — some
  values contain `=` or spaces (JWTs, JSON blobs) that systemd's parser would
  truncate. pydantic-settings handles them cleanly.

## What breaks + how to fix it

### `PEM not found`
The Lightsail SSH key isn't at the default path. Point to yours:
```bash
LIGHTSAIL_PEM=/path/to/LightsailDefaultKey-us-east-1.pem ./scripts/deploy.sh
```

### `SSH to ubuntu@3.227.16.73 failed`
Either the box is down, the PEM is wrong, or your IP is blocked. Test:
```bash
ssh -i LightsailDefaultKey-us-east-1.pem ubuntu@3.227.16.73 'echo ok'
```

### `systemctl restart failed`
The systemd unit refused to restart (usually a Python syntax error in the new code). SSH in and read the journal:
```bash
ssh -i LightsailDefaultKey-us-east-1.pem ubuntu@3.227.16.73 'journalctl -u receptionist.service -n 100'
```

### `Health check FAILED after 8 attempts`
The service restarted but isn't responding on `/`. Two common causes:
1. Prompt cache warmup is slow today (>24s). The service will come up; wait 30s and hit the URL manually.
2. The new code has a runtime error. Same journalctl command as above.

Rollback:
```bash
git revert HEAD && git push
# OR revert on the box directly:
ssh -i LightsailDefaultKey-us-east-1.pem ubuntu@3.227.16.73 \
    'cd /home/ubuntu/receptionist-agent && git checkout HEAD~1 && sudo systemctl restart receptionist.service'
```

## Env vars the script respects

| Var | Default | What it does |
|-----|---------|--------------|
| `LIGHTSAIL_HOST` | `3.227.16.73` | Where to deploy |
| `LIGHTSAIL_USER` | `ubuntu` | SSH username |
| `LIGHTSAIL_PEM` | `<repo>/LightsailDefaultKey-us-east-1.pem` | SSH private key path |
| `HEALTH_URL` | `https://agent.eternalconquests.com/` | Where to poll for HTTP 200 |

## What the script deliberately does NOT do

- **Does not commit or push git**. You do that. This means you can hotfix without committing (useful for tight loops).
- **Does not touch secrets on Lightsail.** The `.env` file on the box is edited manually via SSH. This is a feature — deploy code without touching credentials.
- **Does not run tests before deploying.** Do that yourself (`pytest apps/api/tests/`) or trust CI.
- **Does not migrate databases.** Alembic runs on service startup, not on deploy. If a migration breaks the boot, the health check will catch it.
- **Does not do blue-green.** There's ~10s of downtime during systemctl restart. Acceptable until we have paying customers on the box.

## For other Claude sessions using this

When you need to ship a change from your own session:
1. `./scripts/deploy.sh --dry-run` first — confirms your session can reach Lightsail
2. If green, run without `--dry-run`
3. Health check output tells you if the code booted. If it fails, the runbook above tells you where to look.

Never modify `deploy.sh` without also updating `.github/workflows/deploy-lightsail.yml` and vice versa — they're a contract.

## What this doesn't scale to

- Multiple Lightsail boxes (blue-green, canary, per-region)
- Fargate (that's Track B — see `terraform/` when it exists)
- Client-owned AWS accounts (Track B, per-client Terraform module)

For any of those, use the Terraform module (task #92) when it lands.
