# AWS deployment requirements brief

Author: voice-agent (humanness lane)
Owner of implementation: networking lane
Date: 2026-08-29

## Scope

What the receptionist-agent service needs from AWS to run in production. This is NOT the terraform module. This is what the terraform module has to provision. Networking writes the actual infra code.

The user asked for "auto deploy from here with a script." Answer: yes, and the right shape is a Terraform module + a `make deploy` target that fronts it. This doc gives networking everything they need to write both.

## Traffic profile

- Twilio Media Streams over WebSocket + a modest REST surface (webhooks, dashboards).
- Call latency budget: <150ms wall-clock overhead from network hop. This forces the service into the same region as the primary Twilio media edge for the target market (US callers → us-east-1, EU callers → eu-west-1).
- Peak concurrent calls in demo/pilot: 5. In production year one: 50. Long-term (year two, if pipeline #3 hits): 500.
- **Cannot use Lambda.** WebSocket + streaming LLM + long-lived state (up to 10 min per call) is not a Lambda shape. Ignore anyone who suggests it.
- Not spiky — call arrivals are Poisson-ish across business hours. Auto-scale on request count, not on CPU.

## Recommended shape

**ECS Fargate** behind an ALB, single service, single container image, auto-scaling on `ALBRequestCountPerTarget` metric.

Reasons:
- Fargate = no EC2 management, no AMI drift, we ship a container.
- ALB terminates TLS + upgrades WebSocket cleanly.
- Auto-scale target-tracking against request count is more predictable than CPU for a call-audio workload.
- ECS task role gives us clean per-service IAM without secrets in env vars.

**Not App Runner.** App Runner does NOT support WebSocket upgrade cleanly at the time of writing. Confirmed limitation — do not attempt.

**Not EKS.** Overkill for one service. Cost + operational surface not justified until we run multiple services.

## Required infrastructure

### Networking

- VPC with 2 public subnets (ALB) + 2 private subnets (Fargate tasks + RDS).
- NAT Gateway for outbound (calls to OpenAI, Deepgram, ElevenLabs, Twilio REST).
- One ALB, HTTPS listener on 443 with an ACM cert.
- Health check: `GET /health` on the task port (see below). 30s interval, 5s timeout, 2 healthy / 3 unhealthy.

### Compute

- **Fargate task definition** with:
  - `awsvpc` network mode.
  - CPU: **2048 (2 vCPU)**. RAM: **4096 MB**.
    - Rationale: startup does prompt cache warm + Deepgram DNS+TLS handshake + response cache warm + 25 TTS cache entries per channel. Measured startup RSS is ~1.2 GB. Headroom for concurrent calls.
  - Ephemeral storage: 21 GB (Fargate minimum + our TTS cache + call_events.db working set).
  - One container.
- **Auto-scaling**:
  - Min tasks: 2 (never single-point-of-failure a live call receptionist).
  - Max tasks: 10 (protects against runaway auto-scale burning provider credits).
  - Target: 30 requests/target/minute. Scale-out cooldown 60s, scale-in cooldown 300s (call durations up to 10 min, don't kill live tasks).
  - Circuit breaker: enable ECS deployment circuit breaker so failed deploys auto-rollback.

### Container registry

- ECR private repo `receptionist-agent`.
- Image tag = git SHA. `latest` is a moving pointer for convenience but the task def pins to SHA.
- Lifecycle policy: keep last 20 images, drop everything else.

### Persistent state

- **RDS PostgreSQL 16**, `db.t4g.small` for pilot, `db.t4g.medium` for year one.
  - Reason we need real Postgres, not SQLite: multi-task Fargate service can't share a SQLite file. Every existing SQLite usage (`data/receptionist.db`, `data/call_events.db`, `data/response_cache.db`) needs a migration story.
  - Alembic migrations run on task startup (see startup order below).
  - Multi-AZ off for pilot, on for prod.
- **S3 bucket `receptionist-tts-cache`** for the TTS cache. Mount via env var pointing task at bucket + prefix. Cache read-through + write-through on first miss.
  - Alternative: EFS mount into the Fargate task. Slower cold-read than S3 but no S3 API calls per cache hit. Networking decides.
- **S3 bucket `receptionist-call-recordings`** if we ever enable Twilio call recording. Not enabled at pilot.

### Secrets

Use **AWS Secrets Manager**, not env vars, for anything sensitive. Task IAM role reads them at boot.

Required secrets (one Secrets Manager secret per group is fine):

- `receptionist/llm` → `OPENAI_API_KEY`, `GROQ_API_KEY`, `GEMINI_API_KEY` (fallback), `OPENROUTER_API_KEY` (fallback)
- `receptionist/stt-tts` → `DEEPGRAM_API_KEY`, `ELEVENLABS_API_KEY`
- `receptionist/twilio` → `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_APP_SID`
- `receptionist/tenant-keys` → `API_KEYS_JSON` (the bootstrap tenant keys until Sprint 6j DB-issued keys fully replace them)
- `receptionist/session` → `SHORT_TICKET_SECRET` (32 bytes hex — needed for the Twilio WSS ticket flow + dashboard signed-session flow. `openssl rand -hex 32`.)
- `receptionist/db` → RDS master password (managed by Secrets Manager rotation).
- `receptionist/crm` → `HUBSPOT_API_KEY`, `GHL_API_KEY`, `PIPEDRIVE_API_TOKEN` (any that are actually configured).
- `receptionist/sms-email` → `TWILIO_MESSAGING_SERVICE_SID`, `SENDGRID_API_KEY` (if configured).

**Do not put any of these into environment variables in the task definition.** Task IAM must have `secretsmanager:GetSecretValue` on the specific secret ARNs only.

### Environment variables (non-secret config)

These CAN live in the task definition environment block:

- `ENVIRONMENT=production`
- `API_AUTH_ENFORCE=true`
- `AWS_REGION=us-east-1` (or eu-west-1)
- `DATABASE_URL=<constructed from Secrets Manager RDS creds at boot>`
- `CALL_EVENT_LOG_PATH=postgresql://...` (once the event log migrates off SQLite)
- `TTS_CACHE_S3_BUCKET=receptionist-tts-cache`
- `TTS_CACHE_S3_PREFIX=v1/`
- `RESPONSE_CACHE_S3_BUCKET=receptionist-tts-cache` (share bucket, different prefix `response-v1/`)
- `PORT=8000` (container listens here; ALB forwards)
- `DASHBOARD_ALLOW_TOKEN_IN_URL=false` (production forces query-token off regardless, but explicit anyway)
- `NEXT_ACTION_POLICY_ENABLED=true` (flag was default-off in the config; landing this activates the wired policy directive path)
- `OBSERVABILITY_API_ENABLED=false` (leave /debug/* off in prod — /trace/* is the tenant path)
- `LOG_LEVEL=INFO`
- `PYTHONUNBUFFERED=1`

### Networking + observability

- **CloudWatch Logs group**: `/receptionist/api` with 30-day retention.
- Structured logs already emit JSON when `ENVIRONMENT=production` — logs will parse cleanly.
- **Prometheus**: the `/metrics` endpoint exists. Two options:
  1. Push via ADOT sidecar to CloudWatch or Prometheus-managed.
  2. Just scrape via CloudWatch Container Insights.
  - Networking picks. Task #136 (my open task) requires `/metrics` NOT be public — it currently is. Whichever option networking chooses must not expose `/metrics` on the internet-facing ALB. Same-VPC scrape only.

## Startup sequence (must complete in this order)

The task container's entrypoint does this:

1. Load secrets from Secrets Manager → env. Fail-loud if any required secret is missing.
2. Run `alembic upgrade head` against RDS. Fail-loud if migrations fail.
3. Warm the caches. This is DEFENSIVE: the app's own `startup` handlers do it, but if warming fails we still want the task to become healthy so the ALB can round-robin.
4. Bind port 8000, start uvicorn.

Startup measured wall-clock: **8-12 seconds** (LLM prompt cache warm + response cache warm + TTS cache warm + Deepgram DNS+TLS handshake).

**Set the ALB health-check grace period to 90 seconds** on the ECS service. Anything less and the ALB will register the task unhealthy before it finishes warming.

## Health check contract

- `GET /health` → 200 with `{"status": "ok"}` once uvicorn is up AND alembic ran cleanly.
- The service does NOT wait for LLM/TTS provider connectivity to answer healthy. Providers can be flaky and we still want the task in service — provider failures are handled at request time.

## Deploy flow (what "auto deploy from here" means)

Networking implements this as a `make deploy` target:

```bash
make deploy   # → scripts/deploy.sh
```

Which does:

1. `git rev-parse --short HEAD` → tag.
2. Build container: `docker build -t receptionist-agent:$SHA .`
3. Auth: `aws ecr get-login-password | docker login`
4. Push: `docker tag receptionist-agent:$SHA $ECR_URI:$SHA && docker push $ECR_URI:$SHA`
5. Terraform apply with `image_tag=$SHA` variable → updates task definition + triggers ECS rolling deploy.
6. `aws ecs wait services-stable` on the service.
7. Report deployment URL + tag.

**The terraform state lives in a state-only S3 bucket + DynamoDB lock table.** Standard pattern.

## Rollback

- ECS deployment circuit breaker + `enable_execute_command=true` gives us:
  - Auto-rollback on failed deploy.
  - `aws ecs execute-command` to shell into a task for post-mortem.
- Manual rollback: `make deploy TAG=<previous-sha>` re-runs the deploy with an older image tag.

## Cost estimate

Pilot (2 tasks always on, 1 ALB, 1 t4g.small RDS, ~5 GB S3, ~50 GB CW logs/mo):

- Fargate: 2 tasks × 730h × ($0.04048 vCPU-hr + $0.004445 GB-hr) = ~$76/mo compute
- ALB: $16.20/mo base + LCU (~$5/mo at pilot volume) = ~$21/mo
- RDS db.t4g.small: ~$25/mo
- S3: ~$1/mo
- CloudWatch: ~$5/mo
- Secrets Manager: ~$2.50/mo (5 secrets × $0.40 + API calls)
- NAT Gateway: **$32/mo + $0.045/GB**. Real number for a chatty service like ours could be $50-80/mo just for LLM/STT/TTS outbound. Consider VPC endpoints for S3/ECR/Secrets Manager to drop NAT traffic.

**Pilot AWS bill: ~$150-200/month before provider (OpenAI/Deepgram/ElevenLabs/Twilio) costs.**

## What networking still owns

Everything infra. Specifically:

1. Terraform module implementing all of the above.
2. Docker + entrypoint script per startup sequence.
3. GitHub Actions (or user-run `make deploy`) that invokes the deploy flow.
4. Migration story for SQLite → Postgres for the three DBs I named. This is the SINGLE largest blocker to a real deploy — the code assumes local SQLite paths.
5. The task #133 TenantRuntimeContextResolver — because multi-task Fargate means every task must resolve tenant context per-request identically. Cross-task drift = correctness bug.
6. `/metrics` gating fix (my task #136) is trivially resolvable at ALB level: don't route `/metrics` externally.

## What voice-agent (me) still owns

- The app-layer things this infra requires:
  - S3-backed TTS cache adapter (right now cache is filesystem-only). Small — I can land this next batch if you want.
  - S3-backed response cache adapter (same).
  - Postgres event-log adapter (call_event_log currently SQLite-only).
- Making sure `settings.dashboard_allow_token_in_url` is forced False in prod (already done — verify).
- Prompt/LLM cost efficiency work — separate lane, not this doc.

## Open questions for networking

1. Region choice: US-first (us-east-1) or dual-region from day one? Dual doubles infra cost but halves EU latency.
2. Twilio Media Streams supports us-east-1 + us-west-2 + eu-west-1 currently. Any preference?
3. Do we want ADOT/OpenTelemetry sidecar for traces, or CloudWatch alone?
4. RDS Aurora Serverless v2 vs. plain RDS Postgres for pilot — cost trade at low load favors plain RDS, but Aurora auto-scales cleaner. Networking's call.
5. Timeline — is this a "spec now, implement next sprint" or "start terraform this week"?

Answer any of these and I'll iterate. Otherwise this is enough for networking to write the module.
