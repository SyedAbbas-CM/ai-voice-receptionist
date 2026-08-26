# EU Real-Estate Voice-Agent Demo Script

**Tenant:** Ribeira Prime Real Estate, Lisbon (`sample-data/real-estate/business.json`)
**Persona:** Sofia, front-desk lead
**Purpose:** Reproducible test-call script covering the 4 edge cases from the job brief + the 6 primary flows.
**Recorder:** call comes in on the Lightsail deploy, `/twilio/status` fires `completed`, per-call transcript persists to `data/logs/calls/<CA...>.log` and dashboard.

---

## Pre-flight checklist (before dialing)

- [ ] Lightsail server up: `curl -s -o /dev/null -w "%{http_code}\n" https://agent.eternalconquests.com/healthz` → 200
- [ ] `BUSINESS_PROFILE_PATH=sample-data/real-estate/business.json` in `.env`
- [ ] Restart: `sudo systemctl restart receptionist.service`
- [ ] Twilio number → Voice URL points at `https://agent.eternalconquests.com/twilio/voice`
- [ ] Twilio number → Status Callback → `https://agent.eternalconquests.com/twilio/status`
- [ ] Log tail open: `tail -f /tmp/uvicorn.log | grep -E "TWILIO_|BRAIN|NEXT_ACTION_SYNTH|HUBSPOT"`
- [ ] Record the call in Twilio console OR use OS-side recording (QuickTime) since we're not billing minutes for the demo
- [ ] Have this script open in a second window

Expected greeting (from `business.json`): Sofia opens with a Portuguese-or-English contextual greeting per `voice_persona`. The GDPR notice fires at greeting via `recording_notice_script_en` / `_pt`.

---

## Flow 1 — Buyer enquiry (happy path, 5 min)

**You dial in as a UK-based buyer looking for a €600–800k apartment in central Lisbon.**

| You (caller) | What Sofia should do (verify) |
|---|---|
| "Hi, my name's James, I'm calling about buying a place in Lisbon." | Confirm name, ask what area / budget / property type. NO "sure!" or "absolutely!" openers. |
| "I'm looking around six hundred to eight hundred thousand euros. Two-bed apartment ideally, in Chiado or Príncipe Real." | Should invoke `qualify_buyer_lead` OR ask financing/timeline next. Watch for random `budget_max_eur` — should be 800000 exactly. |
| "I'm a UK resident, financing sorted, closing in the next three months." | Should ask about NIF + Portuguese bank per non-resident flow. Optionally invoke `lookup_faq(topic="non resident")`. |
| "Yeah, I don't have a NIF yet — is that a problem?" | Should NOT invent legal advice. Should say "we help many non-resident buyers, you'll need a NIF and a Portuguese bank account, we can help with that" — should match the fixture FAQ verbatim. |
| "Great — can I see a place next week?" | `check_viewing_availability(date=<next Wed>)`. Reads back 2–3 slots (not all 10). Deterministic renderer should fire — grep log for `NEXT_ACTION_SYNTH_HIT source=slot_proposal`. |
| "Wednesday at 3 works." | `book_viewing(caller_name="James Chen", phone=<+44...>, property_ref=<caller-provided or "Chiado 2-bed shortlist">, start_iso, viewers_count=1)`. Confirms deterministically — grep for `source=confirm_action`. |
| "Perfect, thanks!" | Farewell + Twilio REST hangup. Grep `FAREWELL_HANGUP` + `TWILIO_END_CALL_OK`. Call should drop within ~3s of "see you Wednesday". |

**Success criteria:**
- Booking row appears in DB
- HubSpot contact + note created (grep `HUBSPOT_CONTACT_UPSERT`)
- Confirmation SMS fires to caller (grep `TWILIO_SMS_SENT`)
- Owner email fires with ICS attachment
- Dashboard shows the new booking at `/dashboard/?token=<api-key>`

---

## Flow 2 — Seller / valuation request (never quote on phone)

**You dial in as a seller with a 90sqm apartment in Alfama.**

| You | What Sofia should do |
|---|---|
| "Hi, I want to sell my apartment." | Ask address, sqm, condition, timeline, reason. |
| "It's in Alfama, ninety square metres, two bedrooms, good condition, been maintained. Looking to sell in the next six months." | `qualify_seller_lead(...)` with those fields. Score should be ~63 (150sqm profile earns 25 bonus; 90sqm earns 10). |
| "**What's it worth?**" | **CRITICAL EDGE CASE:** Sofia must refuse to quote. Persona says "never quote a valuation on the phone — that always requires a home visit." Expected: "That's not something I can give you accurately over the phone — it really needs a home visit. Can I book Maria or João in for a valuation this week?" |
| "OK, next Thursday afternoon works." | `book_valuation_visit(caller_name, phone, address, start_iso)`. |
| "Thanks, bye." | Farewell + hangup. |

**Success criteria:**
- Sofia NEVER speaks a numeric price
- `qualify_seller_lead` fires before `book_valuation_visit`
- CRM note logs the seller intent with `reason_selling` if given

---

## Flow 3 — Rental enquiry (short)

**You dial as a French national moving to Lisbon in a month, €1800/mo budget.**

| You | Sofia response check |
|---|---|
| "Bonjour, I'm looking for a rental in Lisbon." | Language-switch or ask English/Portuguese preference. Fixture says one agent speaks French — Sofia herself doesn't. Should offer to have French-speaking agent call back OR continue in English. |
| "English is fine. Two bedrooms, around €1800 a month, moving in a month." | `qualify_rental_lead(budget_month_eur=1800, move_in_date=<+30d>, ...)`. |
| "I'm employed remotely by a French company." | `employment_status="employed_foreign"`. Score should be moderate (~58-63). |
| "OK, can someone call me back tomorrow morning?" | `take_message` — this is the "caller doesn't want a call NOW" edge case. Should NOT fire `escalate_to_human` since the caller declined immediate transfer. |

**Success criteria:**
- Rental lead in CRM
- Message stored with `preferred_callback_time="tomorrow morning"`
- No mis-fired escalation

---

## Flow 4 — Angry caller (Q3 edge case)

**You dial in furious about a "waste of time" viewing that fell through.**

| You | Sofia response |
|---|---|
| "This is Mark, I had a viewing scheduled for yesterday and NOBODY SHOWED UP. I drove forty minutes." | **EDGE CASE — must transfer, not book.** `human_transfer_rules.always_transfer_on: ["complaint", ...]`. Sofia should acknowledge briefly ("I'm really sorry to hear that, that shouldn't have happened") then invoke `escalate_to_human(reason="complaint about missed viewing", urgency="high")`. No chirpy "Sure!". No pretending to book a make-up viewing herself. |
| (if she asks for anything else) | Should NOT continue qualifying — the transfer is the whole response. |

**Success criteria:**
- `escalate_to_human` fires with `reason` mentioning "complaint" AND `urgency="high"`
- No booking attempt
- Sofia uses empathy tone (per ACK_EMPATHY primitive shipped in commit 738d8ea — though wiring not activated yet, prompt guidance still applies)

---

## Flow 5 — Caller asks something outside knowledge (Q3 edge case)

**You dial in and ask for today's Portuguese mortgage rates.**

| You | Sofia response |
|---|---|
| "Hi, what's the current mortgage rate for non-residents in Portugal?" | **EDGE CASE — must decline honestly.** Persona: "you say honestly 'that's not something I can answer accurately'". Expected: "That's not something I can quote you accurately over the phone — but I can have one of our mortgage brokers call you back within the hour, they'll be much better on that. Can I take your number?" |
| "Yeah, +351 91 234 5678." | `take_message(recipient="mortgage broker", subject="current non-resident rate", ...)` |

**Success criteria:**
- Sofia does NOT hallucinate a rate
- Does NOT say "I don't know" bluntly — offers the callback
- `take_message` fires OR `escalate_to_human(reason="mortgage rate question")` — either is acceptable per fixture

---

## Flow 6 — Unqualified lead (Q3 edge case)

**You dial in vague, no clear intent.**

| You | Sofia response |
|---|---|
| "Hi, um, I was just wondering what you guys do." | Clarify intent politely — "we do sales, rentals, and valuations across central Lisbon — is there something specific you had in mind?" |
| "Not really, just browsing." | **EDGE CASE — unqualified.** Should NOT push a booking. Should offer to send the shortlist / add to newsletter / take a light-touch note. Reasonable: "no problem — want me to text you our current shortlist to browse? No pressure to book anything." |
| "Sure." | `take_message(caller_name, phone, message="light-touch enquiry, wants shortlist SMS", priority="normal")` |

**Success criteria:**
- No forced qualification
- No pressure booking
- Message stored so agent can follow up later without wasting the caller's time now

---

## Flow 7 — CRM integration failure (Q3 edge case, hard to reproduce)

**Requires temporarily breaking the CRM.** Do this LAST because it may leave the tenant in a weird state.

Setup:
```bash
# On Lightsail before dialing:
export HUBSPOT_ACCESS_TOKEN=deliberately-invalid-token
sudo systemctl restart receptionist.service
```

**You dial in as a normal buyer (repeat Flow 1's opening).**

| You | Sofia response |
|---|---|
| Complete a buyer qualification + book viewing normally | Sofia should complete the flow WITHOUT ever telling the caller "sorry, the CRM is down." CRM failures are internal — caller-facing UX must not degrade. |

**Verify (post-call):**
- Booking row IS in local SQLite (calendar backend independent of CRM)
- HubSpot log line: `hubspot upsert_contact failed: HubSpot POST /crm/v3/objects/contacts/search -> 401 after 4 attempts (retryable)`
- SMS confirmation still fired to caller
- Owner email still fired
- Once outbox lands (networking's Day 4 alembic), the failed CRM write will queue for retry — verify by re-setting the token + waiting for outbox worker

**Success criteria:**
- Caller has ZERO awareness of the CRM failure
- Logs show the specific failure + retry attempts
- Booking + follow-ups still fire

---

## Post-demo cleanup

- Restore `HUBSPOT_ACCESS_TOKEN` to valid value if Flow 7 was run
- Capture recording (Twilio console → Calls → download)
- Save all 7 call SIDs for the application submission
- Dashboard screenshot: `https://agent.eternalconquests.com/dashboard/?token=<api-key>` — should show all 7 calls with outcomes
- Grep the 7 CA-ids from `/tmp/uvicorn.log` and save the transcript slices for evidence

---

## What to send with the application

1. **Best 2 recordings** — pick Flow 1 (happy path) + Flow 4 (angry caller handled well)
2. **1 dashboard screenshot** showing the 7-call session
3. **1 architecture diagram** — networking's artifact (0e82270c-4ec3-4af3-93f0-6f6b226c4009) or my updated version if we ship one
4. **The application text** at `docs/APPLICATION-REAL-ESTATE-2026-08-25.md`
