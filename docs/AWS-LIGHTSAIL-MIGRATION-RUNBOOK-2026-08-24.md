# AWS Lightsail us-east-1 Migration Runbook

**Date:** 2026-08-24
**Target:** Move voice-agent from Karachi laptop → AWS Lightsail Virginia
**Expected impact:** felt latency 2.5-3s → 1.5-2s
**Total time:** ~90-120 minutes if you know AWS console, ~2-3 hours first-time
**Zero code changes.** Same repo, same env, moved location.

## Phase 0 — Verify current state before starting

```bash
# On your Karachi Mac, current server:
ps aux | grep uvicorn | grep -v grep
# Expected: PID 80802 (or whatever the latest bounce PID is)
```

**Don't stop the Karachi server yet.** Keep it running until Lightsail is verified.

## Phase 1 — Provision Lightsail (10 min)

1. Log in to AWS Console → Search **Lightsail** → open the service
2. Click **Create instance**
3. **Instance location:** us-east-1 (N. Virginia), Zone A (any zone is fine)
4. **Platform:** Linux/Unix
5. **Blueprint:** OS Only → **Ubuntu 24.04 LTS**
6. **Instance plan:** **$10/month** (2 GB RAM, 2 vCPU, 60 GB SSD, 3 TB transfer)
   - Do NOT go cheaper — the $5 plan has 512MB RAM and Python + our deps will OOM under load
   - $10 is the sweet spot
7. **Identify:** name it `voice-agent-va` or similar
8. Click **Create instance**
9. Wait ~60 seconds for the instance to boot
10. Click the instance → note the **Public IPv4** (call it `<LIGHTSAIL_IP>`)
11. **Networking tab** → make sure port 22 (SSH) is open (default). Port 8000 doesn't need to be exposed — Cloudflare Tunnel handles that.

## Phase 2 — SSH in + install deps (25 min)

```bash
# In Lightsail console, click "Connect using SSH" for browser SSH
# OR: download the default key from Account → SSH keys, then:
ssh -i ~/Downloads/LightsailDefaultKey-us-east-1.pem ubuntu@<LIGHTSAIL_IP>
```

Once in:
```bash
# System packages
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3.11 python3.11-venv python3-pip git build-essential \
    libffi-dev libssl-dev pkg-config libsndfile1 ffmpeg curl

# Verify Python version
python3.11 --version
# Expected: Python 3.11.x (Ubuntu 24.04 ships 3.12 by default; we need 3.11)
# If 3.11 isn't available: sudo add-apt-repository ppa:deadsnakes/ppa -y && sudo apt update && sudo apt install -y python3.11 python3.11-venv
```

## Phase 3 — Get the code + secrets there (15 min)

Two options: git clone (if repo is pushed to a remote) or SCP the whole tree.

**Option A: git clone (preferred if repo has a remote):**
```bash
cd ~
git clone <YOUR_GIT_REMOTE> receptionist-agent
cd receptionist-agent
git status  # verify it's clean or matches your Karachi tree
```

**Option B: SCP tarball (if not pushed):**
```bash
# On Karachi Mac:
cd "/Users/az/Desktop"
tar --exclude='Receptionist Agent/.venv' \
    --exclude='Receptionist Agent/.git' \
    --exclude='Receptionist Agent/data/logs' \
    --exclude='Receptionist Agent/apps/api/data/logs' \
    --exclude='Receptionist Agent/output' \
    --exclude='Receptionist Agent/data/models' \
    --exclude='Receptionist Agent/checkpoints' \
    -czf receptionist-src.tar.gz "Receptionist Agent/"

scp -i ~/Downloads/LightsailDefaultKey-us-east-1.pem receptionist-src.tar.gz ubuntu@<LIGHTSAIL_IP>:~/

# On Lightsail:
cd ~
tar -xzf receptionist-src.tar.gz
mv "Receptionist Agent" receptionist-agent
cd receptionist-agent
```

**Copy .env separately** (secrets shouldn't hit git or tarballs):
```bash
# On Karachi Mac:
scp -i ~/Downloads/LightsailDefaultKey-us-east-1.pem "/Users/az/Desktop/Receptionist Agent/.env" ubuntu@<LIGHTSAIL_IP>:~/receptionist-agent/.env
```

## Phase 4 — Install Python deps (20-30 min, longest step)

```bash
# On Lightsail:
cd ~/receptionist-agent
python3.11 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip setuptools wheel
pip install -r apps/api/requirements.txt
# Wait ~15-20 min for torch/onnx/etc to install
# If it hangs: pip install with --no-cache-dir to avoid Lightsail's slower disk
```

**Verify:**
```bash
.venv/bin/python3 -c "import fastapi, uvicorn, httpx, websockets, deepgram; print('deps OK')"
```

## Phase 5 — Test start the server (5 min)

```bash
# From ~/receptionist-agent
bash apps/api/scripts/run_server.sh &
sleep 10
tail -30 apps/api/data/logs/uvicorn-latest.log
```

**Expect to see:**
- `Started server process [<PID>]`
- `LLM_CALL site=brain.warmup provider=openai model=gpt-4o-mini elapsed_ms=<VERY_SMALL>`
  - **Critical:** warmup from Virginia to OpenAI Iowa should be **100-200ms** (was 800-2000ms from Karachi)
- `Uvicorn running on http://0.0.0.0:8000`

**If warmup is <300ms, you've won.** That's the ~1s speedup showing up.

Stop the Karachi server now if you want a clean cutover, OR keep it running for the A/B — pick one.

## Phase 6 — Cloudflare Tunnel (15 min)

You currently have a Cloudflare tunnel from Karachi laptop → your public URL. You have two options:

**Option A: Rename the Karachi tunnel and start fresh:**
- In Cloudflare Zero Trust → Access → Tunnels
- Delete the Karachi tunnel OR rename it (`voice-karachi-archived`)
- Create new tunnel `voice-va-lightsail`
- Copy the `cloudflared` install command it gives you (looks like `curl -L https://... | sudo bash`)
- Run on Lightsail
- Route it to `http://localhost:8000`
- Get the new public URL (e.g. `voice-va.your-domain.workers.dev` or similar)

**Option B: Reuse the existing tunnel token (faster):**
- Get the tunnel token from your Karachi Cloudflare dashboard
- Install cloudflared on Lightsail: `curl -L --output cloudflared.deb https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64.deb && sudo dpkg -i cloudflared.deb`
- Run: `sudo cloudflared service install <TOKEN>`
- Same public URL, just points to Lightsail now
- Stop cloudflared on Karachi Mac

## Phase 7 — Update Twilio webhook (2 min)

If Option 6-A (new URL): Twilio Console → Phone Numbers → your voice number → Voice Configuration → change the webhook URL to the new tunnel URL.

If Option 6-B (same URL): nothing to change.

## Phase 8 — Test call (5 min)

**Dial the number.** Watch the Lightsail log:
```bash
tail -f ~/receptionist-agent/apps/api/data/logs/uvicorn-latest.log
```

**What to look for:**
- `CALL_START_PROMPT` appears when call connects
- `LLM_STREAM_START` fires — check elapsed_ms values
- **Compare to your Karachi baseline:**
  - Karachi turn: LLM_FIRST_TEXT ~800-1000ms
  - Virginia turn: **should be 100-300ms**
- `TTS_FIRST_BYTE first_byte_ms` should also drop from ~260ms → ~50-100ms

**Count fingers.** Should feel dramatically snappier.

## Phase 9 — Set up systemd for auto-restart (10 min, do later)

Not urgent for the demo, but do it before shipping:
```bash
sudo tee /etc/systemd/system/receptionist.service > /dev/null <<'EOF'
[Unit]
Description=Receptionist Voice Agent
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/receptionist-agent
ExecStart=/home/ubuntu/receptionist-agent/apps/api/scripts/run_server.sh
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable receptionist
sudo systemctl start receptionist
sudo systemctl status receptionist
```

## Rollback plan (if things break)

1. Stop cloudflared on Lightsail: `sudo systemctl stop cloudflared`
2. Start cloudflared back on Karachi Mac
3. If webhook URL changed in Phase 7: change it back in Twilio Console
4. Old Karachi server picks up next call

Everything reversible in ~5 min if something goes wrong.

## Monthly cost breakdown

- Lightsail 2GB/2vCPU/60GB/3TB Virginia: **$10/mo**
- Cloudflare Tunnel: free
- Everything else (Twilio, OpenAI, Deepgram, EL) unchanged

**Net additional: $10/mo.** Worth it for the felt-latency drop.

## What to check on the first Lightsail call

Grep for these log lines after the call:

```bash
# LLM RTT — should be MUCH lower
grep "LLM_STREAM_DONE" ~/receptionist-agent/apps/api/data/logs/uvicorn-latest.log | tail -5
# total_ms should be ~200-400ms (was ~1000-1400 from Karachi)

# TTS first byte — should also drop
grep "TTS_FIRST_BYTE" ~/receptionist-agent/apps/api/data/logs/uvicorn-latest.log | tail -5
# first_byte_ms should be ~50-100 (was ~260 from Karachi)

# POST_EOT_HOLD (metric fix I just shipped)
grep "POST_EOT_HOLD" ~/receptionist-agent/apps/api/data/logs/uvicorn-latest.log | tail -5
# Should show real post_eot_ms values now (was -1 due to Flux bug I also fixed)

# Twilio wire — probably still ~300ms because your phone is in PK
grep "TWILIO_FIRST40_ACK" ~/receptionist-agent/apps/api/data/logs/uvicorn-latest.log | tail -5
# send_to_ack_ms unchanged (this is US↔PK carrier, unaffected by server move)
```

**Expected total felt latency after Lightsail:** 1.5-2s. If it's still 3s, the bottleneck is the PK↔Twilio wire (not the server), and next step is Telnyx Dubai anchorsite.

## Ready to start?

**When you're ready:**
1. Provision the Lightsail instance in the AWS console (Phase 1)
2. Copy me the public IP + tell me it's ready
3. I'll walk you through Phases 2-8 step by step in real-time
4. First test call from Lightsail: ~90-120 min from now
