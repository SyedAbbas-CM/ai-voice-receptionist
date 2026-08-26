# Webhook Event Schema — Canonical Contract

**Version 1.0 — 2026-08-26**
**Sink:** `packages/integrations/sinks.py::WebhookSink`
**Client:** `packages/integrations/webhook_client.py::WebhookClient`

This is the contract we deliver to tenants who wire our voice agent into their n8n / Make / Zapier / custom workflow platform. Copy the relevant sections into your handover doc; tenants show it to their integration developer.

---

## 1. Overview

We POST JSON to your configured URL on every business event. Delivery is best-effort with 4-attempt retry (jittered exponential backoff, honors Retry-After). Each request carries an HMAC-SHA256 signature so you can verify authenticity + block replay attacks.

**You do NOT need to poll our system.** We push. Your workflow triggers on our POST.

---

## 2. Configuration

**Tenant-side (n8n / Make / Zapier):**
1. Create a Webhook trigger node in your workflow
2. Set method: `POST`, content type: `application/json`
3. Copy the trigger URL — this is what you give us

**Us-side (during onboarding):**
1. Set `WEBHOOK_URL=<the tenant's trigger URL>` in the tenant's config
2. Generate a shared secret: `openssl rand -hex 32`
3. Set `WEBHOOK_SECRET=<the hex string>` in the tenant's config
4. Share the same hex secret with the tenant so their workflow can verify our signatures

---

## 3. Request format

**URL:** `POST <tenant's configured URL>`

**Headers:**
```
Content-Type: application/json
User-Agent: voiceops-ai-agent/1.0
X-VoiceOps-Signature: t=<unix_epoch>,v1=<hmac_sha256_hex>
X-VoiceOps-Idempotency-Key: <string>
X-VoiceOps-Event-Type: <event name>
```

**Body:** JSON envelope (see section 4).

---

## 4. Envelope shape

Every event is wrapped in this envelope:

```json
{
  "event": "<event name>",
  "source": "voiceops-ai-agent",
  "timestamp": 1756253400,
  "idempotency_key": "booking:twilio_CA123:book_appointment",
  "data": {
    "...event-specific fields..."
  }
}
```

- `event` — one of the event types in section 5
- `source` — always the string identifying us
- `timestamp` — Unix epoch seconds (integer)
- `idempotency_key` — deterministic per business event. If we retry a request that already succeeded on your side (e.g. our network dropped before we saw your 200), you'll see the same key. Store keys, skip duplicates.
- `data` — event-specific payload (see section 5)

---

## 5. Event types

### 5.1 `booking.created`

Fires when the agent successfully books an appointment via any `book_*` tool.

```json
{
  "event": "booking.created",
  "source": "voiceops-ai-agent",
  "timestamp": 1756253400,
  "idempotency_key": "booking:sess-abc:book_appointment",
  "data": {
    "tenant_id": "tenant-xyz",
    "business_id": "biz-abc",
    "session_id": "sess-abc",
    "call_sid": "CA1234...",
    "caller_name": "Sarah Chen",
    "phone": "+15551234567",
    "email": null,
    "service": "cleaning",
    "start_iso": "2026-08-28T14:30:00",
    "duration_minutes": 45,
    "notes": null,
    "tool_name": "book_appointment",
    "booked_at": null
  }
}
```

**Fields that may be null:** `email`, `notes`, `duration_minutes`, `call_sid` (null for non-Twilio callers e.g. web widget). Every other field is populated when available; use n8n's `IF` node to branch on presence.

### 5.2 `call.completed`

Fires when the call ends for any reason (voluntary hangup, farewell, idle timeout, escalation).

```json
{
  "event": "call.completed",
  "source": "voiceops-ai-agent",
  "timestamp": 1756253600,
  "idempotency_key": "call_end:sess-abc",
  "data": {
    "tenant_id": "tenant-xyz",
    "business_id": "biz-abc",
    "session_id": "sess-abc",
    "call_sid": "CA1234...",
    "status": "completed",
    "caller_name": "Sarah Chen",
    "phone": "+15551234567",
    "intent": "book_appointment",
    "urgency": "medium",
    "lead_score": 80,
    "summary": "Booked cleaning for Tuesday at 2:30",
    "escalation_reason": null
  }
}
```

- `status` is one of `active | completed | escalated | abandoned`
- `intent` is the classified intent (`book_appointment | reschedule | cancel | faq | emergency | handoff | other`)
- `urgency` is `low | medium | high`
- `lead_score` is 0-100 heuristic

### 5.3 `missed_call` (planned, not yet emitted)

Will fire when Twilio reports `CallStatus=no-answer | busy | failed` on `/twilio/status`. Not yet wired — call this endpoint yourself if you need it today, or wait for the missed-call branch to ship.

### 5.4 `message.taken` (planned, not yet emitted)

Will fire when the `take_message` tool completes. Requires the TakeMessage feature which is on the humanness roadmap.

### 5.5 `transfer.requested` (planned, not yet emitted)

Will fire when `escalate_to_human` tool completes. Payload will include the escalation reason and any specific agent-name the caller requested.

---

## 6. Signature verification

Every request carries `X-VoiceOps-Signature: t=<timestamp>,v1=<sig>`.

**Verification pseudo-code (any language):**

```python
import hmac, hashlib, time

def verify(secret, signature_header, raw_body, max_age_seconds=300):
    parts = dict(p.split("=", 1) for p in signature_header.split(","))
    ts = int(parts["t"])
    provided_sig = parts["v1"]
    # Anti-replay: reject old / future-dated requests
    if abs(int(time.time()) - ts) > max_age_seconds:
        return False
    expected = hmac.new(
        secret.encode(),
        f"{ts}.{raw_body}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, provided_sig)
```

**In n8n:**
1. Add a Function node right after the Webhook trigger
2. Paste the equivalent JavaScript
3. Reject if verify returns false

**In Make.com:**
1. Add a Tools → Set variable step to compute the expected signature
2. Filter step compares expected vs actual using `constant-time` comparison

We publish a full n8n workflow verifying signatures as part of the handover kit.

---

## 7. Retry behavior

We retry on `408 | 429 | 500 | 502 | 503 | 504` and network errors. Up to 4 attempts total (initial + 3 retries). We honor `Retry-After` headers.

**What YOU should do:**
- Return `2xx` as fast as you can (before doing heavy work) — do the work asynchronously in a downstream node.
- If you MUST reject: return `4xx` (non-retryable — 400/401/403/404) if the payload is broken and we shouldn't retry.
- Return `503` with `Retry-After: N` if you're overloaded — we'll wait N seconds.

**Idempotency:** we may deliver the same `idempotency_key` more than once if our network drops before seeing your 200. Store the last 24h of keys and skip duplicates. This is the standard pattern for Stripe / Twilio / any signed webhook.

---

## 8. Testing your integration

Use our test route:
```
POST https://agent.eternalconquests.com/public/webhooks/verify
Content-Type: application/json
X-VoiceOps-Signature: t=...,v1=...
X-VoiceOps-Idempotency-Key: test-...
X-VoiceOps-Event-Type: booking.created

<envelope body>
```

Returns `200 {"verified": true}` if the signature matches, `401` if not. Lets you test your verification code without needing a real call.

---

## 9. Versioning

- **Adding a field is non-breaking.** Ignore unknown fields.
- **Removing or renaming a field is breaking.** We bump the version + notify tenants 30 days in advance.
- **Envelope shape is frozen.** New event types can be added without version bump.

Current version: `1.0` (2026-08-26).

---

## 10. Support

Email: `<tenant onboarding contact>`
Doc source: `docs/WEBHOOK-EVENT-SCHEMA.md` in the platform repo.
Client library: `packages/integrations/webhook_client.py::WebhookClient` (see `WebhookClient.verify_signature` for the reference implementation).
