# Clinic (dental / medical / vet) — product playbook

Domain-knowledge reference for the product-lead agent when reviewing
receptionist features for clinic tenants.

Last updated: 2026-08-29 by voice-agent session (spawned during
Christiaan follow-up bisection).

## 1. Business shape

- **Staffing:** 1-2 dentists, 1-2 hygienists, 1-2 dental assistants, 1-2 front-desk. Multi-provider practices have specialists (endodontist, periodontist, orthodontist, oral surgeon) referred in or on staff.
- **Hours:** Mon-Fri 8-5 typical; one late day (until 7pm) for working patients. Saturday half-day is common for family practices. Never Sunday except emergency line.
- **Physical footprint:** Single location dominant. Multi-location groups exist but each has its own reception line + calendar.
- **Payment:** Insurance-first (PPO/DPPO/HMO/Medicaid), then cash. Payment plans + CareCredit for treatment plans >$1000. Copays collected at check-in.
- **Regulatory:** HIPAA — no clinical detail persisted unnecessarily, no reading back patient records to callers without ID verification. State licensing means providers can't practice across state lines. Utah/Texas AI-disclosure laws when directly asked. Emergency situations must escalate (can't triage over phone).

## 2. Real caller archetypes

- **New patient booking** — has never been in, needs new-patient exam + X-rays, longest form to fill out, most FAQ questions upfront (insurance, cost, what to bring, parking).
- **Recall patient** — 6-month cleaning due, knows the routine, wants "the earliest morning slot" or "same time as last year." Fast turn, minimal info.
- **Emergency / pain** — swollen face, broken tooth, lost filling, kid fell. Needs SAME-DAY or NEXT-DAY. Wrong to try to schedule two weeks out. Right receptionist triage: "when did it start, how bad on 1-10, any swelling, any bleeding — I can get you in this afternoon at 2:30."
- **Follow-up / recheck** — post-procedure. Free if within 30 days of original visit. Needs to be WITH THE SAME PROVIDER who did the original work (continuity of care). Common shapes: implant osseointegration check at 3-4 months, root canal follow-up crown placement, post-op check after extraction.
- **Insurance question only** — "do you take Delta Dental?" — no intent to book yet. Give the answer, offer to book if they want.
- **Cancellation / reschedule** — has existing appointment. Needs their phone or name to look it up. Different flow from new booking.
- **Referral inquiry** — "my doctor referred me for..." — usually needs the specialist, not the generalist. Different scheduling rules (longer slots, sometimes different location).
- **Anxious / phobic** — "I haven't been to the dentist in 5 years, I'm scared." Needs a warm receptionist voice, mention sedation options if the practice offers them, offer a consultation-only first visit.
- **Parent booking for child** — kid slot, pediatric-specialist provider if practice has one, insurance under parent's name.
- **Spanish-speaking / bilingual need** — "does anyone there speak Spanish?" Practice may have a bilingual hygienist or offer a translator line. Real receptionist knows immediately.

## 3. Full service catalog

Real dental menu at the granularity a receptionist knows. (Vet + general medical differ; note the vertical if you build those playbooks.)

- **Diagnostic**
  - New patient exam with X-rays — 60min, first visit, most complete
  - Adult recall exam — 30min, returning patient, 6-month standing
  - Pediatric first visit — 45min, under 12
  - Emergency exam — 30min, same-day
  - Consultation (implant / Invisalign / cosmetic) — 45-60min, treatment planning
- **Preventive**
  - Adult cleaning (prophy) — 45min, standard hygienist visit
  - Pediatric cleaning — 30min
  - Deep cleaning (SRP scaling and root planing) — 60-90min, gum disease
  - Fluoride treatment — 15min add-on
  - Sealants — 30min, usually pediatric
- **Restorative**
  - Composite filling (1-3 surfaces) — 45min, price varies by surfaces
  - Crown — 90-120min, may be 2 visits (prep + placement)
  - Bridge — 2-3 visits, 90min each
  - Denture (partial, full) — multi-visit, weeks
  - Root canal (endo) — 60-90min, sometimes referred to specialist
- **Cosmetic**
  - Zoom whitening (in-office) — 90min
  - Take-home whitening trays — 30min fitting
  - Veneers — consultation + 2 visits, weeks
- **Ortho**
  - Invisalign consultation — 45min, free at many practices
  - Invisalign treatment start — 90min, includes scan + first trays
  - Traditional braces consult — same as Invisalign consult
- **Surgical**
  - Simple extraction — 30-45min
  - Surgical extraction — 60min, may need referral to oral surgeon
  - Wisdom teeth (all four) — 90-120min, oral surgeon
  - Implant placement — 90min, often specialist
- **Follow-up / recheck**
  - Post-procedure follow-up — 30min, often FREE within 30 days, otherwise $75
  - Post-antibiotic recheck — 15-30min
  - Implant integration check — 30min, 3-4 months after placement
  - Second-visit of two-visit treatment — variable duration

## 4. Ambiguous requests → clarification

- **"A follow-up"** → follow-up to WHAT procedure? WITH WHICH provider? WHEN was original visit? These three determine duration + price + which slot to search + whether the free-within-30-days rule applies. Note: "A follow-up" is not an ambiguous SERVICE — it is an under-specified INTENT. Do NOT treat it like "a cleaning" where the question is which service; treat it like a returning-patient chart lookup. Enforce via a DISCOVER_CONTEXT dialogue-policy branch that fires BEFORE ASK_SLOT(phone). Additional dual-signal note: the phrase itself is the returning-patient marker — no chart-lookup gate should require the caller to say "I've been in before". See `docs/product/journey-audit-follow-up-clinic-2026-08-29.md` for turn-by-turn spec.
- **"A cleaning"** → adult vs pediatric? regular vs deep? insurance covered? Prefer their usual hygienist if returning patient.
- **"A check-up"** → new patient vs recall? Adult vs pediatric? Different durations + intake forms.
- **"An exam"** → same as check-up. Also could be emergency exam if pain-context.
- **"Consultation"** → for what? Invisalign / implants / cosmetic / second opinion? Different providers, different durations.
- **"An extraction"** → simple (any dentist) or surgical/wisdom (oral surgeon)? Which tooth? Emergency vs planned?
- **"Something's hurting"** → PAIN triage first, not calendar. When did it start? How bad 1-10? Swelling? Bleeding? Get them in TODAY if severe.
- **"Just a look"** → probably new patient exam without X-rays if new. But X-rays usually mandatory on first visit for baseline.
- **"My kid needs something"** → age (pediatric specialist vs adult practitioner), what happened (emergency vs recall vs first visit).

## 5. Real failure modes (ordered by frequency)

- **False-complete follow-up (wrong provider + wrong duration + wrong price rule)** — the compound Christiaan-shape failure. Booking "succeeds" for `Follow-up visit` at 30min with the next-open dentist, no link to the original procedure, no continuity-of-care check, no 30-day free-window verification. Detection: `Follow-up visit` booking with empty `notes` and no `original_procedure` / `original_provider` populated. This is the canonical false-complete for the vertical — deserves its own row above the individual failures below.
- **Booked wrong duration** — 30min follow-up when a 90min consultation was actually needed. Cascades: patient shows up, real work can't be done in the slot, has to reschedule, real slot is 3 weeks out, patient loses trust.
- **Wrong provider** — booked with generalist when specialist required (surgical extraction, endo, ortho). Same cascade as above.
- **Skipped insurance verification** — quoted cash price when patient's covered. Patient shows up expecting free and gets billed, or worse the front desk collects and has to refund.
- **Missed same-day emergency** — treated a pain call like a regular booking, scheduled 2 weeks out. Patient goes to urgent care or ER.
- **Wrong provider gender preference** — some patients (especially certain religious / cultural backgrounds) prefer same-gender provider. Real receptionist notes preference; AI often doesn't.
- **Missed the referral trigger** — patient mentions specialty need (endo, oral surgery), booked with generalist who then refers out — wasted an appointment.
- **Didn't offer alternate provider** — patient's usual doctor is booked 3 weeks out; receptionist should offer other providers same week rather than lose them.
- **Meds / allergy question skipped** — didn't ask about anesthesia allergies or blood thinners for procedures where it matters.
- **Family-name overload** — "Smith" booked for the wrong patient in the same family (mom vs daughter). Real receptionist confirms DOB.
- **Called back at wrong time** — patient works nights, receptionist calls back at 10am to confirm and wakes them.

## 6. Regulatory + safety

- **MUST say:** AI disclosure when directly asked ("am I talking to a person?" → "I'm an automated receptionist for Smile Dental, but I can connect you to a real person if you'd like.")
- **MUST NOT say:** medication dosing, drug interaction advice, diagnostic claims ("that sounds like an infection" — no, receptionist doesn't diagnose)
- **MUST NOT persist:** clinical symptom details in CRM without patient consent (HIPAA). Booking + phone + name OK; "toothache started 3 days ago severity 7" is protected.
- **MUST escalate:** severe pain 8+/10, active bleeding, facial swelling near eye/throat, trauma with LOC, suicidal ideation, medication overdose questions.
- **Consent for recording:** two-party consent states require notice at call open (California, Florida, most of New England).

## 7. Cross-sell / upsell opportunities

Legitimate value-adds a real receptionist raises:

- New patient calling — offer to email intake forms before visit (saves 15min at reception).
- Recall booked — mention "you're due for X-rays" if patient hasn't had a bitewing in 12+ months.
- Cleaning booked — offer fluoride add-on if under insurance.
- Emergency slot booked — send SMS with "arrive 15 minutes early, bring insurance card, plan for X-rays."
- Consultation for Invisalign — mention financing option upfront so caller doesn't ghost after seeing price.
- Whitening inquiry — mention take-home tray option if in-office is out of budget.

## 8. Sources

- Real call transcripts: `docs/transcripts/` (start with README.md for the index).
- Christiaan's original call: `CA2fa1fef2065a7df388c3d6f58d7a7792` in `data/call_events.db` — verbatim Dutch mobile capture + "A follow-up" trigger.
- Sample fixture: `sample-data/clinic/business.json`.
- Industry benchmark on missed-emergency conversion cost: [add source when found]
- HIPAA reference: US HHS OCR guidance on telephone protected health information.
