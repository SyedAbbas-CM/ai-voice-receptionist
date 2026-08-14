# PHASE 4a — Booking Completeness + Real Calendar

**Goal:** the receptionist can do everything a real receptionist does with bookings — not just first-time create. Reschedule, cancel, waitlist, no-show recovery, reminders, and it does it against a REAL calendar, not FakeCalendar.

**Position:** first commercially-critical sub-phase. Doc #56 line 154. Blocks any client demo that needs actual bookings.

## Prereqs (blocking)
- PHASE2 (kernel owns Workflow lane) shipped
- One paying client target identified (soft blocker — informs which calendar backend goes first)

## Global Constraints
- **FakeCalendar stays** as the test backend. Never delete.
- **Calendar backend is pluggable.** Interface: `CalendarBackend` protocol with `list_slots`, `book`, `reschedule`, `cancel`, `find_by_phone`. Same shape as `FakeCalendar` already has.
- **Idempotency is non-negotiable.** Every write carries `idempotency_key`; retries never double-book. Doc #56 line 156.
- **Time zones are per-tenant.** Business timezone comes from `BusinessProfile.timezone`. Caller-local time gets converted before speaking.
- **Waitlist + no-show recovery are OPT-IN per tenant.** Defaults off — some businesses would find them intrusive.

## File Structure

- `packages/integrations/calendar/base.py` — `CalendarBackend` protocol
- `packages/integrations/calendar/fake.py` — rename existing FakeCalendar, extract from fake_calendar.py
- `packages/integrations/calendar/google.py` — Google Calendar backend
- `packages/integrations/calendar/cal_com.py` — Cal.com backend
- `packages/integrations/calendar/router.py` — pick backend from `BusinessProfile.calendar_provider`
- `packages/dialogue/workflows/booking_full.py` — extends WorkflowController for reschedule/cancel/waitlist
- `apps/api/tests/test_calendar_google.py`
- `apps/api/tests/test_calendar_cal_com.py`
- `apps/api/tests/test_booking_full_workflows.py`

## Task 1: Extract CalendarBackend protocol
- [ ] Define protocol in `packages/integrations/calendar/base.py`
- [ ] Move existing `FakeCalendar` to `packages/integrations/calendar/fake.py` (keeps back-compat import shim in old location)
- [ ] Update all imports in tests
- [ ] All 19 existing appointment_lifecycle tests pass unchanged
- [ ] Commit

## Task 2: Google Calendar backend
- [ ] OAuth flow: `packages/integrations/calendar/google_oauth.py` (service account for MVP — user-consent OAuth in PHASE6)
- [ ] Implement `GoogleCalendarBackend(CalendarBackend)`
- [ ] Test with a real dev calendar (manual + captured golden fixtures)
- [ ] Handle rate limits (429 → exponential backoff)
- [ ] Handle timezone conversion tenant-tz ↔ Google's RFC3339
- [ ] Commit

## Task 3: Cal.com backend (open-source-friendly alt for tenants who don't want Google)
- [ ] `CalComBackend(CalendarBackend)` against Cal.com's public API
- [ ] Handle their event-type-based booking model (differs from Google's "raw slot")
- [ ] Test against Cal.com sandbox
- [ ] Commit

## Task 4: Calendar router + BusinessProfile plumbing
- [ ] `BusinessProfile.calendar_provider: Literal["fake", "google", "cal_com"]`
- [ ] `packages/integrations/calendar/router.py` → returns the right backend instance
- [ ] Session-manager wires the routed backend into the tool handler
- [ ] Test — swap provider per tenant, verify correct backend called
- [ ] Commit

## Task 5: Reschedule flow (WorkflowController extension)
- [ ] Add `RescheduleWorkflow` state machine: find_existing → confirm_which → capture_new_time → confirm_new → commit
- [ ] Tool handler: `reschedule_appointment` becomes pure (takes appointment_id + validated new_start)
- [ ] Test 15 canned reschedule flows
- [ ] Commit

## Task 6: Cancel flow
- [ ] Same shape as reschedule
- [ ] Idempotent cancel (already implemented in FakeCalendar; keep same contract for real backends)
- [ ] Test 10 canned cancel flows
- [ ] Commit

## Task 7: Waitlist (opt-in)
- [ ] `WaitlistWorkflow`: if requested slot taken → offer waitlist entry
- [ ] Store waitlist in tenant DB (new table)
- [ ] On real cancel: check waitlist, trigger callback (dispatches to outbound-call system when it exists — for now just SMS via existing channels)
- [ ] Test with mock calendar
- [ ] Commit

## Task 8: No-show recovery (opt-in)
- [ ] Cron/scheduled: 30min after appointment start, check calendar for "still upcoming" → mark no-show → optional SMS
- [ ] Test — mock time, verify state transitions
- [ ] Commit

## Task 9: Reminders (opt-in)
- [ ] Scheduled: 24h + 2h before each booking → SMS reminder (using existing channels)
- [ ] Idempotent — don't send twice
- [ ] Test
- [ ] Commit

## Task 10: 30-call soak against real calendar
- [ ] Point test-clinic tenant at a real Google Calendar dev account
- [ ] Dial 30 calls covering: book / reschedule / cancel / waitlist / conflict
- [ ] Verify calendar state after each
- [ ] Target: ≥95% success on well-formed flows, 100% no double-books

## Task 11: Close-out
- [ ] `docs/rnd-2026-08/68-phase4-4a-close.md`
- [ ] Doc #58 update — 4a done, 4b/4c unblocked

## Success criteria
- Book, reschedule, cancel, waitlist, reminders, no-show recovery all live
- At least 2 calendar backends (Google + Cal.com) working
- Real Google Calendar sees the bookings; no double-books across 30 soak calls
- All flows idempotent (same call twice → same result)
