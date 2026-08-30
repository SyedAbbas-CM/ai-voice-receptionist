# Clinic (dental / medical / vet) — product playbook

Domain-knowledge reference for the product-lead agent when reviewing
receptionist features for clinic tenants.

Status: **REFERENCE-QUALITY on US dental (Smile Dental Clinic shape).**
This is the anchor vertical for the whole receptionist product — every
architectural pattern (chart-lookup gate, container-shaped services,
phonetic-name persistence, pain-first triage, HIPAA scaffolding) was
proven here first, then generalized. Filled to Smile Dental demo depth
using the tenant fixture at `sample-data/clinic/business.json`, the
Christiaan follow-up bisection (`CA2fa1fef2...`), and 9 real call
transcripts. Vet + general medical adjacencies noted where the shape
diverges — those verticals need their own playbooks before deploy.
All statutory / regulatory content is marked
`[VERIFY WITH COMPLIANCE OFFICER]`, `[VERIFY WITH STATE DENTAL BOARD]`,
or `[VERIFY WITH LOCAL COUNSEL]` — the receptionist must never quote
these to a caller as advice, and this playbook is not legal reference.

Last updated: 2026-08-30 by clinic playbook enrichment session (brought
to real-estate-playbook depth: staffing patterns split by practice
shape, per-jurisdiction regulatory detail, expanded archetypes, full
service catalog with pricing bands, expanded ambiguous-request catalog,
expanded failure modes with detection signals, expanded regulatory +
safety, expanded cross-sell keyed to archetypes, sources block with
'known' vs 'TODO' explicit gaps).
Previous updates: 2026-08-30 by persona-ladder session (added canonical
intent-label enum, promoted expat/foreign-phone follow-up caller to
sub-persona status, added four known-gap archetypes); 2026-08-30 by
golden-scripts session (surfaced language-enumeration gap, phonetic-
name persistence gap, prompt-injection archetype); 2026-08-29 by
voice-agent session (spawned during Christiaan follow-up bisection).

## 1. Business shape

### Staffing patterns

- **Solo private practice (Smile Dental shape, single dentist +
  hygienist + assistant + one front-desk lead):** the dominant US
  dental shape by count. One licensed dentist owns and operates.
  Reception is one person (Alex in the fixture) who knows every
  patient by voice. Same-week continuity of care is a given. The
  front desk answers everything except acute clinical questions.
  Common ratios: 1 dentist, 1-2 hygienists, 1-2 assistants, 1
  front-desk. Recall + emergency + new patient all funnel through
  the same line.
- **Small group practice (2-4 dentists sharing overhead):** two or
  three general dentists plus one specialist (endo / perio / ortho
  / oral surgeon) either on staff part-time or referred to a nearby
  partner. Reception is 1-2 people. Common in suburban US markets
  (Plano, Frisco, Sugar Land shape) — Smile Dental is at the small
  end of this bucket. Provider-specific scheduling is a real
  constraint — Dr. Chen may not be there Fridays; Dr. Ramanathan
  (Invisalign) may only be there Tue/Thu.
- **DSO-owned practice (Dental Service Organization — Heartland,
  Aspen, Pacific Dental, Smile Brands):** looks like a private
  practice on the outside but the DSO parent handles billing,
  insurance, HR, marketing, and often centralized call-routing.
  Reception may be OUTSOURCED to a call center that handles
  multiple DSO practices simultaneously — receptionist doesn't
  know local providers by name. High-volume production culture:
  hygiene recall + treatment plan upsells are metrics-tracked.
  Reception script is often DSO-authored, not local.
- **Hospital-affiliated dental clinic (academic medical center,
  VA, community health center):** insurance mix includes Medicaid
  and sliding-scale. Multi-provider always. Reception is
  hospital-system reception; wait times to book new patient
  weeks-to-months. Emergency triage is handled by the ER, not the
  dental clinic reception.
- **Mobile / house-call dentistry (elderly + disability
  populations):** reception is scheduling geography + van routes,
  not chairs. Very different flow, usually a specialist tenant
  shape — not covered in this playbook version.
- **Specialist-only practice (endodontist / periodontist /
  orthodontist / oral surgeon / pediatric dentist):** referral-in
  is the dominant new-patient path. Reception asks "who referred
  you" as an early turn. Insurance mix skews toward the specialist
  benefit (some plans have separate endo / ortho maximums).
  Different appointment durations (endo appointments are 90-120
  min, ortho check-ins 15 min, oral surgery extractions 60-90 min
  including recovery).

### General dentist vs specialist

- **General dentist (GP):** exam, cleaning, filling, crown,
  extraction (simple), root canal (some do them, some refer),
  bridge, denture, whitening, veneer (some), basic implant
  restoration on an already-placed implant.
- **Endodontist:** root canal specialist. 2-3 year post-DDS/DMD
  residency. Handles complex canals, retreatments, apicoectomies.
  Referral-in typical.
- **Periodontist:** gums + bone specialist. Deep scaling (SRP),
  gum grafting, bone grafting, some implant placement. Referral-in
  for advanced perio.
- **Orthodontist:** braces + Invisalign + jaw-alignment. Long
  treatment relationships (18-36 months typical). Referral-in OR
  direct-to-consumer for Invisalign.
- **Oral surgeon (OMS):** surgical extractions, wisdom teeth,
  implant placement, jaw surgery, biopsies. MD or DDS/DMD +
  4-6 year residency. Referral-in typical.
- **Pediatric dentist (pedodontist):** kids 0-12 (some to 18).
  Behavior guidance, papoose board, minimal sedation for
  procedures. Some general dentists (Dr. Whitfield in the fixture)
  are "pediatric-friendly" without formal pedo residency —
  reception must know the distinction.
- **Prosthodontist:** dentures, complex restorative, full-mouth
  reconstruction. Rarer.

### Hours patterns

- **Weekday standard:** Mon-Fri 7:30 or 8:00 to 5:00 typical.
  Smile Dental fixture: 07:30-17:00 M-W, until 19:00 Thu, 15:00
  Fri, 08:00-13:00 Sat, closed Sun.
- **Late day:** at least one weekday night until 6-7 PM for
  working professionals who can't take time off during the day.
  Thursday is the most common late day in US markets.
- **Saturday half-day:** family practices + pediatric practices
  often 8-1 or 9-1 Saturday. Books out fastest for kids' cleanings
  because parents don't want to pull them from school.
- **Never Sunday** except emergency line. After-hours dental
  emergencies route to the ER for uncontrollable bleeding /
  facial trauma / airway swelling; otherwise the on-call dentist
  offers phone triage + first-thing-Monday booking.
- **Peak demand windows:**
  - Cleanings/recall: weekday evening (5-7 PM) + Saturday morning.
  - New patient exams: weekday mornings (patients want to get it
    over with).
  - Emergency: any time, but skewed morning after a bad night.
  - Kids: after school (3-5 PM) + Saturday morning.
  - Cosmetic (Zoom, veneers, Invisalign): weekday afternoons
    (patients take a long lunch).

### Physical footprint

- Single-location dominant for private practices. Multi-location
  DSO groups exist but each site has its own reception line +
  calendar (some DSOs centralize the call center — see staffing
  above).
- Chair count: 3-6 chairs is a small practice, 8-12 chairs is a
  medium DSO, 20+ is a large group / academic clinic.
- Waiting room + reception desk + business office (for
  insurance-heavy billing questions).

### Payment mix

- **Insurance-first, cash-secondary.** US dental insurance is
  category-heavy: preventive (100% covered typically), basic
  restorative (80% after deductible), major restorative (50%
  after deductible), ortho (separate lifetime max).
- **In-network vs out-of-network:** materially different patient
  cost. Smile Dental fixture: in-network with Delta Dental PPO,
  Cigna DPPO, Blue Cross Blue Shield of Texas Dental, United
  Concordia (military). Out-of-network means patient pays full
  fee, insurance reimburses at a lower "usual and customary" rate,
  patient owes the balance.
- **PPO / DPPO / HMO / Medicaid conventions:**
  - **PPO (Preferred Provider Organization):** patient can see any
    provider, better rates in-network. Most common US dental
    insurance.
  - **DPPO (Dental PPO):** same shape, dental-specific.
  - **HMO / DHMO:** capitated (dentist paid per-patient-per-month
    regardless of usage). Patient MUST use assigned dentist. Lower
    premiums, tighter access. Uncommon in dental.
  - **Medicaid / CHIP:** state-administered, adult coverage varies
    dramatically by state (some states cover emergency only;
    others cover full preventive). Kids coverage more consistent
    under CHIP. [VERIFY WITH STATE MEDICAID OFFICE] for
    per-state rules.
  - **Capitation vs FFS:** FFS (fee-for-service) is per-procedure
    billing (the norm). Capitation is per-patient-per-month
    (HMO/DHMO shape).
- **Cash pay:** discount plans (many practices offer 10-20% off
  for cash-up-front); in-house membership plans ($300-500/year
  covers 2 cleanings + exams + X-rays + 20% off other services);
  straight cash pay at published fee schedule.
- **Financing:** CareCredit + LendingClub Patient Solutions +
  Sunbit + in-house payment plans for treatment plans >$1000.
  Smile Dental fixture: in-house payment plans + CareCredit.
- **Copays:** collected at check-in for insured patients. The
  reception script often includes a copay quote in the confirmation
  ("your copay today will be around $45").

### Regulatory shape (US federal + state)

- **HIPAA:** federal patient-privacy law. Applies to any
  "protected health information" (PHI) — patient name + phone +
  DOB + insurance + clinical history + appointment reason. See §6
  for the full scope. [VERIFY WITH COMPLIANCE OFFICER]
- **State dental board licensing:** each US state has its own
  dental board. Provider licensure does NOT transfer between
  states (a Texas-licensed dentist cannot practice in California
  without a California license). Some reciprocity for
  military-adjacent moves. Sedation permits are separate from
  base license — a dentist can be licensed but not sedation-
  permitted. [VERIFY WITH STATE DENTAL BOARD]
- **DEA registration:** required for any dentist prescribing
  controlled substances (opioid pain relievers, benzodiazepines
  for oral sedation). Per-state registration, separate from
  dental license. Receptionist NEVER discusses controlled-
  substance scripts on the phone — refer to prescribing
  provider.
- **CDC infection-control guidelines:** post-COVID heightened.
  Practices publish sterilization + PPE protocols. Receptionist
  may be asked "what infection-control do you do" — safe answer
  is "we follow CDC and OSHA guidelines; want me to email you
  our written policy?" — do NOT invent specifics.
- **ADA (American Dental Association) guidelines:** ethical
  advertising (no misleading before/after photos), informed
  consent for procedures, treatment plan disclosure with cost
  itemization before starting work.
- **State AI-disclosure laws:** Utah Consumer AI Protection Act
  (CACPA, effective 2024), Texas AI-disclosure requirements,
  California BOT Disclosure Law (SB 1001), Colorado AI Act.
  Any voice AI must disclose on direct question. Some
  jurisdictions require proactive disclosure. [VERIFY WITH
  COMPLIANCE OFFICER]
- **State two-party recording consent:** California, Florida,
  Illinois, Maryland, Massachusetts, Michigan, Montana, Nevada
  (2-party), New Hampshire, Pennsylvania, Washington. Notice at
  call open required. [VERIFY WITH LOCAL COUNSEL]
- **No Surprises Act (2022):** requires good-faith estimates for
  uninsured / self-pay patients on scheduled services. The
  receptionist quote for a self-pay new patient exam counts as
  part of the GFE process. [VERIFY WITH COMPLIANCE OFFICER]
- **State-level fair-billing laws** (California AB 3087,
  Colorado HB 21-1232, others) add additional cost-disclosure
  requirements on scheduled dental care. [VERIFY WITH LOCAL
  COUNSEL]
- **EU AI Act Article 50** (in force 2 Aug 2026) — applies only
  if the practice serves EU residents (rare for a US practice;
  possible for border-town practices near the US-Canada border,
  cruise-port practices, or medical-tourism operators). Not the
  default assumption for the vertical.
- **Emergency escalation:** state Good Samaritan laws protect
  medical advice given in good faith, but receptionist should
  NEVER give medical advice regardless. Escalate to on-call
  provider or route to ER / 911. See §6.

## 2. Real caller archetypes

### Booking-side

- **New patient booking** — has never been in, needs new-patient
  exam + X-rays, longest form to fill out, most FAQ questions
  upfront (insurance, cost, what to bring, parking). Longest call.
  De-risking the practice before committing.
- **Recall patient (returning-patient short-turn)** — 6-month
  cleaning due, knows the routine, wants "the earliest morning
  slot" or "same time as last year." Fast turn, minimal info.
  Expects the call to complete in under 90 seconds.
- **Cancellation / reschedule** — has existing appointment. Needs
  their phone or name to look it up. Different flow from new
  booking; often merged with recall in analysis but distinct in
  execution (no availability search needed if cancelling).
- **Emergency / pain caller** — swollen face, broken tooth, lost
  filling, kid fell. Needs SAME-DAY or NEXT-DAY. Wrong to try to
  schedule two weeks out. Right receptionist triage: "when did
  it start, how bad on 1-10, any swelling, any bleeding — I can
  get you in this afternoon at 2:30." Escalation trigger:
  severity ≥ 8, active bleeding, facial swelling near eye/throat,
  loss of consciousness, breathing issue.
- **Follow-up / recheck (post-procedure)** — highest-risk
  archetype for silent failure in this vertical. Christiaan
  case. Free if within 30 days of original visit. Needs to be
  WITH THE SAME PROVIDER who did the original work (continuity
  of care). Common shapes: implant osseointegration check at
  3-4 months, root canal follow-up crown placement, post-op
  check after extraction.
  - **Sub-persona: expat / foreign-phone follow-up caller
    (Christiaan shape).** Chart is on file but the number they're
    calling from is non-US-region or differs from the chart
    number. Failure mode: agent silently falls back to "new
    patient" branch. Fix: `chart_not_found_under_this_number`
    must trigger "what number would we have you under" before
    booking, never a silent branch. See
    `docs/product/journey-audit-follow-up-clinic-2026-08-29.md`.
- **Anxious / phobic patient** — "I haven't been to the dentist
  in 5 years, I'm scared." Needs a warm receptionist voice,
  mention sedation options if the practice offers them, offer a
  consultation-only first visit. Tone must audibly soften on the
  agent's SECOND turn once the phobic signal is picked up.
- **Parent booking for child** — kid slot, pediatric-specialist
  provider if practice has one, insurance under parent's name.
  After-school or Saturday morning preferred. Chart under kid's
  name, responsible party under parent's.
- **Referral / specialty inquiry** — "my doctor referred me
  for..." — usually needs the specialist, not the generalist.
  Different scheduling rules (longer slots, sometimes different
  location). Should mention the referring doctor's name and the
  specialty procedure.

### Information-only side

- **Insurance / cost inquiry (no booking intent yet)** — "do you
  take Delta Dental?" — no intent to book yet. Give the answer,
  offer to book if they want, don't push.
- **Insurance claims dispute** — "I got billed for X, I thought
  it was covered." NOT the receptionist's job — route to
  business office / billing manager. Different phone tree branch
  in bigger practices.
- **New insurance card / update on file** — patient's carrier
  changed at open enrollment, wants to update the record before
  their next appointment. 30-second call in a good system;
  requires patient chart lookup + insurance re-verification.
- **Facility / location question** — parking, entrance, ADA
  access, kids' area, waiting-room time, WiFi. Straightforward
  FAQ.
- **Provider question** — "does Dr. Chen still work there?",
  "who does implants?", "is Dr. Whitfield accepting new
  patients?". Requires per-provider status field in the
  fixture — currently thin.

### Language / accessibility

- **Spanish-speaking / bilingual need** — "does anyone there
  speak Spanish?" Practice may have a bilingual hygienist (Rosa
  in the fixture) or offer a translator line. Real receptionist
  knows immediately.
- **Non-supported-language caller** — opens in a language nobody
  on shift speaks (Vietnamese, Mandarin, Farsi, ASL video-relay
  common in TX). Real receptionist doesn't fake comprehension;
  offers callback in language, translator line, or family-member-
  on-the-line. Requires tenant to publish a `languages_supported[]`
  list, not just per-language FAQs.
- **Interpreter-on-line caller** — family member interpreting
  for the patient in real time. Three-party turn-taking. Slower
  cadence. Chart is under the patient's name, not the interpreter.

### Non-patient callers

- **Sales / vendor caller** — medical supply, staffing agency,
  dental lab, insurance auditor, marketing pitch. Not the
  receptionist's job. Fast polite deflection to a business-line
  callback. Tenant should have a "sales inquiries after 4 PM"
  policy — reception says "our office manager handles vendor
  calls after 4 PM Monday-Thursday, want to leave a message?"
- **Wrong-number caller** — dialed us by mistake. Handle in 1-2
  turns without frustrating them. "You've reached Smile Dental
  in Plano — did you mean to call a different number?"
- **Referring-doctor's office** — the primary care office
  calling to check that a referral went through, confirm the
  patient booked, coordinate records. Route to business office
  in bigger practices; front desk handles in small practices.
- **Chart-holder-relative caller** — "I'm calling for my father
  / my elderly mother / my husband who's at work." Overlaps
  with parent-for-child on the "different-person-on-chart"
  dimension but distinguishes on age + consent implications
  (can this caller book, agree to a slot, get insurance quotes
  on the chart-holder's behalf). Likely needs a HIPAA-consent
  flow we don't have — see §6 + product gaps.
- **Elderly / hard-of-hearing patient** — louder voice, "can
  you repeat that", requests for slower speech, sometimes a
  family member speaking on their behalf. Needs slower speech
  rate, shorter sentences, per-data-point confirmation. No
  transcript evidence yet — playbook stub.
- **Chatty / lonely caller** — real archetype for elderly
  patients; wants conversation as much as the appointment.
  Served well by warmth without losing structure. Distinguishing
  signal is turn ratio + call duration relative to booking
  complexity.

### Edge / adversarial

- **Adversarial / prompt-injection caller** — usually a red-team
  test rather than a real patient; opens with "ignore all
  previous instructions" or "what tools do you have access to".
  Real receptionist stays in scope, refuses the meta-question
  without lecturing, snaps back to booking flow the moment the
  caller drops the adversarial thread. Also watch obviously-test
  phone numbers (555-555-5555, 000-000-0000).
- **Complaint / angry caller** — missed appointment, provider
  no-show, billing dispute, bad experience. Escalate to office
  manager. Never argue on the phone. Reception's job: acknowledge,
  don't defend, promise a callback from the manager within X
  hours.
- **AI-disclosure probe (not adversarial — legitimate)** — "am
  I talking to a person?" MUST answer honestly: "I'm an
  automated receptionist for Smile Dental, but I can connect
  you to a real person if you'd like." Different from
  prompt-injection.

### Canonical intent-label enum (do not rename)

Used by the Phase 4 golden-corpus regression sweep + the LK Phase 3
auto-judges. Any change here breaks tests. Full definitions in
`docs/product/persona-ladder-clinic-2026-08-30.md`.

```
NEW_PATIENT_BOOKING
RETURNING_SHORT_TURN
EMERGENCY_PAIN
FOLLOWUP_RECHECK
INSURANCE_COST_INQUIRY
ANXIOUS_PHOBIC
PARENT_FOR_CHILD
LANGUAGE_SCOPE
REFERRAL_SPECIALTY
META_ADVERSARIAL
UNKNOWN
```

Sub-personas the ladder documents but classifier doesn't (yet)
split: expat/foreign-phone follow-up, elderly/hard-of-hearing,
chart-holder-relative, sales/vendor, wrong-number, chatty/lonely,
interpreter-on-line, dental-tourism. Fold into parent classes for
now; split when transcript evidence supports it.

## 3. Full service catalog

Real dental menu at receptionist granularity. Vet + general medical
differ — note the vertical if you build those playbooks. Prices are
Smile Dental fixture values or [VERIFY WITH PRACTICE] ranges — prices
vary 30-50% between markets (Manhattan / SF > Plano > rural
Mississippi). Insurance-eligibility notes are USA in-network general
rules — always confirm with the patient's specific plan before
quoting.

### Diagnostic

- **New patient exam with X-rays** — 60 min. Smile Dental: $189
  cash. [VERIFY WITH PRACTICE] typical range $150-350 depending
  on market + X-ray series (bitewings + PA vs full-mouth series
  vs panoramic + CBCT). Provider: any general dentist (Dr. Chen
  or Dr. Whitfield in fixture). Insurance: covered in-network at
  100% typically (preventive category); out-of-network patient
  pays balance. Cross-sell: email intake forms 24h ahead
  (see §7). Sub-typing: "with X-rays" vs "without" — most
  practices require baseline X-rays on first visit unless the
  patient brings them from a prior dentist within 12 months.
- **Adult recall exam** — 30 min. Smile Dental: $95 cash. [VERIFY
  WITH PRACTICE] typical $75-150. Provider: any adult dentist,
  often paired with hygienist visit same day. Insurance: covered
  in-network at 100% (preventive). Cross-sell: X-ray add-on if
  last set >12 months, fluoride if covered.
- **Pediatric first visit** — 45 min. Smile Dental: price NOT
  listed in fixture (gap). [VERIFY WITH PRACTICE] typical $100-
  200. Provider: Dr. Whitfield (fixture attributes "pediatric
  specialist"). Insurance: usually parent's plan; kid may be on
  separate CHIP if income-qualified. Cross-sell: fluoride,
  sealants at future visit if age-appropriate.
- **Emergency exam** — 30 min. Smile Dental: $115 cash. [VERIFY
  WITH PRACTICE] typical $100-250 depending on X-rays + palliative
  treatment included. Provider: any dentist on floor; Dr. Chen is
  on-call after hours per fixture FAQ. Insurance: category
  varies (some plans code as diagnostic, some as basic
  restorative). Required-info FIRST: pain_context (when started,
  severity 1-10, swelling, bleeding).
- **Consultation (container-shaped — Invisalign / implant /
  cosmetic / second opinion / sleep apnea):** duration + provider
  vary by consult type. Sub-types:
  - Invisalign consultation — 45 min, Dr. Ramanathan, FREE
    consult (Smile Dental fixture).
  - Implant consultation — 60 min, provider varies (Smile Dental
    fixture: unattributed; playbook: often oral surgeon or
    perio for placement, GP for restoration).
  - Cosmetic consultation (veneers, whitening plan, smile
    makeover) — 45-60 min.
  - Second opinion (patient wants a review of a treatment plan
    from another dentist) — 45 min. Insurance often does NOT
    cover second-opinion consults; cash-quote by default.
  - Sleep apnea / oral appliance consult — 60 min, requires MD
    sleep-study referral.

### Preventive

- **Adult cleaning (prophylaxis)** — 45 min. Smile Dental: $135
  cash. [VERIFY WITH PRACTICE] typical $95-200. Provider: Rosa
  Delgado (fixture). Insurance: covered in-network at 100%
  (preventive, 2x/year typical). Cross-sell: fluoride add-on
  (usually $25-45 not covered for adults).
- **Pediatric cleaning** — 30 min. [VERIFY WITH PRACTICE]
  typical $75-140. Provider: any hygienist, though pediatric-
  friendly hygienist preferred. Insurance: usually covered under
  parent's plan preventive tier.
- **Deep cleaning (SRP — scaling and root planing)** — 60-90 min,
  often quadrant-by-quadrant over 2-4 visits. [VERIFY WITH
  PRACTICE] typical $250-350 per quadrant. Provider: hygienist
  or periodontist. Insurance: category = basic restorative
  usually 80% covered after deductible with prior perio
  diagnosis. Cross-sell: perio maintenance recall every 3-4
  months (not standard 6-month recall).
- **Fluoride treatment** — 15 min add-on. [VERIFY WITH PRACTICE]
  $25-45. Covered for kids under 18 (age varies by plan),
  usually not for adults.
- **Sealants** — 30 min, usually pediatric (permanent molars, ages
  6-14). [VERIFY WITH PRACTICE] $40-70 per tooth. Covered under
  kids' preventive.
- **Perio maintenance recall** — 60 min, post-SRP recall every
  3-4 months. Different from prophy. Insurance sometimes has a
  cap on frequency (2x/year).

### Restorative

- **Composite filling (1-3 surfaces)** — 45 min. Smile Dental:
  starts at $245. [VERIFY WITH PRACTICE] typical $150-450
  depending on surfaces + tooth (molar > premolar > anterior).
  Insurance: category = basic restorative, 80% covered after
  deductible.
- **Amalgam (silver) filling** — 45 min, cheaper than composite,
  many practices no longer offer.
- **Crown** — 90-120 min, may be 2 visits (prep + placement)
  unless same-day CEREC. [VERIFY WITH PRACTICE] typical $900-
  1800 depending on material (PFM, zirconia, e.max, gold).
  Insurance: category = major restorative, 50% covered after
  deductible + annual maximum cap ($1000-2500). Often requires
  pre-authorization for the insurance to confirm coverage.
- **Bridge** — 2-3 visits, 90 min each. Multi-tooth prosthetic.
  [VERIFY WITH PRACTICE] typical $2000-5000+. Same insurance +
  pre-auth pattern as crown.
- **Root canal (endo)** — 60-90 min. [VERIFY WITH PRACTICE]
  $700-1600 depending on tooth (molar > premolar > anterior).
  Provider: GP does some; endodontist does complex + retreatment.
  Insurance: category = basic restorative, 80% covered typically.
  Often paired with a crown (root canal weakens tooth).
- **Post + core** — 30 min, follow-up to root canal before crown.
  Small charge, often bundled.
- **Onlay / inlay** — 90 min. Between filling and crown in
  size + cost. [VERIFY WITH PRACTICE] $600-1200.

### Cosmetic (usually cash-pay, insurance rarely covers)

- **Zoom whitening (in-office)** — 90 min. Smile Dental: $549.
  [VERIFY WITH PRACTICE] typical $400-900. Cross-sell: take-home
  tray option if in-office out of budget.
- **Take-home whitening trays** — 30 min fitting + patient does
  treatment at home over 2 weeks. [VERIFY WITH PRACTICE] $250-
  500. Cheaper than in-office.
- **OTC whitening recommendation** — receptionist can mention if
  cost is a barrier, but agent should NOT recommend a specific
  brand. Warm redirect to the dentist for advice.
- **Veneers** — consultation + 2 visits, weeks apart. [VERIFY
  WITH PRACTICE] $1000-2500 per tooth (porcelain), $250-1500
  (composite). Insurance almost never covers.
- **Smile makeover / full-mouth cosmetic plan** — multi-visit,
  usually $10k+. Escalate quote to dentist consultation.

### Ortho

- **Invisalign consultation** — 45 min. Smile Dental: FREE. All
  ortho practices consult free typically. Provider: Dr. Ramanathan.
- **Invisalign treatment start** — 90 min, includes scan + first
  tray delivery. Smile Dental: full treatment ~$4895. [VERIFY
  WITH PRACTICE] typical $3500-8500. Insurance: separate ortho
  lifetime max ($1000-3000 typical if plan has ortho rider).
  Financing (CareCredit) common.
- **Invisalign progress check** — 15-30 min, every 6-8 weeks.
- **Traditional braces consultation** — same as Invisalign
  consult. Same or lower price.
- **Braces adjustment** — 15-20 min, monthly.
- **Retainer** — 30 min post-treatment. Replacement retainers
  $150-400.

### Surgical

- **Simple extraction** — 30-45 min. [VERIFY WITH PRACTICE] $150-
  400. Provider: any GP. Insurance: category = basic restorative,
  80% covered.
- **Surgical extraction** — 60 min. [VERIFY WITH PRACTICE] $250-
  650. May need referral to oral surgeon.
- **Wisdom teeth (all four)** — 90-120 min. Oral surgeon.
  [VERIFY WITH PRACTICE] $800-3000 depending on impaction +
  sedation. Insurance: often covered under medical, not dental,
  when medically necessary — separate billing process.
- **Implant placement** — 90 min. Oral surgeon or periodontist
  or specially-trained GP. Smile Dental fixture: implant
  consultation only, placement provider unattributed. [VERIFY
  WITH PRACTICE] single implant + crown $3000-6000+ (Smile
  Dental: $4250+).
- **Bone graft / sinus lift** — surgical prerequisite for some
  implants. [VERIFY WITH PRACTICE] $500-3000+.
- **Biopsy / oral lesion evaluation** — 30-45 min. Oral surgeon
  or GP.

### Follow-up / recheck (container-shaped — highest failure risk)

Smile Dental fixture uses ONE `Follow-up visit` service with a
`duration_by_original_procedure` map for sub-typing. This is the
canonical container pattern for the vertical. Cross-reference:
`docs/product/service-taxonomy-clinic-2026-08-30.md`.

- **Post-procedure follow-up (generic)** — 30 min default.
  FREE within 30 days of original visit, $75 after (Smile Dental
  fixture rule). Duration overridden by
  `duration_by_original_procedure` map:
  - implant integration check: 30 min
  - root canal follow-up: 45 min
  - crown seat (second visit of two-visit): 60 min
  - extraction post-op: 15 min
  - antibiotic recheck: 15 min
  - filling check: 20 min
  - denture adjustment: 30 min
  - bridge cementation: 45 min
  - veneer placement: 30 min
- **Post-antibiotic recheck** — 15-30 min. Confirms infection
  resolved. Often free.
- **Implant integration check** — 30 min, 3-4 months after
  placement. Confirms osseointegration before restoration
  (crown placement).
- **Second-visit of two-visit treatment** — variable duration
  per original procedure. Included in original quote typically.
- **Denture fitting / adjustment** — 30-60 min. Chair-time to
  refine bite + eliminate sore spots. Multiple visits common.

### Container-shaped services (multiple sub-types under one
receptionist name)

Sub-typing hint: the following container names ALL need `service_
subtype` capture before booking to avoid mis-duration + mis-price:

- **Follow-up visit** — sub-typed by `original_procedure` (see
  above).
- **Consultation** — sub-typed by consult purpose (Invisalign,
  implant, cosmetic, second opinion, sleep apnea).
- **Cleaning** — sub-typed by adult vs pediatric AND prophy vs
  SRP vs perio maintenance.
- **Extraction** — sub-typed by simple vs surgical vs wisdom.
- **Denture** — sub-typed by fitting vs adjustment vs
  replacement vs cleaning.
- **X-rays** — sub-typed by bitewing (recall) vs FMX (full-mouth
  series, new patient) vs panoramic (implant / wisdom) vs
  CBCT (implant placement, endo diagnosis).

## 4. Ambiguous requests → clarification

Concrete ambiguity examples the receptionist MUST clarify — each is
a DISCOVER_CONTEXT branch before any tool call.

- **"A follow-up"** → follow-up to WHAT procedure? WITH WHICH
  provider? WHEN was original visit? These three determine
  duration + price + which slot to search + whether the
  free-within-30-days rule applies. Note: "A follow-up" is not
  an ambiguous SERVICE — it is an under-specified INTENT. Do
  NOT treat it like "a cleaning" where the question is which
  service; treat it like a returning-patient chart lookup.
  Enforce via a DISCOVER_CONTEXT dialogue-policy branch that
  fires BEFORE ASK_SLOT(phone). Additional dual-signal note:
  the phrase itself is the returning-patient marker — no
  chart-lookup gate should require the caller to say "I've been
  in before". See
  `docs/product/journey-audit-follow-up-clinic-2026-08-29.md`
  for turn-by-turn spec.
- **"A cleaning"** → adult vs pediatric? Regular vs deep (SRP)
  vs perio maintenance? Insurance covered? Prefer their usual
  hygienist if returning patient.
- **"A check-up"** → new patient vs recall? Adult vs pediatric?
  Different durations + intake forms. Ambiguous with New patient
  exam vs Adult recall — disambiguate by "have you been in
  before?" or by chart-lookup on phone.
- **"An exam"** → same as check-up. Also could be emergency exam
  if pain-context. Probe pain first.
- **"Consultation"** → for what? Invisalign / implants /
  cosmetic / second opinion / sleep apnea? Different providers,
  different durations, different pricing.
- **"An extraction"** → simple (any dentist) or surgical / wisdom
  (oral surgeon)? Which tooth? Emergency vs planned? If wisdom
  teeth, do all four at once or one at a time?
- **"Something's hurting"** → PAIN triage first, not calendar.
  When did it start? How bad 1-10? Swelling? Bleeding? Get them
  in TODAY if severe. Escalate to ER if severity 8+ + facial
  swelling near eye/throat + trauma with LOC.
- **"Just a look"** → probably new patient exam without X-rays
  if new. But X-rays usually mandatory on first visit for
  baseline. Also could be a phobic caller wanting consultation-
  only first visit.
- **"My kid needs something"** → age (pediatric specialist vs
  adult practitioner), what happened (emergency vs recall vs
  first visit). Age drives duration + provider + insurance
  chart. Injury → same-day. Discoloration → cosmetic path.
- **"A whitening"** → in-office Zoom (90 min, $549 at Smile
  Dental) vs take-home trays (30 min fitting + 2 weeks at
  home, ~$250-500) vs OTC recommendation (warm redirect to
  dentist)? Cost sensitivity often drives the answer.
- **"Insurance question"** → verify coverage for a specific
  service (route to insurance-verification flow) vs claims
  dispute on a past bill (route to business office) vs new
  insurance card / update on file (chart-update flow). All
  different.
- **"Implants"** → initial consult (60 min, no work) vs
  single-tooth placement vs full-arch (All-on-4) vs peri-
  implantitis / integration check vs implant restoration
  (crown on existing implant)? Different providers, different
  cost bands ($4k to $50k+).
- **"Wisdom teeth"** → evaluation (30 min consult + panoramic
  X-ray) vs extraction booking (90-120 min oral surgery slot)
  vs post-op check (15-30 min recheck)? First-time wisdom
  question: usually needs the evaluation first — panoramic to
  confirm impaction + sedation planning.
- **"A cavity"** → which tooth? How long has it hurt? Filling
  type preference (composite vs amalgam)? Sometimes the caller
  self-diagnoses and it's actually pulpitis (needs endo) or
  cracked-tooth syndrome (needs crown) — MUST see the tooth,
  don't quote a cavity-fix price on the phone.
- **"Root canal"** → evaluation + diagnosis (60-90 min endo
  exam) vs treatment start (root canal appointment 60-90 min)
  vs completion (post + core + crown seat)? Also: is this a
  referral-in from a GP endo failure (retreatment — endodontist
  specialist only)?
- **"My dentures"** → new fitting (multi-visit weeks) vs
  adjustment (30 min chair time) vs replacement (multi-visit)
  vs cleaning (30 min hygiene)? Denture callers often mean
  "adjustment" — sore spot, bite issue — but the word does not
  clarify.
- **"How much is it"** → without a specific service anchor, this
  is unanswerable. Reception must anchor to a service before
  quoting. If the caller says "just anything, ballpark" — offer
  the new-patient exam price as the standard entry-point cost
  ($189 at Smile Dental) and offer to send a full fee schedule.
- **"Do you take my insurance"** → carrier name is required. If
  they name a plan the practice takes, confirm. If they name a
  plan the practice doesn't take, offer the cash rate + explain
  the out-of-network reimbursement path.
- **"I need to see the dentist"** → for what? Pain (emergency),
  cleaning (preventive), or checkup (recall or new patient)?
  Also confirm they've been in before (chart lookup) — the word
  "the" implies continuity.
- **"Is it covered"** → for WHAT? Coverage varies wildly by
  category (preventive 100%, basic restorative 80%, major 50%,
  ortho separate max, cosmetic 0%). Do NOT answer "yes,
  probably" — always tie to the specific service.
- **"Can I get an appointment"** → for what? When? Sounds like
  a booking request but has no service anchor yet. Ask "what
  brings you in?" NOT "which service."

## 5. Real failure modes

Ordered by frequency × severity within tiers. Each includes
detection signal for automated regression + prevention rule.

### High-severity, high-frequency

- **False-complete follow-up (wrong provider + wrong duration +
  wrong price rule)** — the compound Christiaan-shape failure.
  Booking "succeeds" for `Follow-up visit` at 30min with the
  next-open dentist, no link to the original procedure, no
  continuity-of-care check, no 30-day free-window verification.
  Detection signal: `Follow-up visit` booking with empty `notes`
  and no `original_procedure` / `original_provider` populated.
  This is the canonical false-complete for the vertical —
  deserves its own row above the individual failures below.
  Prevention: DISCOVER_CONTEXT gate + chart lookup BEFORE
  ASK_SLOT(phone).
- **Booked wrong duration** — 30 min follow-up when a 90 min
  consultation was actually needed. Cascades: patient shows up,
  real work can't be done in the slot, has to reschedule, real
  slot is 3 weeks out, patient loses trust. Detection: booking
  duration deviates from service canonical duration by >15 min
  without an explicit override note. Prevention: enforce
  container-shaped-service sub-typing before booking.
- **Wrong provider** — booked with generalist when specialist
  required (surgical extraction, endo, ortho). Same cascade as
  above. Detection: booking service class + provider mismatch
  (e.g., "wisdom teeth" booked with a GP not an oral surgeon).
  Prevention: service-catalog provider-eligibility list.
- **Skipped insurance verification** — quoted cash price when
  patient's covered. Patient shows up expecting free and gets
  billed, or worse the front desk collects and has to refund.
  Detection: booking created for a patient with insurance on
  chart, but call transcript never mentions the carrier.
  Prevention: insurance verification prompt before quoting cash.
- **Missed same-day emergency** — treated a pain call like a
  regular booking, scheduled 2 weeks out. Patient goes to
  urgent care or ER. Detection: booking created for `Emergency
  exam` service more than 2 business days out, OR non-emergency
  service booked when pain-context signals present in transcript.
  Prevention: pain-context detection triggers same-day slot
  search first.
- **Missed CT scan / panoramic X-ray requirement for implant
  consult** — patient shows up for implant consult without the
  required imaging, appointment wasted, has to reschedule.
  Detection: implant consultation booked without a preceding or
  attached imaging appointment / referral note. Prevention:
  service-catalog pre-requisite check.
- **Booked new-patient without confirming X-rays intent** —
  patient assumed no X-rays, practice needs them for baseline,
  awkward moment at chair. Detection: new-patient exam booked
  without insurance-verification for X-ray coverage AND without
  a "no X-rays" note. Prevention: reception script mentions
  X-rays explicitly on new-patient bookings.
- **Missed pediatric-specialist requirement for very young child**
  — 3-year-old booked with adult GP instead of Dr. Whitfield.
  Kid melts down at chair, appointment wasted, family churns.
  Detection: pediatric booking with age <6 not routed to
  pediatric-specialist provider. Prevention: age-based provider
  gating.
- **Confused hygienist vs dentist visit** — patient thought
  "cleaning" meant an exam too, got 45 min with hygienist only,
  didn't see the doctor. Detection: patient complaint pattern +
  cleaning visit without paired exam. Prevention: reception
  disambiguates ("cleaning only, or cleaning plus a check-up
  with the doctor?").
- **Wrong provider for wisdom teeth** — booked with general
  dentist, needs oral surgeon referral, patient shows up and
  gets referred out. Detection: "wisdom teeth" language + non-
  oral-surgeon provider. Prevention: service-catalog specialist
  routing.
- **Insurance pre-auth needed but not caught (crown / implant /
  ortho)** — often requires pre-authorization or patient pays.
  Patient shows up, insurance denies at chair, has to pay full
  fee out of pocket. Detection: major-restorative or ortho
  booking without a preceding insurance pre-auth flag.
  Prevention: service-catalog pre-auth requirement + reception
  prompt ("your insurance may need to pre-approve — I'll get
  that started; usually takes 3-5 days").

### Medium-severity

- **Wrong provider gender preference** — some patients
  (especially certain religious / cultural backgrounds) prefer
  same-gender provider. Real receptionist notes preference; AI
  often doesn't. Detection: patient re-books with different
  provider after first visit. Prevention: provider-preference
  field on chart, ask on new-patient intake.
- **Missed the referral trigger** — patient mentions specialty
  need (endo, oral surgery), booked with generalist who then
  refers out — wasted an appointment. Same shape as wrong-
  provider but on the input side. Prevention: specialty
  keyword detection in service-request phase.
- **Didn't offer alternate provider** — patient's usual doctor is
  booked 3 weeks out; receptionist should offer other providers
  same week rather than lose them. Detection: booking cancelled
  or 3+ week wait accepted when other same-week providers are
  available. Prevention: proactive alternate-provider offer.
- **Meds / allergy question skipped** — didn't ask about
  anesthesia allergies or blood thinners for procedures where
  it matters. Detection: procedure booking without medical
  history flag. Prevention: procedure-eligibility checklist.
- **Family-name overload** — "Smith" booked for the wrong
  patient in the same family (mom vs daughter). Real
  receptionist confirms DOB. Detection: multiple charts under
  same last name + same phone. Prevention: DOB confirmation
  on chart lookup when family-shared phone.
- **Sedation planning missed** — patient anxious, needs minimal
  sedation (nitrous / oral) for the procedure, no prep. Patient
  shows up expecting help with anxiety, none prepared.
  Detection: anxious-caller intent + procedure booking without
  sedation flag. Prevention: sedation-option offer on
  anxious-caller path.
- **Pregnancy contraindication missed for elective X-rays /
  anesthesia** — pregnant patient books cleaning + X-rays,
  routine X-rays contraindicated (or need lead shielding +
  urgent-only justification), some anesthesia contraindicated
  in first trimester. Detection: patient self-discloses
  pregnancy in call, agent proceeds with elective imaging
  booking. Prevention: pregnancy-flag capture + service-
  eligibility contraindications.
- **Language mismatch after booking** — Spanish-speaking patient
  books, English-only hygienist assigned. Patient shows up,
  can't communicate. Detection: `spanish` FAQ triggered on
  call + non-bilingual provider assigned. Prevention:
  bilingual-provider preference stored on chart.
- **Called back at wrong time** — patient works nights,
  receptionist calls back at 10 am to confirm and wakes them.
  Detection: multiple no-answer callbacks. Prevention: capture
  `preferred_callback_time` + timezone.
- **Language scope faked** — non-English caller opens, agent
  guesses intent from tone and books wrong service, or defaults
  to "please try in English" and loses the patient entirely.
  Detection: booking service mismatch when caller's opening
  turn wasn't in a supported language. Prevention:
  `languages_supported[]` field on tenant + honest three-option
  offer (transfer if we speak it / callback in language /
  family member on the line).

### Low-severity but corrosive

- **Phonetic name lost across visits** — patient corrects
  pronunciation at first visit, next booking gets it wrong
  again because the phonetic sticks only to that one
  appointment's `notes` field, not to the patient chart.
  Detection: same phone number, same pronunciation correction
  logged on multiple bookings. Prevention: tenant-level
  `patient_notes[]` keyed by phone.
- **Provider constraint hidden until booking-time failure** —
  caller asks for a provider on a day the provider doesn't
  work; agent doesn't know the schedule pattern and either
  books with wrong provider silently or fails opaquely.
  Detection: booking attempted for provider on a day they
  aren't scheduled. Prevention: provider-day availability
  stated proactively when the caller names a provider + a day.
- **Caller memory overrides chart** — on follow-up flow the
  caller misremembers original visit date and agent quotes
  price based on caller's number instead of pulling chart.
  Chart is authoritative; agent should reverse the quote when
  chart contradicts. Detection: booking date-of-service in
  transcript diverges from chart date. Prevention: chart-first
  quoting rule.
- **HIPAA slip on shared voicemail / family-shared phone** —
  agent leaves a voicemail with clinical detail on a phone that
  isn't the patient's private number. Detection: clinical-
  language voicemail sent to non-primary contact. Prevention:
  no clinical detail in voicemails without explicit patient
  consent flag.
- **After-hours emergency call routed to voicemail instead of
  on-call line** — dental emergency at 8 PM lands in an
  after-hours message that says "we're closed, call back
  tomorrow" instead of transferring to on-call dentist.
  Detection: pain-severity emergency call outside business
  hours + no escalation transfer. Prevention: after-hours
  routing must include on-call number.
- **Recall reminder / no-show pattern lost** — patient no-shows,
  isn't flagged for follow-up outreach, chart goes cold.
  Detection: patient with 2+ no-shows in 12 months. Prevention:
  no-show flag on chart + outreach cadence.

## 6. Regulatory + safety

Clinical + healthcare regulatory surface is larger than most
verticals because the receptionist handles Protected Health
Information (PHI) + insurance-benefit conversations that touch
federal fair-billing law + state licensing + AI-disclosure. Treat
every rule below as [VERIFY WITH COMPLIANCE OFFICER] or [VERIFY
WITH STATE DENTAL BOARD] or [VERIFY WITH LOCAL COUNSEL] before
deploying to a new tenant or state.

### HIPAA (federal — Health Insurance Portability and Accountability
Act of 1996 + HITECH Act 2009)

- **Purpose disclosure at call open:** receptionist should
  identify the practice + note that the call may be recorded
  for quality / training. Two-party consent states require
  explicit notice. [VERIFY WITH COMPLIANCE OFFICER]
- **Minimum-necessary rule:** collect only the PHI needed for
  the booking task. Phone + name + reason-for-visit +
  insurance carrier are minimum-necessary for a booking. Full
  symptom description + medication list are NOT. Persist
  minimum-necessary only.
- **No clinical detail persisted without ID verification:** if
  the caller asks about test results, treatment plan details,
  or clinical history over the phone, the receptionist must
  verify identity (DOB + address on file) BEFORE reading it
  back. NEVER read chart contents to an unverified caller.
- **No reading records to third parties without authorization:**
  even family members. HIPAA authorization form on file, OR
  patient on the line consenting in-real-time, OR a legal
  guardian for a minor.
- **Voicemail / message rules:** clinical detail must NOT be
  left in voicemails on shared / family phones. "This is
  Smile Dental calling for [name], please call us back at
  [number]" is safe. "This is Smile Dental calling to
  confirm your extraction of tooth #14 tomorrow" is a
  violation.
- **Breach notification:** if PHI is inadvertently disclosed
  (wrong caller, wrong number, misrouted voicemail), reception
  MUST report to the practice's HIPAA Privacy Officer within
  the same business day. Practice then evaluates whether it
  triggers 60-day patient notification + HHS reporting.
  [VERIFY WITH COMPLIANCE OFFICER]
- **Business Associate Agreements (BAAs):** every vendor that
  touches PHI (Twilio, Deepgram, OpenAI, ElevenLabs, EHR /
  practice-management system, CRM sink) must have a signed
  BAA with the practice. This is a hard blocker on
  deployment — no BAA = HIPAA violation. Voice-AI-vendor BAA
  status must be documented per-integration.
- **De-identification for training / analytics:** any call
  recording used for model training or QA must be de-
  identified per HIPAA Safe Harbor (18 identifiers removed)
  or Expert Determination method.
- **Retention:** state-specific. Texas dental records: 5 years
  from last visit for adults, 5 years past age of majority for
  minors. Federal Medicare / Medicaid claims: 10 years.
  [VERIFY WITH STATE DENTAL BOARD]

### State dental board licensing

- **Per-state licensing:** each state's board sets pre-licensing
  hours, exam, continuing-education requirements. A Texas
  license does not transfer to California. Reception should
  never book cross-state without checking.
- **Sedation permits (separate from base license):** minimal
  sedation (nitrous), moderate sedation (oral), deep sedation +
  general anesthesia (IV). Different permit tiers, different
  training + facility requirements. Reception must know which
  sedation modalities the practice offers before offering to
  an anxious caller.
- **DEA registration (federal, prescribing controlled
  substances):** required for opioid pain relievers,
  benzodiazepines for oral sedation. Per-state DEA number,
  separate from dental license. Reception NEVER discusses
  scripts on the phone.
- **Corporate practice of medicine / dentistry (CPOM) laws:**
  some states restrict who can OWN a dental practice (must
  be a licensed dentist in states like California, Colorado,
  New Jersey, New York). Affects DSO tenant structure.
  [VERIFY WITH LOCAL COUNSEL]
- **Scope-of-practice for hygienists + assistants:** varies
  by state. Some states allow hygienists to do local
  anesthesia; others require dentist presence. Reception must
  not book a hygienist for a task outside their state scope.
  [VERIFY WITH STATE DENTAL BOARD]

### CDC infection-control + OSHA

- **CDC dental infection-control guidelines:** post-COVID
  heightened. Sterilization protocols, PPE, HVE (high-volume
  evacuation), pre-procedural rinses. Practices publish written
  protocols; reception may be asked, safe answer is "we follow
  CDC and OSHA guidelines; want me to email you our written
  policy?" Never invent specifics.
- **OSHA bloodborne pathogen standard:** employer-facing, not
  patient-facing, but affects staff scheduling (if a hygienist
  is out on needlestick protocol, reception must reschedule
  their patients).

### ADA (American Dental Association) guidelines

- **Ethical advertising:** no misleading before / after photos,
  no fee comparisons that misrepresent scope.
- **Informed consent:** for any procedure, patient must be
  informed of risks / benefits / alternatives. Reception
  doesn't obtain consent — provider does at chair — but
  reception must not oversell / undersell a procedure.
- **Treatment plan disclosure:** cost itemization before
  starting work. Reception's cash-quote or insurance-estimate
  is the FIRST touchpoint of this disclosure.
- **NPI (National Provider Identifier):** every provider has
  one. Required for insurance billing. Reception may need to
  provide when insurance carrier verifies.

### State AI-disclosure laws

- **Utah Consumer AI Protection Act (CACPA, 2024):** AI must
  disclose on direct question ("am I talking to a person?").
  Proactive disclosure not required unless the caller could
  reasonably think they're talking to a human — voice-AI
  qualifies. [VERIFY WITH COMPLIANCE OFFICER]
- **California BOT Disclosure Law (SB 1001):** requires
  disclosure of AI when using bots to communicate for
  commercial or political purposes.
- **California CCPA / CPRA:** caller's personal data (phone,
  name) is covered. Right to know + right to delete + right
  to opt-out of sale. Practice must publish a privacy notice.
  [VERIFY WITH COMPLIANCE OFFICER]
- **Colorado AI Act (SB 24-205, effective 2026):** high-risk
  AI system disclosure. Voice-AI in a healthcare context
  MAY qualify. [VERIFY WITH LOCAL COUNSEL]
- **Texas AI-disclosure requirements:** patchwork. HB 2060 +
  proposed frameworks. [VERIFY WITH LOCAL COUNSEL]
- **General rule:** AI disclosure on direct question is
  minimum. Some tenants may prefer proactive disclosure in
  the greeting for defensibility.

### Federal fair-billing (No Surprises Act 2022)

- **Good Faith Estimate (GFE):** required for uninsured or
  self-pay patients on any scheduled service. GFE must be
  provided within 3 business days for services scheduled
  10+ days out, within 1 business day for services scheduled
  3-9 days out. Reception's cash-quote is the FIRST leg of
  the GFE.
- **Dispute resolution:** if final bill exceeds GFE by $400+,
  patient can dispute through federal patient-provider
  dispute resolution process.
- **Publish standard charges:** hospitals + hospital-affiliated
  dental clinics must publish a machine-readable file of
  standard charges (not required for private practices).
- [VERIFY WITH COMPLIANCE OFFICER]

### State-level fair-billing + surprise-billing extensions

- **California AB 3087 + Colorado HB 21-1232 + New York + Texas +
  New Jersey + others:** state-level surprise-billing extensions
  layer additional cost-disclosure requirements. [VERIFY WITH
  LOCAL COUNSEL]

### Recording consent

- **Two-party consent states:** California, Florida, Illinois,
  Maryland, Massachusetts, Michigan, Montana, Nevada, New
  Hampshire, Pennsylvania, Washington. Notice at call open
  required. [VERIFY WITH LOCAL COUNSEL]
- **One-party consent states:** most others. Notice not
  strictly required but ethical + HIPAA-adjacent practice
  favors it.
- **Federal (18 USC § 2511):** allows one-party consent
  minimum. States can add.

### EU AI Act (rare for US clinic, non-zero for border towns +
medical-tourism)

- **Article 50 (in force 2 Aug 2026):** callers interacting
  with an AI system must be informed unless obvious. Voice-AI
  not obvious. Applies to any tenant whose service is offered
  in the EU market — a US border-town practice near Canada
  probably not affected; a US medical-tourism operator
  advertising in the EU probably is. [VERIFY WITH LOCAL
  COUNSEL]

### Universal safety rules (never violate)

- **MUST say:** AI disclosure when directly asked ("am I
  talking to a person?" → "I'm an automated receptionist for
  Smile Dental, but I can connect you to a real person if
  you'd like.")
- **MUST NOT say:** medication dosing, drug interaction
  advice, diagnostic claims ("that sounds like an infection"
  — no, receptionist doesn't diagnose), interpretation of
  X-ray or clinical findings, insurance benefits final
  determination (always "estimate — call your carrier to
  confirm").
- **MUST NOT persist:** clinical symptom details in CRM
  without patient consent (HIPAA minimum-necessary). Booking
  + phone + name + reason-for-visit at receptionist granularity
  OK; "toothache started 3 days ago severity 7 with pus" is
  protected + should live in chart, not booking notes.
- **MUST escalate:** severe pain 8+/10, active bleeding, facial
  swelling near eye/throat, trauma with LOC, breathing
  difficulty, chest pressure (route to 911, not on-call
  dentist), suicidal ideation, medication overdose questions,
  child-abuse disclosure (mandated reporting via provider).
- **MUST warm-handoff on:** offer / demand for controlled
  substance script, benefit-verification questions beyond
  what's on the FAQ, treatment-plan cost disputes, provider
  complaints.

## 7. Cross-sell / upsell opportunities

Legitimate value-adds a real receptionist raises. All disclosed as
optional; never pressure. Keyed to archetype + service.

### New patient / recall-side

- **New patient booked** → offer to email intake forms 24h
  ahead (saves 15 min at reception). Same-time reduces
  no-show rate.
- **New patient with insurance** → offer to run eligibility
  check before the appointment so cost estimate is accurate
  at chair.
- **New patient cash-pay** → mention practice discount plan
  or CareCredit financing upfront.
- **Recall booked** → offer X-rays add-on if 12+ months
  since last bitewing series.
- **Cleaning booked** → offer fluoride add-on if under
  insurance (usually kids' coverage; adult cash $25-45).
- **Recall booked** → mention "you're due for your Zoom
  whitening touch-up" if there's a previous whitening on
  chart (patient-approved cadence, not push).

### Emergency / pain-side

- **Emergency slot booked** → send SMS with pre-visit
  checklist: "arrive 15 minutes early, bring insurance
  card, plan for X-rays, don't eat 2 hours before if we
  might sedate."
- **Emergency patient without insurance on file** → offer
  cash-pay estimate PLUS practice payment plan for anything
  over $500.
- **Post-emergency follow-up** → book the follow-up recall
  at the emergency visit's SMS (30-day free window rules
  make it high-value).

### Follow-up / recheck-side

- **Follow-up booked** → suggest scheduling the next recall
  (6-month cleaning) at the same appointment so the patient
  doesn't have to call back.
- **Kid follow-up** → offer sibling booking same day if
  practice hygiene chair open (family logistics win).
- **Adult follow-up** → confirm insurance is still current
  (re-verify carrier + effective date).

### Cosmetic / ortho-side

- **Whitening asked (in-office)** → mention take-home tray
  option if cost is a barrier ($250-500 vs $549 in-office).
- **Whitening asked (cost-sensitive)** → mention OTC option
  redirect to dentist for guidance.
- **Invisalign consultation booked** → mention CareCredit
  financing upfront so caller doesn't ghost after seeing
  the $4895 price tag.
- **Invisalign started** → book 6-8 week progress checks in
  a series.

### Anxious / phobic-side

- **Anxious patient** → mention minimal-sedation availability
  (nitrous / oral) IF the practice offers it (fixture must
  expose).
- **Anxious patient** → offer consultation-only first visit,
  no work, meet-the-doctor.
- **Anxious patient with kid** → offer sibling or spouse
  attendance if the practice permits.

### Language / accessibility-side

- **Spanish caller** → schedule with bilingual hygienist
  (Rosa in fixture) proactively — don't wait for the patient
  to ask.
- **Elderly caller** → offer transportation info / accessible
  entrance / paperwork help.
- **Interpreter-on-line caller** → schedule with slower-cadence
  provider if practice has one; note interpretation need on
  chart.

### Insurance / cost-side

- **Insurance-only caller** → offer to run eligibility check
  now (30 seconds via clearinghouse), turns an info-only
  call into a warm booking prospect.
- **Cash-pay caller** → mention practice membership plan
  ($300-500/year, cheaper than most cleaning + exam bundles).
- **Multi-service quote request** → send full fee schedule PDF
  via SMS or email + offer treatment-plan consultation.

### Referral / specialty-side

- **Referral-in caller** → confirm specialist scope + gather
  referring-doctor letter / imaging BEFORE the appointment.
- **Referral-in caller** → ask if they want us to fax records
  back to the referring provider post-visit (courtesy that
  strengthens the referral relationship).
- **Post-specialist referral-out** → schedule the follow-back
  visit to close the loop.

### Universal

- **Any booking** → offer SMS reminder 24 hours + 2 hours
  before (reduces no-show).
- **Any booking** → offer online intake forms link.
- **Any patient with a birthday in the current month** →
  small gesture (birthday card, coffee gift if the practice
  does that — check fixture policy).

## 8. Sources

### Direct sources (used to populate this playbook)

- **Smile Dental Clinic fixture:**
  `sample-data/clinic/business.json` — canonical tenant
  shape, services, FAQs, persona (Alex — Texan front-desk
  lead in Plano), hours, escalation.
- **Legacy fixture (for cross-shape reference, non-authoritative):**
  `sample-data/clinic/business.riverside-old.json` — 5-service
  medical GP shape, kept for cross-vertical comparison.
- **Christiaan follow-up call (canonical failure case):**
  CallSid `CA2fa1fef2065a7df388c3d6f58d7a7792` in
  `data/call_events.db` — verbatim Dutch mobile capture +
  "A follow-up" trigger + false-complete cascade.
- **Roxana call:** CallSid `CA3dac68...` — bilingual /
  Spanish-preference archetype.
- **Abbas call:** CallSid `CAa8d209...` — new patient
  booking flow.
- **All transcripts index:** `docs/transcripts/README.md`.
- **Journey audit (Christiaan deep dive):**
  `docs/product/journey-audit-follow-up-clinic-2026-08-29.md`.
- **Golden scripts corpus (24 scripts, H/F/E — happy /
  failure recovery / edge cases):**
  `docs/product/golden-scripts-clinic-2026-08-30.md`.
- **Persona ladder (10 archetypes, canonical intent-label
  enum, coverage gaps):**
  `docs/product/persona-ladder-clinic-2026-08-30.md`.
- **Service taxonomy (full catalog reconciled with fixture +
  container-shaped services + required-info matrix):**
  `docs/product/service-taxonomy-clinic-2026-08-30.md`.
- **Service alias map:**
  `packages/integrations/service_aliases.py` — ~30 caller
  phrases → service keywords.
- **Tool contract:**
  `packages/integrations/clinic_tools.py` —
  `check_availability`, `book_appointment`,
  `find_existing_appointment`, `cancel_appointment`,
  `reschedule_appointment`.

### Gaps to close (still needs a real practice interview)

Each is tagged with the staff role most likely to answer.

- **TODO: real Smile Dental / private-practice office manager
  interview.** Insurance-mix specifics, pre-auth patterns,
  cash-quote vs GFE process, no-show policy execution.
  Ownership: **office manager**.
- **TODO: real dentist / clinical director interview.** Service
  duration ground truth (how much of a "60 min crown" is
  actual chair time vs setup), specialist-referral criteria
  ("when do YOU refer out for a root canal"), sedation-
  offering scope (nitrous only vs oral vs IV). Ownership:
  **owner-dentist**.
- **TODO: real dental hygienist interview.** Prophy vs SRP
  vs perio maintenance criteria, X-ray cadence enforcement,
  fluoride-add-on frequency. Ownership: **lead hygienist**.
- **TODO: real compliance officer / HIPAA privacy officer
  interview.** All statutory content in §6 marked [VERIFY
  WITH COMPLIANCE OFFICER] needs validation. Voicemail-
  policy language especially. Ownership: **HIPAA Privacy
  Officer** (may be office manager in a solo practice, may
  be a compliance vendor for DSOs).
- **TODO: real state dental board reference for TX + CA + FL
  + NY + IL** — the states we're most likely to deploy
  first. Sedation permit tiers + hygienist scope + record
  retention. Ownership: **compliance officer or dental
  board reference published data**.
- **TODO: real DSO reception interview.** Centralized call-
  center flow, script constraints, upsell metric pressure.
  Very different tenant shape than Smile Dental. Ownership:
  **DSO regional operations manager**.
- **TODO: real pediatric-only practice interview.** Behavior
  guidance protocols, parent-in-room policy, minor-consent
  edge cases. Ownership: **pediatric dentist + pediatric
  practice office manager**.
- **TODO: real oral surgeon interview.** Wisdom-teeth
  pre-op checklist, sedation planning cadence, panoramic
  imaging requirement. Ownership: **oral surgeon**.
- **TODO: real endodontist interview.** Root canal
  retreatment shape, CBCT imaging requirement, referral-in
  flow with GPs. Ownership: **endodontist**.
- **TODO: real orthodontist interview.** Invisalign vs
  braces decision drivers, consultation vs treatment start,
  retainer replacement flow. Ownership: **orthodontist**.
- **TODO: real periodontist interview.** SRP quadrant
  planning, gum-graft consent process, implant-placement
  scope. Ownership: **periodontist**.
- **TODO: real medical / GP practice interview (vet + medical
  playbooks are downstream deliverables).** The playbook
  should generalize but needs domain interviews to be
  reference-quality outside dental. Ownership: **PCP + vet
  clinic manager**.
- **TODO: industry benchmark on missed-emergency conversion
  cost.** How much revenue does a practice lose per emergency
  routed to the ER instead? Rough number would sharpen the
  priority of the emergency-triage failure mode. Ownership:
  **published industry data (ADA + trade press)**.
- **TODO: HIPAA Privacy Officer sign-off on voicemail script
  language.** Current fixture doesn't specify. Ownership:
  **compliance officer**.

### External references (not yet fetched)

- HIPAA Privacy Rule (45 CFR Parts 160 + 164)
- HIPAA Security Rule (45 CFR Part 164 Subpart C)
- HITECH Act 2009
- CDC Dental Infection Prevention & Control Guidelines
- OSHA Bloodborne Pathogen Standard (29 CFR 1910.1030)
- ADA Code of Ethics + Principles of Ethics + Code of
  Professional Conduct
- No Surprises Act 2022 (Consolidated Appropriations Act
  2021)
- Utah CACPA (Utah Code § 13-72)
- California BOT Disclosure Law (SB 1001)
- California CCPA / CPRA (Cal. Civ. Code § 1798.100 et seq.)
- Colorado AI Act (SB 24-205)
- Texas HIPAA Privacy Act extensions
- State dental board sites (per-state; TX, CA, FL, NY, IL as
  priority)
- CMS Medicare / Medicaid dental coverage rules
- DEA registration requirements (21 CFR 1301)

## Product gaps flagged for engineering

Concrete deficiencies in the current codebase (as of 2026-08-30)
revealed by walking the playbook against real Smile Dental use.
Pass to engineering as spec candidates. Cross-reference existing
`docs/product/*.md` gap sections.

1. **Chart-lookup gate on `FOLLOWUP_RECHECK` intent (BEFORE
   `ASK_SLOT(phone)`).** The single most important
   architectural gap the playbook confirms. DISCOVER_CONTEXT
   dialogue-policy branch must fire when intent = FOLLOWUP_
   RECHECK, must look up chart by phone, must name the original
   procedure + provider back to the caller before booking. If
   chart not found under the calling number, must ask "what
   number would we have you under" — never silently fall back
   to new-patient flow. Journey audit spec:
   `docs/product/journey-audit-follow-up-clinic-2026-08-29.md`.

2. **`languages_supported[]` field on tenant fixture.** Currently
   only per-language FAQs (`faqs.spanish`). Reception can't
   honestly enumerate scope for a non-supported-language caller.
   Add tenant-level list; honest three-option offer keyed off
   presence / absence.

3. **`patient_notes[]` (phonetic + preference persistence) keyed
   by phone.** Corrections stick only to one booking's notes
   field; next call gets it wrong. Add tenant-level patient
   notes structure keyed by phone number. Include: phonetic
   name, provider-gender preference, bilingual preference,
   sedation preference, callback-time preference, no-show
   history flag.

4. **Provider-day availability field on providers.** Fixture
   has practice-level `hours` but not per-provider day-of-week
   availability. Reception should be able to answer "does
   Dr. Ramanathan work Thursdays?" from the fixture, not the
   scheduling system. Add `providers[].schedule` per-day map.

5. **`caller_intent` field on booking records.** Once the
   Phase 4 intent extractor exists (persona-ladder deliverable
   #1), its output should be persisted on the booking so we
   can measure per-archetype outcome rates. Without this we
   can't close the loop on the auto-judges.

6. **Container-shaped-service sub-typing enforcement.** Fixture
   `Follow-up visit` has `duration_by_original_procedure` map
   (shipped 2026-08-30 in commit d099354). Same pattern needed
   for Consultation, Cleaning, Extraction, Denture, X-rays.
   Service taxonomy §Container-shaped-services is the spec.

7. **Pre-authorization requirement flag on services.** Crown +
   implant + ortho typically require insurance pre-auth. Fixture
   doesn't expose per-service pre-auth requirement. Add
   `services[].requires_preauth: bool` and surface via
   reception prompt.

8. **Provider specialty routing field.** Fixture doesn't
   attribute specialty to providers (Dr. Whitfield is
   "pediatric specialist" only in the `faqs.kids` string,
   Dr. Ramanathan is Invisalign in the service description).
   Add `providers[].specialties[]` for routing. Wisdom-teeth
   should route to oral-surgeon-specialty, not any dentist.

9. **Pediatric age-cap field per provider.** Some pediatric
   providers cap at 12, some at 18. Fixture doesn't expose.
   Add `providers[].pediatric_age_max`.

10. **Sedation-offerings field per practice.** Anxious-caller
    handling depends on knowing what's available (nitrous / oral
    / IV). Fixture doesn't expose. Add `sedation_offerings[]`.

11. **`bilingual_providers[]` field.** Fixture has Spanish FAQ
    pointing to Rosa; needs structured mapping so agent can
    proactively route bilingual caller to the right provider
    without prompting the caller to ask.

12. **Insurance-plan structured list (not just FAQ string).**
    Fixture has `faqs.insurance` free-text. Reception needs to
    check a specific carrier against a structured list to
    answer "do you take X?" reliably. Add
    `accepted_insurance[]` with plan-name + in-network status.

13. **After-hours emergency routing configuration.** Fixture
    mentions Dr. Chen is on-call in FAQ, but no structured
    on-call routing rules. Emergency call after hours should
    know to transfer to on-call line vs voicemail vs 911
    depending on severity keywords.

14. **HIPAA-consent flag for chart-holder-relative callers.**
    Currently no scaffolding for "I'm calling for my father"
    caller who needs authorization to book on behalf. Add
    consent flag + reception prompt to ask.

15. **BAA-status audit for every voice-AI vendor.** Twilio,
    Deepgram, OpenAI, ElevenLabs, CRM sink — each needs a
    documented BAA before production deploy. Not a fixture
    issue but a compliance gate.

16. **Meta-adversarial intent as a labeled class.** Currently
    treated as unknown or booking-attempt gone weird. Needs
    own branch in policy tree for deterministic one-line
    in-scope refusal. Persona ladder gap #8.

17. **Shame-tolerance heuristic for anxious archetype.** No
    mechanism in current prompt for "second-turn tone-softening"
    behavior. Prompt-eng task, not classifier task. Persona
    ladder gap #7.

18. **Bad-outcome catalog doc doesn't exist yet.** Referenced
    by charter as deliverable #4. Should be next product-lead
    deliverable after this playbook enrichment.
