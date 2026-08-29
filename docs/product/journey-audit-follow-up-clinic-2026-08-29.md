# Customer journey audit — "a follow-up" (dental / clinic)

Author: product-lead (subagent)
Scenario: Returning Dutch expat patient calls dental clinic and asks for "a follow-up".  Agent resolves to `Follow-up visit` service and marches straight into ASK_SLOT(phone) with zero discovery — no question about follow-up TO WHAT, with WHICH doctor, or WHEN the original visit was.
Vertical: clinic
Date: 2026-08-29

## Scope

Deep dive on ONE specific caller scenario.  Turn-by-turn analysis of the current agent path vs the ideal receptionist path, plus the product gaps each divergence exposes.

The audit is triggered by real call `CA2fa1fef2065a7df388c3d6f58d7a7792` (Christiaan, `+31 6 25 00 76 00`), where the caller opened with "I'd like to book a follow-up please" and the agent, after resolving the service via `packages/integrations/service_aliases.py`, jumped straight to asking for a phone number.  The booking would have "succeeded" against an arbitrary 30-min slot with an arbitrary provider — a canonical false-complete.

## Caller context

**Persona:** *Recall / follow-up patient* — returning patient with an existing chart at the practice, calling because a doctor said "come back in a few months" or because a procedure needs a scheduled recheck.  Speaks fluent English with a Dutch accent, expat mobile number (`+31 …`), so the number itself signals returning-from-abroad or foreign-registered handset.  See playbook §2 "Follow-up / recheck".

**Trigger call reference:** `CA2fa1fef2065a7df388c3d6f58d7a7792` — Christiaan, `0625007600`.  Verbatim opener: "My name is Christian, and my number is zero six two five zero zero seven six zero zero".  Full transcript in `data/call_events.db`.

**Business context:**
- Tenant: `Smile Dental Clinic`, Plano TX (fixture: `sample-data/clinic/business.json`).
- Providers on staff (name → speciality per fixture): **Dr. Chen** (general + on-call for after-hours emergency), **Dr. Whitfield** (general + pediatric first-visit specialist), **Dr. Ramanathan** (Invisalign / ortho), **Rosa Delgado** (lead hygienist, bilingual EN/ES).
- Services relevant here: `Follow-up visit` (30 min, free within 30 days of original visit else $75), `Emergency exam`, `Adult recall exam`, plus procedure services (`Composite filling`, `Implant consultation`, etc.) that might have generated the follow-up need.
- Hours: Mon-Wed 07:30-17:00, Thu 07:30-19:00, Fri 07:30-15:00, Sat 08:00-13:00, Sun closed.
- Persona voice: Alex, warm Texan front-desk lead.

## Transcript (current agent vs ideal)

### A) What the current agent actually does today (scripted from code path)

```
[TS 00:00.000] AGENT:   Smile Dental, this is Alex — how can I help?
[TS 00:03.400] CALLER:  Hi, I'd like to book a follow-up please.
[TS 00:06.100] AGENT:   Yeah, absolutely — can I grab your phone number to get you on the schedule?
[TS 00:11.900] CALLER:  My name is Christian and my number is zero six two five zero zero seven six zero zero.
[TS 00:19.400] AGENT:   Perfect, so that's plus three one six two five zero zero seven six zero zero — and what day were you thinking for the follow-up?
[TS 00:26.200] CALLER:  Uh, tomorrow morning if that works?
[TS 00:29.000] AGENT:   Let me check that for you.  [check_availability(service="Follow-up visit", date="2026-08-30")]
[TS 00:31.800] AGENT:   I've got nine, nine thirty, or ten fifteen tomorrow morning — which works?
[TS 00:37.000] CALLER:  Nine thirty is great.
[TS 00:39.200] AGENT:   Booked!  Christian, nine thirty tomorrow for a follow-up visit — see you then.
                        [book_appointment(service="Follow-up visit", start_iso="2026-08-30T09:30",
                                          duration_minutes=30, phone="+31625007600", caller_name="Christian")]
```

**What is silently wrong:**
- Service resolved to `Follow-up visit` (30 min) via alias-map exact keyword hit (`packages/integrations/service_aliases.py:118`).  MATCH_EXACT with confidence 0.95, no clarification triggered.
- No question about which procedure this is following up on → could be a 3-min suture check or a 90-min crown seat that needs a *different service* entirely.
- No question about which provider did the original work → violates continuity-of-care rule (playbook §5, second bullet).  Fixture has 3 dentists.  The system will pick whoever the calendar has open, which is close to guaranteed to be the wrong one for this specific patient.
- No question about when the original visit was → misses the "free within 30 days" pricing rule baked into the fixture's `Follow-up visit` description.  Patient could be quoted-nothing when they owe $75, or vice versa.
- No lookup of existing chart / existing appointment.  `find_existing_appointment` tool exists in `packages/integrations/clinic_tools.py:66` and is *never called on this path*.  The patient is treated as a new booking with no history — which is doubly wrong because we already know they're returning (a follow-up definitionally means they've been in before).

### B) What the IDEAL receptionist would say (turn-by-turn)

Voice = the Alex persona from the fixture (warm Texan, contractions, not chirpy).

```
[TS 00:00.000] AGENT:   Smile Dental, this is Alex — how can I help?
[TS 00:03.400] CALLER:  Hi, I'd like to book a follow-up please.
[TS 00:06.100] AGENT:   Yeah, happy to.  A follow-up to what — was this after
                        a filling, a cleaning, something else?
[TS 00:12.500] CALLER:  A filling.  I had one done a couple weeks back and
                        the doctor said to come back in.
[TS 00:19.000] AGENT:   Gotcha — and who did the filling for you, was that
                        Dr. Chen or Dr. Whitfield?
[TS 00:24.200] CALLER:  Dr. Chen.
[TS 00:25.400] AGENT:   Okay, and roughly what date was that — end of last
                        month, or more recent than that?
[TS 00:31.100] CALLER:  Uh, about two weeks ago I think, maybe the fifteenth?
[TS 00:36.600] AGENT:   Perfect, that puts you inside the thirty-day window
                        so this follow-up won't be charged.  Let me pull
                        your chart real quick — can I grab the phone number
                        that'd be on file?
                        [find_existing_appointment(phone=..., upcoming_only=False)
                         once we get the number — confirms original visit + provider]
[TS 00:44.900] CALLER:  Zero six two five zero zero seven six zero zero.
[TS 00:51.100] AGENT:   Got it, plus three one six two five zero zero seven six
                        zero zero — that's Christian, right?  I see the
                        filling with Dr. Chen on August fifteenth.  He's in
                        Tuesday, Wednesday, and Thursday next week — want
                        me to pull his openings, or is a specific day better?
[TS 01:02.400] CALLER:  Tomorrow morning if that works?
[TS 01:05.000] AGENT:   Tomorrow's Friday — Dr. Chen doesn't work Fridays.
                        His next opening is Tuesday.  I can either get you
                        in with him Tuesday, or if you want to be seen
                        sooner Dr. Whitfield has tomorrow morning open — but
                        for continuity, most folks like to see the same
                        dentist who did the original work.  Which would
                        you rather?
[TS 01:22.000] CALLER:  I'll wait for Dr. Chen, Tuesday works.
[TS 01:24.500] AGENT:   Perfect.  Tuesday I've got nine, ten thirty, or
                        two.  Which?
[TS 01:29.800] CALLER:  Ten thirty.
[TS 01:31.000] AGENT:   Booked — Tuesday September second at ten thirty with
                        Dr. Chen, thirty-minute follow-up on that filling,
                        no charge.  We'll text you a reminder the day
                        before.  Anything else?
[TS 01:44.500] CALLER:  No, that's it, thanks.
[TS 01:46.000] AGENT:   You got it, see you Tuesday.
                        [book_appointment(service="Follow-up visit",
                                          start_iso="2026-09-02T10:30",
                                          duration_minutes=30, phone="+31625007600",
                                          caller_name="Christian",
                                          notes="Follow-up to composite filling done by Dr. Chen 2026-08-15;
                                                 free within 30-day window; continuity-of-care assignment")]
```

Key differences vs current path:
- Three DISCOVERY turns happen BEFORE any slot is asked for — procedure type, provider, original-visit date.
- Chart lookup runs *before* the calendar write, not after.
- Provider constraint (Dr. Chen doesn't work Fridays) is honored, and the patient is offered a real continuity-vs-speed tradeoff.
- Price rule fires deterministically ("no charge") from the 30-day window.
- Booking `notes` capture provenance so the assistant seating the patient knows what this visit is for.

## Turn-by-turn audit

### Turn 1 — greeting

- **Current:** "Smile Dental, this is Alex — how can I help?"
- **Ideal:** identical.
- **Divergence severity:** OK
- **Root cause:** —
- **Product gap:** none.

### Turn 2 — caller opener "I'd like to book a follow-up please"

- **Current agent hearing:** raw string "a follow-up" arrives in the turn.
- **Ideal handling:** phrase should be classified as *AMBIGUOUS_INTENT* at the dialogue-policy layer, distinct from AMBIGUOUS_SERVICE.  It is not "which of your services did they mean" — it's "we know the service class but we're missing the qualifying context that every human receptionist would ask for".

### Turn 3 — first agent response (the critical divergence)

- **What the agent SAID:** "Yeah, absolutely — can I grab your phone number to get you on the schedule?"
- **What the ideal agent WOULD say:** "Yeah, happy to.  A follow-up to what — was this after a filling, a cleaning, something else?"
- **Divergence severity:** **broken**.  This is where the whole call goes wrong.  Every downstream turn compounds the error.
- **Root cause of divergence:** two independent failures compound.
  1. **Service resolver reports MATCH_EXACT with 0.95 confidence** for the literal string "a follow-up" against tenant service `Follow-up visit` (`packages/integrations/service_aliases.py:118-124` maps "a follow-up" → keyword `("follow-up",)` → matches the single tenant service by substring → returns `MATCH_EXACT`).  There is no rule that says "even if the surface form matches, follow-up is a special class of service where you MUST gather context before booking".  The resolver is a pure name-canonicalizer; it has no concept of *pre-booking-required context*.
  2. **NextActionPolicy has no discovery branch.**  In `packages/dialogue/next_action_policy.py:305-394`, the decision ladder is: `EMERGENCY → ESCALATE`, `tool_pending → TOOL_PREAMBLE`, `requires_confirmation → CONFIRM_ACTION`, `missing[0] → ASK_SLOT`, `OPENING → ACKNOWLEDGE`, `WRAPPING → END_CALL`, else `ANSWER`.  Once the service is "resolved", the reducer populates `missing=["phone", "date"]` (or similar) and `ASK_SLOT` fires on `phone`.  There is no rule of the shape *"if service is in the follow-up class AND we don't know {original_procedure, original_provider, original_visit_date}, fire CLARIFY on the missing context slot before touching phone/date"*.
- **Product gap exposed:** the system treats "which service" and "enough context to book that service" as the same question.  For most services these coincide.  For follow-up-class services (and to varying degrees for consultation-class services), they do not.

### Turn 4 — caller volunteers name + phone

- **Current:** phone accepted, agent moves on to date.
- **Ideal:** phone would be requested LATER — after context is gathered — because we'd use the phone to look up the existing chart, not to open a new record.  Same digits, different purpose, different order.
- **Divergence severity:** significant (not broken, but wrong ordering).
- **Root cause:** the ASK_SLOT sequence in the reducer is driven by tool argument requirements (`book_appointment.required = [caller_name, phone, service, start_iso]`).  Phone is required by the *booking* tool, so it becomes the first missing slot.  But there is no tool-arg on `book_appointment` for `original_procedure`, `original_provider`, or `original_visit_date` — so those slots are *invisible* to the ASK_SLOT sequence entirely.
- **Product gap:** slot-gathering is driven by tool schema, not by service-type context requirements.  Anything not in the tool schema doesn't exist.

### Turn 5 — agent asks for a day

- **Current:** "what day were you thinking for the follow-up?"
- **Ideal:** would already be constrained to days the correct provider actually works.  For Christiaan asking "tomorrow morning", if the original provider doesn't work Fridays, the ideal agent surfaces the tradeoff.  The current agent doesn't know who the original provider is, so it can't surface anything.
- **Divergence severity:** significant.
- **Root cause:** `check_availability` takes `(service, date)` only.  Provider is not a parameter.  The `FakeCalendar` layer does not model per-provider schedules.  Fixture doesn't encode which provider does which procedure or their weekly hours.
- **Product gap:** provider is a first-class concept in a real practice but a zero-class concept in the fixture, the tool schema, the calendar, and the prompt.

### Turn 6 — agent proposes slots + books

- **Current:** books a 30-min slot on the next open time.
- **Ideal:** slot is with the specific provider, notes capture procedure provenance, price rule ("free within 30 days") fires from real data.
- **Divergence severity:** broken (silent false-complete).
- **Root cause:** duration lookup is service-name based (`_service_duration`); the fixture's `Follow-up visit` has a single hardcoded 30-min duration regardless of what procedure it is following up on.  Post-implant follow-up (real-world ~30 min including radiographs) and post-endo suture check (~10-15 min) collapse into the same slot.  Post-crown SEAT (typically 45-60 min) collapses into the same slot too, which will bleed into the next patient.
- **Product gap:** `Follow-up visit` is a container type, not a booking type.  It needs sub-typing driven by the original procedure — either as separate services in the fixture, or as a duration modifier the tool computes from the linked original visit.

### Turn 7 — closing readback

- **Current:** "Booked!  Christian, nine thirty tomorrow for a follow-up visit — see you then."
- **Ideal:** "Booked — Tuesday at ten thirty with Dr. Chen, thirty-minute follow-up on that filling, no charge.  We'll text you a reminder the day before."
- **Divergence severity:** significant.
- **Root cause:** `CONFIRM_ACTION`'s `must_include_facts` list in `next_action_policy.py:344-348` is fixed to `service, date, time, caller_name`.  Provider name and price are not in the readback contract.
- **Product gap:** confirmation readback contract is one-size-fits-all — doesn't verticalize.  Dental follow-up needs `provider` + `price_or_free_reason` in the readback so the caller can catch a wrong-provider booking before it goes on the calendar.

## Product gaps summary

Aggregated across the turn audit above.

- **Gap 1 — No "pre-booking context required" concept for service types.**
  - Turns affected: 3, 4, 5, 6, 7 (everything downstream of the resolver hit).
  - Fix scope: new fixture-side metadata on `Follow-up visit` (and eventually `Consultation`-class services) marking which context fields must be gathered before booking; new dialogue-policy branch that reads that metadata.
  - Priority: **P0**.

- **Gap 2 — NextActionPolicy has no DISCOVERY branch for ambiguous-context services.**
  - Turns affected: 3, 4, 5.
  - Fix scope: add a `ConversationAction.DISCOVER_CONTEXT` (or reuse `CLARIFY` with a `context_slot` field) that fires before `ASK_SLOT` when service-type-required context is missing.
  - Priority: **P0**.

- **Gap 3 — Provider is a zero-class concept end-to-end.**
  - Turns affected: 3, 5, 6, 7.
  - Fix scope: fixture-side (per-service `providers: [...]`, per-provider `weekly_hours`), tool-schema-side (`check_availability` and `book_appointment` accept optional `provider`), calendar-side (per-provider slot lists), prompt-side (agent knows which provider does what).
  - Priority: **P1** (large, but this shows up in half of the clinic bad-outcome catalog).

- **Gap 4 — Chart lookup is never invoked on the follow-up path.**
  - Turns affected: 4, 6, 7.
  - Fix scope: dialogue policy should, on a "returning patient" signal (which "follow-up" definitionally is), sequence `find_existing_appointment` BEFORE `book_appointment` and use its result to pre-fill provider, verify original-visit date, and confirm the caller against their real name.
  - Priority: **P1**.

- **Gap 5 — `Follow-up visit` is a container type, not a booking type.**
  - Turns affected: 6.
  - Fix scope: duration must be derived from the linked original procedure (implant check ≠ post-extraction check ≠ crown seat).  Either split into sub-services in the fixture or compute a duration modifier at booking time.
  - Priority: **P2** (functional, but silent duration errors cascade into overbooking).

- **Gap 6 — Price rule "free within 30 days" lives in a description string, not in logic.**
  - Turns affected: 6, 7.
  - Fix scope: structured field `free_within_days: 30` on the service, or a computed pricing hook.  Agent should quote-or-not-quote deterministically.
  - Priority: **P2**.

- **Gap 7 — Confirmation readback contract is not verticalized.**
  - Turns affected: 7.
  - Fix scope: allow per-vertical `must_include_facts` templates.  Clinic follow-up: `service, date, time, caller_name, provider, price_or_free_reason, original_procedure_ref`.
  - Priority: **P2**.

- **Gap 8 — Alias resolver conflates "service name maps to a tenant service" with "we have enough info to act".**
  - Turns affected: root cause of Gap 1.
  - Fix scope: `ServiceMatch` gains a `requires_context: list[str]` field populated from tenant service metadata; the caller of `resolve_service` treats a match with non-empty `requires_context` as *"resolved but not actionable"*.
  - Priority: **P1** (small mechanical change, unlocks P0 dialogue-policy branch).

## Cross-references

- Persona-ladder entry: *"Follow-up / recheck"* archetype in `.claude/plugins/product-lead/product_playbooks/clinic.md` §2 (already documented, becomes the anchor for the next persona-ladder deliverable).
- Service-taxonomy entries: playbook §3 "Follow-up / recheck" cluster — `Post-procedure follow-up`, `Post-antibiotic recheck`, `Implant integration check`, `Second-visit of two-visit treatment` are all sub-shapes of what the fixture flattens into a single `Follow-up visit`.
- Bad-outcome catalog: this scenario fires *at least three* items from playbook §5 — "Booked wrong duration", "Wrong provider", "Didn't offer alternate provider".  It is also a canonical "false complete" per the agent charter's *"Watch for false completes"* clause.

## Recommendations for engineering

Ordered by priority.  Product-lead does not write code — these are specs.

1. **P0 — Add `requires_context` to fixture + resolver** (addresses Gap 8 → enables Gap 1)
   - Where: `sample-data/clinic/business.json` (fixture metadata); `packages/schemas.py` (`ServiceOffering` model gets an optional `requires_context: list[str]`); `packages/integrations/service_aliases.py` (`ServiceMatch` dataclass gains passthrough field).
   - Approach: on `Follow-up visit`, set `requires_context: ["original_procedure", "original_provider", "original_visit_date"]`.  Resolver reads it off the matched tenant service and includes it verbatim in the `ServiceMatch` return.  Backward-compatible — services without the field behave exactly as today.
   - Test: unit test on `resolve_service("a follow-up", <fixture>)` asserts the returned match carries the three context slot names.  No behavior change to existing service tests.
   - Estimated effort: half a day.

2. **P0 — Add DISCOVER_CONTEXT branch to NextActionPolicy** (addresses Gap 2 → uses Gap 1 data)
   - Where: `packages/dialogue/next_action_policy.py` (new enum value + new branch); `packages/dialogue/reducer.py` (populate `pending_context_slots` on state from the resolved service's `requires_context`, minus whatever the caller has already provided).
   - Approach: new `ConversationAction.DISCOVER_CONTEXT` with a `context_slot: str` field.  Decision ladder gets a new branch, positioned **before** the existing `missing: → ASK_SLOT` branch: *"if `state.pending_context_slots` is non-empty, return DISCOVER_CONTEXT with `context_slot = pending_context_slots[0]` and delivery=STANDARD, max_tokens=48"*.  Prompt scaffold in `packages/core_agent/prompt.py` renders it as a specific verbal question per slot ("A follow-up to what — was this after a filling, a cleaning, something else?" for `original_procedure`; "Who did the original work for you?" for `original_provider`; "Roughly when was the original visit?" for `original_visit_date`).
   - Test: `test_next_action_policy_followup_discovery.py` — state with resolved-service + `pending_context_slots=["original_procedure"]` returns `DISCOVER_CONTEXT` with `context_slot="original_procedure"`, not `ASK_SLOT("phone")`.
   - Estimated effort: 1-2 days including prompt-side verbalization templates.

3. **P1 — Wire `find_existing_appointment` into the follow-up path** (addresses Gap 4)
   - Where: dialogue policy again — when service class == follow-up AND phone becomes known, sequence a `find_existing_appointment` tool call BEFORE `check_availability`.  Result populates `original_provider` / `original_visit_date` context slots automatically, reducing DISCOVER_CONTEXT turns.
   - Approach: reducer detects "returning patient signal" from the service class, marks a tool sequence.  Brain schedules `find_existing_appointment` when phone lands.  Tool result populates the same context slots the DISCOVER_CONTEXT branch consumes — so if chart lookup succeeds we DON'T re-ask the caller.  If it fails (no chart under this phone), fall back to asking.
   - Test: end-to-end test with a fake calendar preloaded with a past `Composite filling` for phone `+31625007600` → call resolves to booking with `provider="Dr. Chen"` in notes without ever asking the caller who did the original work.
   - Estimated effort: 2-3 days.

4. **P1 — Provider becomes a first-class concept** (addresses Gap 3)
   - Where: fixture (`business.json` — add `providers: [{name, role, weekly_hours}]` array, and `providers: ["Dr. Chen", "Dr. Whitfield"]` per service where applicable); `packages/schemas.py`; `clinic_tools.py` (`check_availability` and `book_appointment` accept optional `provider`); `packages/integrations/fake_calendar.py` (per-provider slot lists).
   - Approach: large-ish but self-contained refactor.  Start fixture + schema, then push through tool signatures, then wire the reducer to include `provider` in the ASK_SLOT sequence for booking (with a smart default of "any" when the caller has no preference and continuity-of-care doesn't apply).
   - Test: multi-provider fixture-based test — booking a follow-up asks (or infers) the provider; booking a new-patient exam doesn't force the question.
   - Estimated effort: 4-5 days.

5. **P2 — Sub-type or dynamic duration for follow-up** (addresses Gap 5)
   - Where: fixture — either split `Follow-up visit` into `Follow-up visit (short)` / `(standard)` / `(long)` with a note on the parent, OR keep single entry with a `duration_by_original_procedure` map.  Product's call: latter is cleaner but tool-side more work.
   - Approach: recommend the map — one service, computed duration from `original_procedure` context slot filled by DISCOVER_CONTEXT / chart lookup.  `_service_duration` becomes `_service_duration(name, context)`.
   - Test: booking a follow-up after an `Implant consultation` yields 30 min; after a `Composite filling` yields 15 min; after a `Crown` yields 45 min.
   - Estimated effort: 2 days after Gap 3 lands.

6. **P2 — Structured pricing rule for the 30-day window** (addresses Gap 6)
   - Where: `packages/schemas.py` (`ServiceOffering.free_within_days: int | None`); `packages/integrations/fake_calendar.py` or a pricing helper; readback template.
   - Approach: compute at booking time using the chart-linked original visit date.  Confirmation readback gets a `price_or_free_reason` string that's either a price or "no charge — within thirty-day recheck window".
   - Test: booking a follow-up 20 days after original returns `price_free=True`; 40 days after returns `price=75.00`.
   - Estimated effort: 1 day.

7. **P2 — Vertical-specific `must_include_facts` for CONFIRM_ACTION** (addresses Gap 7)
   - Where: `packages/dialogue/next_action_policy.py:344-348` — extend `must_include_facts` construction to consult vertical + service-class-specific templates.
   - Approach: keep the fallback list, but add `must_include_facts_by_service_class` map.  Clinic follow-up template: `service, date, time, caller_name, provider, price_or_free_reason, original_procedure_ref`.
   - Test: a follow-up booking's `ConversationNextAction.must_include_facts` contains `provider` and `price_or_free_reason`; a new-patient booking's does not.
   - Estimated effort: 1 day.

## Recommendations for product

### Playbook updates for `.claude/plugins/product-lead/product_playbooks/clinic.md`

Add or enrich the following sections:

- **§4 "Ambiguous requests → clarification"** — the existing "A follow-up" bullet needs to expand into the three-question drill (procedure → provider → original date) and cite the DISCOVER_CONTEXT policy branch as the enforcement mechanism.  Also add: *"'A follow-up' is not an ambiguous SERVICE — it's an under-specified INTENT.  Do not treat it like 'a cleaning' where the question is which service; treat it like a returning-patient chart lookup."*
- **§3 "Full service catalog" — follow-up subsection** — flag that the four bullets already listed (`Post-procedure follow-up`, `Post-antibiotic recheck`, `Implant integration check`, `Second-visit of two-visit treatment`) are the SUB-TYPES that a single `Follow-up visit` fixture entry must dispatch to via `original_procedure`.  Explicitly note the duration/price differences so fixture-side and prompt-side agree.
- **§5 "Real failure modes"** — promote *"False-complete follow-up with wrong provider, wrong duration, wrong price rule"* to its own bullet at the top.  Currently distributed across "Booked wrong duration" / "Wrong provider" / "Didn't offer alternate provider" but the follow-up-specific compound of all three deserves its own row.
- **§2 "Real caller archetypes"** — the existing "Follow-up / recheck" bullet should note the *dual signal*: (a) the phrase "a follow-up" ITSELF is the returning-patient signal — no chart-lookup gate should require the caller to say "I've been in before"; (b) provider continuity matters more here than in almost any other archetype except emergency-pain.
- **§7 "Cross-sell / upsell opportunities"** — add: *"Follow-up booked within 30-day free window — surface `no charge` immediately in the readback, don't make the caller ask.  Frees goodwill for the next real revenue interaction."*

### New personas / archetypes discovered

- **Returning patient with expat / foreign-registered phone** — the Christiaan shape.  Sub-persona of "Follow-up / recheck" but with a phone-parse dimension: the number won't validate as US-region on first pass and the chart may be stored under a different number.  Should get its own row in the next persona-ladder revision, and the resolver should not treat "chart not found under this number" as "new patient" without asking.

### New failure modes discovered

- **"Follow-up phantom booking"** — booking succeeds but is unattached to the original visit.  Front-desk staff shows up to a `Follow-up visit` slot in the schedule with no idea which procedure or which provider.  Add to bad-outcome catalog with detection signal (`notes` field empty on `Follow-up visit` bookings), prevention rule (booking guard: reject `Follow-up visit` bookings without `original_procedure` + `original_provider` in notes), and recovery script (front-desk calls patient back to fill in the blanks — one of the worst impressions a returning patient can get).
