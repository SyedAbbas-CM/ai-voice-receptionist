# Product Positioning — Three Parts

**Captured 2026-08-26** based on a car-wash Upwork brief that clarified how the codebase actually splits into three sellable products, not one.

**Not currently pursuing this specific job** — the brief was just useful thinking material about how to sell what we already have.

---

## The three parts

### Part 1 — AI receptionist (what we've been building)
- Agent answers inbound calls
- Qualifies leads, books appointments, escalates emergencies
- Requires: Deepgram + OpenAI + ElevenLabs + Twilio Media Streams + our custom brain
- Sold to: dental clinics, real-estate agencies, service businesses that miss too many calls to hire full-time reception
- **Existing implementation:** brain.py + prompt.py + all the humanness work

### Part 2 — Call tracking + metrics dashboard
- Works ALSO for tenants where humans answer the phone
- Captures every call transition via Twilio Status Callback
- Optional call recording + Whisper transcription
- Dashboard: caller number, duration, disposition (answered / missed / voicemail), transcript, owner-marked outcome (success / follow-up / spam)
- Success metric: owner-marked OR heuristic vocabulary match
- Sold to: businesses that want visibility into WHAT their front desk is doing without giving up human-first
- **Existing implementation:** `/twilio/status` route + `apps/api/app/routes/dashboard.py`. Need: extend dashboard to show non-AI-answered calls, add owner-outcome-marking UI, optionally wire recording + transcription.

### Part 3 — SMS automation layer
- Missed-call auto-text within seconds ("hey, sorry we missed you — book here: {link}")
- Appointment reminders: day-before + 2h-before
- No-show follow-up ("hey — noticed you couldn't make it, want to rebook?")
- Suppression: never same message twice, respect STOP
- Owner-voice template rendering (offline OpenAI, 5 variants, round-robin per contact)
- Google Sheets logging every SMS sent
- Sold to: businesses that lose customers between the call and the appointment (car washes, detailers, mobile services, appointment-heavy retailers)
- **Existing implementation:** `packages/integrations/sms_sender.py` (SMS send), `FollowupSink` (fires on booking success). **Need:** missed-call handler, reminder scheduler, suppression table.

---

## Why three parts, not one

**Different buyers, different pain, different price points.**

Some tenants only want Part 1. Some want Parts 2+3 but explicitly DON'T want AI answering. Some want all three.

Bundling all three under "AI receptionist" hides the value of Parts 2 and 3 for buyers who don't want AI on the call itself. Splitting them lets us sell to a bigger market with less friction.

**Same code powers all three.** Each part is a config toggle:
- Part 1: `AI_RECEPTIONIST_ENABLED=true`
- Part 2: `CALL_TRACKING_ENABLED=true` (default true when tenant has Twilio numbers)
- Part 3: `SMS_AUTOMATION_ENABLED=true` (requires Twilio SMS credit + consent flow)

---

## What's missing to actually sell Parts 2 + 3

### For Part 2 (call tracking):
- Extend dashboard with an "All calls" view (currently only shows AI-answered sessions)
- Owner-outcome-marking UI (mark call success/failure with 1 click)
- Optional: recording + transcription pipeline
- Optional: heuristic success detection from transcript

### For Part 3 (SMS automation):
- Missed-call handler on `/twilio/status` (branch on `CallStatus=no-answer|busy|failed`)
- Reminder scheduler (systemd timer OR APScheduler in-process, scans `bookings` table for `[+22h,+26h]` and `[+90m,+150m]` windows)
- Suppression table `sms_reminder_log` (`tenant_id + booking_id + reminder_kind + sent_at`) — enforces "never same reminder twice"
- No-show follow-up detection (`scheduled_for + X hours < now AND status == 'confirmed'` → mark no-show + fire follow-up SMS)
- Owner-voice template rendering (offline OpenAI batch job that produces 5 variants; sink picks round-robin per contact)
- Public `POST /public/booking` route for form-submitted bookings (currently only phone-flow writes to bookings table)
- Extend Google Sheets sink schema to include missed-call rows + SMS-sent rows

**Rough scope:** 20 hours of build work. Would ship as a single 2-week sprint.

---

## Pricing signals from the car-wash brief

Their placeholder budget was $500 (they said "the listed $500 is a placeholder"). Realistic pricing for a build of the shape they described:

- **$1,500–2,500** for build + delivery + 30-day support window
- **$500–800/month** ongoing for the runtime (Twilio SMS costs + our hosting)

Same math applies to any SMB brief with a similar shape — plumber, mobile detailer, mobile groomer, small salon chain, appointment-heavy retail.

---

## Sales artifact idea (deferred)

When we do want to pitch Parts 2+3 as separate products:
- 1-page landing per part
- 30-second demo video per part
- One integration diagram showing all three sharing the same platform
- Case study format: "Business X was doing Y manually — now Z happens automatically"

Not building now. Note-to-self for when we're actively selling.

---

## Why this note exists

Codebase already contains 80% of Parts 2+3. Sale-ready with 20 hours of wiring. Don't lose that framing when we pivot back to real-estate delivery + humanness work.

Related tasks (existing in tracker):
- #100: Wire SMS + email follow-up on booking success (COMPLETED — part of Part 3 foundation)
- #121: TakeMessage tool + ReceptionMessage model (PENDING — needed for Part 2's "front desk missed the call but noted it")
- #127: Extend dashboard to full Receptionist Inbox (PENDING — Part 2 core)
