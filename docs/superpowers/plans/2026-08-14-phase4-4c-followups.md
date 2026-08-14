# PHASE 4c — Missed-Call Textback + Follow-ups + Confirmations

**Goal:** the receptionist keeps the caller warm even when it couldn't do the job. If the phone was busy → SMS. If the caller hung up mid-flow → SMS. Post-booking → SMS confirmation. Day-of → SMS reminder.

**Position:** commercial polish. Doc #56 line 158. Cheap to build; high-perception win for clients.

## Prereqs
- PHASE4a done (real bookings exist to remind about)
- Existing SMS channel wire-up (`packages/channels/`) works

## Global Constraints
- **SMS sender is per-tenant.** Twilio number per business; some tenants may swap to their own Twilio account (Sprint 6 auth infra supports this).
- **Consent gate applies.** Sprint 6 built `packages/compliance/` — check consent before every outbound SMS. TCPA compliance.
- **All templates are per-tenant configurable.** Ship default templates; tenants can override.
- **Idempotency across retries.** Same event → same message once. Store hash of (tenant_id, phone_e164, event_type, event_id).
- **Rate limit outbound SMS per number per hour.** Prevents accidental spam if a bug loops.

## File Structure

- `packages/followups/base.py` — `FollowupEvent` types + `FollowupSender` protocol
- `packages/followups/missed_call.py` — busy / unanswered → textback
- `packages/followups/mid_call_abandon.py` — caller hung up while agent was thinking
- `packages/followups/booking_confirmation.py` — post-booking SMS
- `packages/followups/reminder_24h.py` — 24h reminder
- `packages/followups/reminder_2h.py` — 2h reminder
- `packages/followups/scheduler.py` — cron-like runner that fires time-based events
- `packages/followups/templates/` — Jinja templates per event, per language
- `apps/api/tests/test_followups_*.py`

## Task 1: FollowupEvent + sender protocol
- [ ] Define `FollowupEvent` (kind, tenant_id, phone_e164, payload, dedupe_key)
- [ ] `FollowupSender` protocol — one method: `send(event) -> SendResult`
- [ ] Default sender: `TwilioSMSSender` (Twilio Programmable SMS)
- [ ] Test with mock Twilio
- [ ] Commit

## Task 2: Missed-call textback (busy + unanswered)
- [ ] Twilio webhook config: dial-completed with `CallStatus=busy` OR `no-answer` → POST to `/webhooks/missed-call`
- [ ] Handler: build `FollowupEvent(kind="missed_call")` → sender
- [ ] Template: "Sorry we missed you. Reply with what you need and we'll get back to you." (per-tenant)
- [ ] Test with mock Twilio webhook payload
- [ ] Commit

## Task 3: Mid-call abandon detection
- [ ] Actor emits `CALLER_ABANDONED` event when: caller hangs up AND state was mid-workflow (booking not committed)
- [ ] Handler: `FollowupEvent(kind="abandon")` → sender
- [ ] Template: "Looks like we got cut off. Want to pick up where we left off? Reply YES."
- [ ] Test — simulate hangup mid-booking, verify event fires
- [ ] Commit

## Task 4: Booking confirmation SMS
- [ ] Hook: on successful `book_appointment` receipt (via existing sink infrastructure)
- [ ] Template: "Confirmed: {service} on {date} at {time}. Reply RESCHEDULE to change or CANCEL."
- [ ] Test
- [ ] Commit

## Task 5: Time-based reminders (24h + 2h)
- [ ] `packages/followups/scheduler.py` — runs every 5 min; queries calendar for upcoming bookings; fires events at 24h and 2h marks
- [ ] Dedupe via `(booking_id, kind)` — never send twice
- [ ] Templates per tenant
- [ ] Test with mocked time (freeze_time)
- [ ] Commit

## Task 6: Consent gate integration
- [ ] Before every send: `packages/compliance/consent.py` check
- [ ] On no-consent: log SKIP + reason; don't send
- [ ] Test — sender skips when consent record missing
- [ ] Commit

## Task 7: Rate limiter per number per hour
- [ ] SQLite-backed counter, TTL 1h
- [ ] Reject if would exceed 5 SMS/hour to same E.164
- [ ] Test — burst 10 sends, verify 5 succeed 5 rejected
- [ ] Commit

## Task 8: Inbound-SMS reply handling
- [ ] Webhook: `POST /webhooks/sms-inbound` from Twilio
- [ ] If matches dedupe_key of recent outbound (e.g. "YES" to abandon prompt) → route to a resume-workflow handler
- [ ] Test with canned "YES" / "CANCEL" / "RESCHEDULE" replies
- [ ] Commit

## Task 9: 50-event soak
- [ ] Simulate 50 followup events across 5 tenants (missed / abandon / confirmation / reminder)
- [ ] Verify: all delivered, none duplicated, rate limits held, consent enforced
- [ ] Verify against Twilio test credentials

## Task 10: Close-out
- [ ] `docs/rnd-2026-08/70-phase4-4c-close.md`
- [ ] Doc #58: PHASE4 fully done, PHASE5 unblocked

## Success criteria
- All 5 followup event kinds live
- Consent enforced on 100% of outbound
- Rate limit prevents any tenant burst
- Inbound-SMS reply routes correctly to resume workflows
- 50-event soak: ≥98% delivery, 0 duplicates
