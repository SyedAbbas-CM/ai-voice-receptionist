# Bad-outcome catalog — {vertical}

Author: product-lead (subagent)
Vertical: {vertical}
Date: {yyyy-mm-dd}

## Purpose

Every way a call can fail from the CALLER's POV, ordered by severity
+ frequency. The system usually only detects "empty completion" and
"hangup" — real failures include mis-booked service, wrong provider,
missed insurance question, etc. This catalog is what turns silent
production failures into detected/counted signals.

## Severity scale

- **CRITICAL:** caller ends up harmed / betrayed. Trust destroyed.
- **HIGH:** caller has to call back / show up in vain / correct the mistake themselves. Wasted their time.
- **MEDIUM:** caller left with worse impression than needed. Booking succeeded but sloppy.
- **LOW:** minor cosmetic wobble. Booking succeeded, caller unbothered.

## Catalog

### 1. {Failure name} — CRITICAL / HIGH / MEDIUM / LOW

**What it looks like from the caller's POV:** ...

**What went wrong internally:** ...

**Detection signal:** how could our observability catch this?
- Existing humanness_events kind: {event_kind or "NONE — need new"}
- Missing: {what event class would need to exist}

**Prevention rule:** where in the code would we add a guard?
- File / function: ...
- Approach: ...

**Recovery script:** what the ideal agent says when this happens.
- (See golden-scripts.md → failure-recovery script #X)

### 2. {next failure}
[repeat...]

## Cross-linking

| Failure | Persona most affected | Detection event | Recovery script |
|---|---|---|---|
| Wrong service booked | New patient | LlmClaimGuardEvent + ??? | Fail-2 |
| Missed emergency triage | Pain caller | POLICY_DECISION action mismatch | Fail-5 |
| ... | ... | ... | ... |

## Recommendations for engineering

- New humanness event classes needed (list them so engineers can add to `packages/observability/humanness_events.py`):
  - `MisBookedServiceEvent` — detected post-booking, compares booking.service vs conversation-inferred intent
  - `EmergencyMissTriggerEvent` — detected when caller utterance matched an emergency-signal pattern but agent proceeded to normal booking
  - `InsuranceCheckSkippedEvent` — detected when booking-for-service-with-known-insurance-variance completed without an insurance query
- Bad-outcome dashboard widget — extend `/trace/{call_id}` to surface each detected failure with severity, so business owners see them.
- Alert on CRITICAL — Slack/email/PagerDuty when a critical failure fires in prod.
