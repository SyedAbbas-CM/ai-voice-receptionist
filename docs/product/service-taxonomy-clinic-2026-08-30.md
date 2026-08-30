# Service taxonomy — clinic (dental)

Author: product-lead (subagent)
Vertical: clinic
Date: 2026-08-30

## Purpose + consumers

Two consumers drive the shape of this document:

1. **Phase 4 golden-corpus regression sweep** — asserts that per service, the agent collects the right required-info slots BEFORE `book_appointment` fires, picks the right duration, routes to the right provider, and surfaces the right cross-sell hook. The "Required-info slot matrix" and "Taxonomy tree" sections below are the reference.
2. **Follow-up-audit gap 5 propagation** — the `duration_by_original_procedure` map shipped for `Follow-up visit` (commit `d099354`) is the pattern for container-shaped services. This doc identifies which OTHER services are container-shaped and need similar sub-typing (see "Container-shaped services" below).

## Method

Extracted the full catalog from:
- Playbook §3 (`.claude/plugins/product-lead/product_playbooks/clinic.md`) — canonical, 6 categories, 28 services.
- Fixture: `sample-data/clinic/business.json` — 10 services (Smile Dental Clinic, Plano TX).
- Old fixture: `sample-data/clinic/business.riverside-old.json` — 5 services (medical GP, kept for cross-vertical shape comparison, not authoritative).
- Alias map: `packages/integrations/service_aliases.py` (~30 caller phrases → keyword tuples).
- Tool contract: `packages/integrations/clinic_tools.py` (`check_availability`, `book_appointment`, `find_existing_appointment`, `cancel_appointment`, `reschedule_appointment`).
- Related audits: `docs/product/journey-audit-follow-up-clinic-2026-08-29.md`, `docs/product/golden-scripts-clinic-2026-08-30.md`.

Playbook is canonical. Where fixture disagrees, the reconciliation table below flags the gap and recommends whether to add to fixture, add an alias, or leave as tenant-specific.

## Taxonomy tree

Legend for required-info slots:
- **phone / name / date / time** — universal booking slots.
- **service_subtype** — WHICH variant of the service (adult vs pediatric, in-office vs take-home, etc). Required when the parent name maps to two+ tenant services.
- **original_procedure** — for container-shaped services (Follow-up, Second visit). Feeds `duration_by_original_procedure` map.
- **original_provider** — continuity-of-care attribution. Required for any recheck, integration check, or second visit of a two-visit treatment.
- **original_visit_date** — required whenever pricing depends on a time-window rule (30-day free follow-up window).
- **provider** — required when caller names a specific provider OR when the service class demands specialist (endo, ortho, oral surgery).
- **insurance** — REQ for any non-cash service where price is insurance-dependent; OPT for cash-only cosmetic.
- **age** — REQ for anything that splits adult/pediatric; determines provider (Whitfield does peds) + duration + slot search.
- **pain_context** — REQ for emergency triage; determines whether to escalate vs schedule.
- **referral** — REQ when caller mentions "my doctor sent me" or service class is specialist-only.

---

### 1. Diagnostic

- **New patient exam with X-rays** *(fixture: present, 60min, $189)*
  - Duration: 60 min
  - Price: $189 (fixture, cash quote); OPT insurance verify
  - Provider(s): Dr. Chen or Dr. Whitfield (fixture says either)
  - Required-info-before-booking: `phone`, `name`, `date`, `time`, `insurance` (OPT but strongly recommended given cost sensitivity for new patients)
  - Cross-sell hooks: email intake forms before visit (playbook §7 #1), "arrive 15 min early, bring photo ID + insurance card"
  - Ambiguity notes: matches "new patient", "first visit", "become a patient", "haven't been in before". Does NOT match "check-up" alone (that could be recall). Does NOT match "just a look" — probe pain-context first (may be emergency).

- **Adult recall exam** *(fixture: present as "Adult recall exam", 30min, $95)*
  - Duration: 30 min
  - Price: $95 (fixture)
  - Provider(s): any adult dentist; often paired with hygienist visit same day
  - Required-info-before-booking: `phone`, `name`, `date`, `time`; chart lookup via `find_existing_appointment(phone=..., upcoming_only=False)` to attribute prior provider preference
  - Cross-sell hooks: "you're due for X-rays" if last bitewing >12 months (playbook §7 #2), pair with cleaning same day
  - Ambiguity notes: matches "check-up" (returning), "six month", "recall", "regular exam". Ambiguous with New patient exam — disambiguate by asking "have you been in before?" — though for the follow-up class, the phrase itself is the returning-patient marker (see playbook §4 note).

- **Pediatric first visit** *(fixture: present, 45min, no price listed — GAP)*
  - Duration: 45 min
  - Price: FIXTURE MISSING structured price field
  - Provider(s): Dr. Whitfield (fixture attributes "pediatric specialist" to Whitfield)
  - Required-info-before-booking: `phone`, `name` (parent AND child), `age` (REQ — determines whether pediatric or adult), `date`, `time`, `insurance` (usually parent's plan)
  - Cross-sell hooks: fluoride add-on if covered, sealants at future visit if age-appropriate (playbook §7)
  - Ambiguity notes: matches "my kid", "my child", "my son/daughter", "for my kids", "first visit for". "My kid needs something" → also probe pain-context (kid emergency common).

- **Emergency exam** *(fixture: present, 30min, $115)*
  - Duration: 30 min
  - Price: $115 (fixture)
  - Provider(s): any dentist on floor; Dr. Chen is on-call after hours per fixture FAQ
  - Required-info-before-booking: `pain_context` FIRST (when started, severity 1-10, swelling?, bleeding?), THEN `phone`, `name`, `date`, `time` — but "date/time" is "next available same-day" not caller-selected in most cases
  - Cross-sell hooks: SMS with "arrive 15 min early, bring insurance card, plan for X-rays" (playbook §7 #4)
  - Ambiguity notes: matches "emergency", "urgent", "toothache", "pain", "swollen", "broken tooth", "lost filling", "kid fell". MUST-ESCALATE triggers per playbook §6: severity 8+/10, active bleeding, facial swelling near eye/throat, trauma with LOC.

- **Consultation (Invisalign / Implant / Cosmetic)** *(fixture: present as two separate rows — Invisalign consultation 45min, Implant consultation 60min)*
  - **CONTAINER-SHAPED** — see recommendations. Duration + provider vary by consult type.
  - Sub-types + durations:
    - Invisalign consultation — 45 min, Dr. Ramanathan (fixture), FREE consult
    - Implant consultation — 60 min, provider unattributed in fixture (playbook: often specialist / oral surgeon), $0 quote in fixture
    - Cosmetic consultation — playbook says 45-60min, NOT in fixture — GAP
    - Second-opinion consultation — playbook says 45-60min, NOT in fixture — GAP
  - Required-info-before-booking: `service_subtype` (REQ — determines provider + duration + downstream pricing message), then `phone`, `name`, `date`, `time`
  - Cross-sell hooks: mention financing option upfront for Invisalign to prevent price-ghost (playbook §7 #5); for implant, mention cost bracket up front ($4250 per fixture) same reason
  - Ambiguity notes: "consultation" alone is AMBIGUOUS — MUST ask "consultation for what — Invisalign, implants, cosmetic work?" (playbook §4). "Consult about my teeth being crooked" → Invisalign; "consult about a missing tooth" → Implant; "consult about veneers/whitening" → Cosmetic. Do NOT resolve to any single consultation row without sub-type; the alias map currently maps `consultation → ("consultation",)` which will hit tenant fuzzy-match against whichever consultation row alphabetizes first — engineering gap.

### 2. Preventive

- **Adult cleaning (prophy)** *(fixture: present as "Adult cleaning", 45min, $135)*
  - Duration: 45 min
  - Price: $135 (fixture)
  - Provider(s): Rosa Delgado (fixture lead hygienist, bilingual EN/ES) or other hygienist
  - Required-info-before-booking: `phone`, `name`, `date`, `time`; if returning: check chart for last hygienist preference
  - Cross-sell hooks: fluoride add-on if covered (playbook §7 #3), pair with recall exam same-visit, mention Rosa if caller has spanish preference
  - Ambiguity notes: matches "a cleaning", "prophy", "hygiene", "teeth cleaning". AMBIGUOUS with pediatric cleaning + deep cleaning — MUST ask "adult or for a child?" if age unknown; ask "regular cleaning or is this for gum issues?" if any pain / bleeding / gum context.

- **Pediatric cleaning** *(playbook: 30min. FIXTURE MISSING — GAP)*
  - Duration: 30 min
  - Price: fixture-not-set
  - Provider(s): Dr. Whitfield or pediatric-comfortable hygienist
  - Required-info-before-booking: `phone`, `name` (parent + child), `age`, `date`, `time`
  - Cross-sell hooks: sealants at future visit, fluoride add-on
  - Ambiguity notes: same triggers as adult cleaning but with `kid` / `child` context. Currently `service_aliases.py` routes "kids" → keyword `("pediatric",)` — no pediatric cleaning service in fixture, so falls through to fuzzy match that will likely hit `Pediatric first visit` (45min, wrong).

- **Deep cleaning (SRP scaling and root planing)** *(playbook: 60-90min gum disease. FIXTURE MISSING — GAP)*
  - Duration: 60-90 min
  - Price: fixture-not-set; insurance-dependent, may need pre-auth
  - Provider(s): hygienist (Rosa) or periodontist referral
  - Required-info-before-booking: `phone`, `name`, `date`, `time`, `insurance` (REQ — periodontal work often has pre-auth requirement), OPT `referral` if periodontist was requested
  - Cross-sell hooks: quadrant-by-quadrant multi-visit planning if extensive
  - Ambiguity notes: matches "deep cleaning", "SRP", "scaling", "my gums are bleeding". Ambiguous with regular cleaning — if caller mentions bleeding gums or "the last hygienist said I needed a deep clean", route here not to prophy.

- **Fluoride treatment** *(playbook: 15min add-on. FIXTURE MISSING — GAP)*
  - Duration: 15 min add-on (rarely booked standalone)
  - Price: usually bundled with cleaning
  - Provider(s): hygienist
  - Required-info-before-booking: usually N/A standalone; if standalone, `phone`, `name`, `date`, `time`
  - Cross-sell hooks: primarily an ADD-ON at cleaning-booking time, not a first-line service.

- **Sealants** *(playbook: 30min, usually pediatric. FIXTURE MISSING — GAP)*
  - Duration: 30 min
  - Price: fixture-not-set
  - Provider(s): Dr. Whitfield or hygienist for pediatric
  - Required-info-before-booking: `phone`, `name` (parent + child), `age`, `date`, `time`

### 3. Restorative

- **Composite filling** *(fixture: present, 45min, starts $245)*
  - Duration: 45 min (playbook confirms; multi-surface may exceed)
  - Price: from $245, varies by surfaces (fixture description)
  - Provider(s): any dentist
  - Required-info-before-booking: `phone`, `name`, `date`, `time`; ideally `insurance` — most PPO covers portion
  - Cross-sell hooks: none direct; mention "usually one visit but may need follow-up if deep" to set expectation
  - Ambiguity notes: matches "filling", "cavity", "cavities". Do NOT match "chipped tooth" without probe (could be composite, could need crown, could need extraction).

- **Crown** *(playbook: 90-120min, may be 2 visits. FIXTURE MISSING — GAP)*
  - Duration: 90-120 min per visit; typically 2 visits (prep + placement)
  - Price: fixture-not-set; typically $1200-$1800 per tooth
  - Provider(s): any general dentist
  - Required-info-before-booking: `phone`, `name`, `date`, `time`, `insurance`, `visit_number` (prep vs placement — if placement, MUST link to original prep appointment for continuity, similar to Follow-up shape)
  - Cross-sell hooks: mention CareCredit if treatment plan >$1000 (playbook §7 + fixture FAQ)

- **Bridge** *(playbook: 2-3 visits, 90min each. FIXTURE MISSING — GAP)*
  - Duration: 90 min × 2-3 visits
  - Price: fixture-not-set
  - Provider(s): any general dentist
  - Required-info-before-booking: as crown

- **Denture (partial / full)** *(playbook: multi-visit, weeks. FIXTURE MISSING — GAP)*
  - Duration: variable, multi-visit over weeks
  - Provider(s): any general dentist; possibly prosthodontist
  - Required-info-before-booking: `phone`, `name`, `date`, `time`, `service_subtype` (partial vs full — different duration + price + visit count)

- **Root canal (endo)** *(playbook: 60-90min, sometimes referred. FIXTURE MISSING — GAP)*
  - Duration: 60-90 min
  - Price: fixture-not-set; typically $700-$1500
  - Provider(s): general dentist for straightforward; endodontist referral for molar / retreatment
  - Required-info-before-booking: `phone`, `name`, `date`, `time`, `tooth_location` (front vs molar determines specialist referral), `insurance`, OPT `referral`
  - Cross-sell hooks: crown recommendation typically follows root canal — mention as future visit

### 4. Cosmetic

- **Zoom whitening (in-office)** *(fixture: present, 90min, $549)*
  - Duration: 90 min
  - Price: $549 (fixture)
  - Provider(s): any dentist or trained hygienist
  - Required-info-before-booking: `phone`, `name`, `date`, `time`; usually cash / not insurance
  - Cross-sell hooks: mention take-home tray option if in-office out of budget (playbook §7 #6)
  - Ambiguity notes: matches "whitening", "zoom", "teeth whitening". Ambiguous with take-home trays — MUST ask "in-office all at once, or take-home trays over a couple weeks?" if caller says just "whitening".

- **Take-home whitening trays** *(playbook: 30min fitting. FIXTURE MISSING — GAP)*
  - Duration: 30 min (fitting only; treatment happens at home)
  - Price: fixture-not-set; typically $300-$500
  - Provider(s): hygienist or dentist
  - Required-info-before-booking: `phone`, `name`, `date`, `time`

- **Veneers** *(playbook: consultation + 2 visits, weeks. FIXTURE MISSING — GAP)*
  - Duration: variable, multi-visit
  - Price: fixture-not-set; typically $1000-$2500 per tooth
  - Provider(s): dentist with cosmetic focus
  - Required-info-before-booking: booking-of-visit-1 is CONSULTATION, not treatment — route to Cosmetic consultation branch first
  - Cross-sell hooks: financing / payment plan mention up front

### 5. Ortho

- **Invisalign consultation** *(fixture: present, 45min, FREE per fixture description)*
  - Duration: 45 min
  - Price: FREE consult; full treatment "around $4895" (fixture)
  - Provider(s): Dr. Ramanathan (fixture)
  - Required-info-before-booking: `phone`, `name`, `date`, `time`
  - Cross-sell hooks: mention financing upfront so caller doesn't ghost after seeing price (playbook §7 #5)
  - Ambiguity notes: matches "Invisalign", "braces" (currently aliased to invisalign in `service_aliases.py:107` — accurate since fixture only has clear-aligner ortho, but wrong if practice adds traditional braces).

- **Invisalign treatment start** *(playbook: 90min, includes scan + first trays. FIXTURE MISSING — GAP)*
  - Duration: 90 min
  - Provider(s): Dr. Ramanathan
  - Required-info-before-booking: `phone`, `name`, `date`, `time`, MUST link to consultation appointment (continuity of care)
  - This is a container-shaped follow-up of the consult — same pattern as second visit of two-visit treatment.

- **Traditional braces consultation** *(playbook: same 45min as Invisalign. FIXTURE MISSING — GAP)*
  - Only relevant if practice offers non-clear ortho; Plano fixture appears clear-only.

### 6. Surgical

- **Simple extraction** *(playbook: 30-45min. FIXTURE MISSING — GAP)*
  - Duration: 30-45 min
  - Provider(s): any general dentist
  - Required-info-before-booking: `phone`, `name`, `date`, `time`, `tooth_location` (front/back — routes to specialist decision), `pain_context` if emergency-adjacent
  - Ambiguity notes: matches "extraction", "pull my tooth", "remove a tooth". AMBIGUOUS with surgical / wisdom — MUST ask "which tooth — a wisdom tooth, or one of the ones in front?" (playbook §4).

- **Surgical extraction** *(playbook: 60min, may need referral to oral surgeon. FIXTURE MISSING — GAP)*
  - Duration: 60 min
  - Provider(s): oral surgeon referral common
  - Required-info-before-booking: same as simple extraction + `referral` if specialist-only tenant.

- **Wisdom teeth (all four)** *(playbook: 90-120min, oral surgeon. FIXTURE MISSING — GAP)*
  - Duration: 90-120 min
  - Provider(s): oral surgeon (may be off-site referral)
  - Required-info-before-booking: `phone`, `name`, `date`, `time`, `insurance` (REQ — anesthesia coverage often separate), OPT `medical_history_flag` (blood thinners, allergies — playbook §5)

- **Implant placement** *(playbook: 90min, often specialist. FIXTURE MISSING — GAP; fixture only has Implant consultation)*
  - Duration: 90 min
  - Provider(s): specialist typically
  - Required-info-before-booking: `phone`, `name`, `date`, `time`, MUST link to prior Implant consultation appointment.

### 7. Follow-up / recheck  *(CONTAINER-SHAPED CATEGORY — SEE RECOMMENDATIONS)*

- **Follow-up visit** *(fixture: present, 30min default, $75 or FREE within 30 days, `duration_by_original_procedure` map present)*
  - Duration: **DEPENDS ON `original_procedure`** — 15min (extraction / antibiotic / filling), 30min (implant / denture / veneer / default), 45min (root canal / bridge), 60min (crown). Map lives in fixture; brain augmenter (`packages/core_agent/brain.py`, commit `d099354`) injects into tool prompt.
  - Price: FREE if within 30 days of original visit, else $75 (fixture description)
  - Provider(s): **MUST be the same provider who did the original work** (continuity of care per playbook §2 / §5)
  - Required-info-before-booking: `original_procedure` (REQ — feeds duration map), `original_provider` (REQ — continuity), `original_visit_date` (REQ — feeds 30-day free-window rule), then chart lookup via `find_existing_appointment`, then `phone` (from chart or fallback ask), then `date`, `time`
  - Cross-sell hooks: none direct; may mention "if you want to do your cleaning while you're here" if recall due
  - Ambiguity notes: matches "follow up", "follow-up", "recheck", "return visit", "second visit". This is the canonical container. See `docs/product/journey-audit-follow-up-clinic-2026-08-29.md`. Not a service to be pattern-matched to duration=30 blindly — MUST trigger DISCOVER_CONTEXT branch (playbook §4 first bullet).

- **Post-antibiotic recheck** *(playbook: 15-30min. NOT in fixture as separate row — folded into Follow-up map with key "antibiotic": 15)*
  - Treated as a Follow-up sub-type via the map. OK.

- **Implant integration check** *(playbook: 30min, 3-4 months after placement. Treated as Follow-up sub-type with key "implant": 30)*
  - MUST link to original implant placement appointment.

- **Second visit of two-visit treatment (crown-seat, denture-adjust, veneer-place, bridge-seat)** *(playbook: variable duration. Folded into Follow-up map by original_procedure keyword)*
  - REQUIRED-INFO expansion over Follow-up: this is not just "recheck" — it's the completion of a planned two-visit sequence. If prior visit was crown-prep, this visit is crown-seat and needs the lab-fabricated crown to be back. Ideal receptionist confirms "your crown came back from the lab yesterday, so we're good" — beyond current fixture scope, but worth flagging.

---

## Container-shaped services (audit-driven addition)

A "container" service is one where the parent name resolves cleanly by alias but the actual booking parameters (duration, provider constraint, price rule) depend on a sub-type the caller has to be asked for. Follow-up visit is the archetype and was shipped with `duration_by_original_procedure`. The following services have the same shape and need equivalent treatment.

| Service | Sub-type slot needed | Why it's container-shaped | Suggested field name |
|---|---|---|---|
| **Follow-up visit** | `original_procedure` | Duration + free-window rule vary by what was done originally | `duration_by_original_procedure` (SHIPPED) |
| **Consultation (Invisalign / Implant / Cosmetic / Second opinion)** | `consultation_topic` | Duration varies 45-60min; provider varies (Ramanathan vs implant specialist vs cosmetic dentist); free-vs-paid varies | `duration_by_consultation_topic` + `provider_by_consultation_topic` |
| **Cleaning** | `cleaning_type` | Adult prophy (45min) vs pediatric (30min) vs deep/SRP (60-90min) diverge on duration + provider (hygienist vs periodontist) + insurance pre-auth | `duration_by_cleaning_type` |
| **Extraction** | `extraction_type` | Simple (30-45min, generalist) vs surgical (60min, may need referral) vs wisdom (90-120min, oral surgeon) diverge on duration + provider + specialist referral | `duration_by_extraction_type` + `provider_by_extraction_type` |
| **Second-visit-of-two-visit-treatment** (crown-seat, veneer-placement, bridge-seat, denture-adjust) | `original_procedure` | Similar to Follow-up but with lab-fabrication dependency ("is the crown back from the lab?") | reuse Follow-up map + `lab_ready_check` flag |
| **Denture** | `denture_type` | Partial vs full = different visit sequence + duration | `visits_by_denture_type` |

**Recommendation (product):** Consultation is the highest-value next candidate for `duration_by_consultation_topic` because "consultation" is a common bare-word caller phrase and the current alias map (`packages/integrations/service_aliases.py:110`) hits any tenant service containing "consultation" — which for the Smile Dental fixture means fuzzy-match between Invisalign consult and Implant consult. Wrong duration is a false-complete.

---

## Fixture reconciliation

Playbook is canonical (28 services across 6 categories + follow-up). Fixture is 10 services. Everything below is a gap.

| Playbook says | Fixture has | Gap type | Recommendation |
|---|---|---|---|
| New patient exam with X-rays (60min) | Present, matches | ✓ OK | — |
| Adult recall exam (30min) | Present as `Adult recall exam` (30min, $95) | ✓ OK | — |
| Pediatric first visit (45min) | Present, matches — but no price | Missing price | Add structured `price` / `price_display` field (audit Gap 6 per invocation prompt) |
| Emergency exam (30min) | Present, matches | ✓ OK | — |
| Consultation (Invisalign / Implant / Cosmetic) | Split into `Invisalign consultation` (45min) + `Implant consultation` (60min); NO cosmetic consult, NO second-opinion consult | Partial — cosmetic + second-opinion missing | Add both to fixture; add alias-map disambiguation for bare "consultation" |
| Adult cleaning (prophy) 45min | Present as `Adult cleaning` (45min, $135) | ✓ OK | — |
| Pediatric cleaning 30min | MISSING | Gap | Add to fixture; without it "kids cleaning" fuzzy-matches Pediatric first visit (45min, wrong) |
| Deep cleaning (SRP) 60-90min | MISSING | Gap | Add to fixture; caller phrase "bleeding gums" / "deep cleaning" currently falls through |
| Fluoride treatment (15min add-on) | MISSING | Gap (low priority — usually bundled) | Add as `is_add_on: true` OR document as bundled |
| Sealants (30min pediatric) | MISSING | Gap (low priority — future add-on) | Add |
| Composite filling (45min) | Present, matches | ✓ OK | — |
| Crown (90-120min, 2 visits) | MISSING | Gap | Add both `Crown prep` + `Crown seat` OR add `Crown` with visit_number sub-type |
| Bridge | MISSING | Gap | Add |
| Denture | MISSING | Gap | Add with partial/full sub-type |
| Root canal (60-90min) | MISSING | Gap | Add; also add referral trigger for molars |
| Zoom whitening (90min) | Present, matches | ✓ OK | — |
| Take-home whitening trays (30min fitting) | MISSING | Gap | Add — playbook §7 cross-sell requires this row exist |
| Veneers | MISSING | Gap | Add or route through cosmetic consultation |
| Invisalign consultation (45min) | Present, matches | ✓ OK | — |
| Invisalign treatment start (90min) | MISSING | Gap | Add — currently the treatment-start booking has no service to resolve to |
| Traditional braces consult | MISSING | Gap (tenant-specific — Plano fixture may be clear-only) | Optional |
| Simple extraction | MISSING | Gap | Add |
| Surgical extraction | MISSING | Gap | Add with referral flag |
| Wisdom teeth | MISSING | Gap | Add or link to oral-surgeon referral |
| Implant placement (90min) | MISSING | Gap | Add — fixture has consult but not placement |
| Follow-up visit | Present, has `duration_by_original_procedure` map | ✓ OK — SHIPPED as of commit d099354 | Extend to Consultation next |
| Post-antibiotic recheck (15-30min) | Folded into Follow-up map | ✓ OK | — |
| Implant integration check | Folded into Follow-up map | ✓ OK | — |
| Second-visit-of-two-visit-treatment | Folded into Follow-up map | ✓ OK — but see `lab_ready_check` gap | Add lab-ready flag for crown/veneer/bridge cases |
| — | (fixture-only surprise?) | none | Fixture has no rows that playbook doesn't cover. |

**Summary:** Fixture covers 10 of 28 (36%). Missing 18 services span every category except emergency + recall. High-priority adds for regression coverage: Pediatric cleaning, Deep cleaning, Crown, Root canal, Simple extraction, Invisalign treatment start.

---

## Required-info slot matrix

Cell values: **REQ** = MUST collect before `book_appointment` fires; **OPT** = collect if easy; **N/A** = not applicable to this service; **CHART** = lookup via `find_existing_appointment` (not asked from caller).

| Service | phone | name | date | time | service_subtype | provider | insurance | referral | original_procedure | original_provider | original_visit_date | age | pain_context |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| New patient exam w/ X-rays | REQ | REQ | REQ | REQ | N/A | OPT | OPT | N/A | N/A | N/A | N/A | N/A | N/A |
| Adult recall exam | REQ | REQ | REQ | REQ | N/A | CHART | OPT | N/A | N/A | CHART | CHART | N/A | N/A |
| Pediatric first visit | REQ | REQ (parent+child) | REQ | REQ | N/A | REQ (Whitfield) | OPT | N/A | N/A | N/A | N/A | REQ | N/A |
| Emergency exam | REQ | REQ | REQ | REQ | N/A | OPT | OPT | N/A | N/A | N/A | N/A | OPT | **REQ FIRST** |
| Invisalign consultation | REQ | REQ | REQ | REQ | N/A | REQ (Ramanathan) | N/A (free consult) | N/A | N/A | N/A | N/A | N/A | N/A |
| Implant consultation | REQ | REQ | REQ | REQ | N/A | REQ (specialist) | OPT | OPT | N/A | N/A | N/A | N/A | N/A |
| Cosmetic consultation (playbook) | REQ | REQ | REQ | REQ | REQ (which cosmetic goal) | OPT | N/A | N/A | N/A | N/A | N/A | N/A | N/A |
| Adult cleaning | REQ | REQ | REQ | REQ | N/A | CHART (hygienist pref) | OPT | N/A | N/A | N/A | N/A | N/A | N/A |
| Pediatric cleaning | REQ | REQ (parent+child) | REQ | REQ | N/A | OPT | OPT | N/A | N/A | N/A | N/A | REQ | N/A |
| Deep cleaning (SRP) | REQ | REQ | REQ | REQ | N/A | OPT | **REQ** (pre-auth) | OPT | N/A | N/A | N/A | N/A | N/A |
| Composite filling | REQ | REQ | REQ | REQ | OPT (# surfaces) | OPT | OPT | N/A | N/A | N/A | N/A | N/A | N/A |
| Crown (prep or seat) | REQ | REQ | REQ | REQ | REQ (prep vs seat) | REQ (continuity if seat) | REQ | N/A | REQ if seat | REQ if seat | REQ if seat | N/A | N/A |
| Root canal | REQ | REQ | REQ | REQ | REQ (which tooth) | REQ (generalist vs endo) | REQ | OPT | N/A | N/A | N/A | N/A | OPT |
| Zoom whitening | REQ | REQ | REQ | REQ | N/A | OPT | N/A (cash) | N/A | N/A | N/A | N/A | N/A | N/A |
| Simple extraction | REQ | REQ | REQ | REQ | REQ (which tooth) | OPT | OPT | N/A | N/A | N/A | N/A | N/A | OPT |
| Surgical extraction | REQ | REQ | REQ | REQ | REQ (which tooth) | REQ (oral surgeon) | REQ | OPT | N/A | N/A | N/A | N/A | OPT |
| Wisdom teeth | REQ | REQ | REQ | REQ | N/A | REQ (oral surgeon) | REQ | OPT | N/A | N/A | N/A | N/A | N/A |
| Implant placement | REQ | REQ | REQ | REQ | N/A | REQ (specialist) | REQ | OPT | N/A | N/A | N/A | N/A | N/A |
| **Follow-up visit** | CHART then REQ | CHART then REQ | REQ | REQ | N/A | **REQ (continuity)** | N/A (rule-based) | N/A | **REQ (drives duration)** | **REQ (continuity)** | **REQ (30-day rule)** | N/A | N/A |
| Post-antibiotic recheck | CHART then REQ | CHART then REQ | REQ | REQ | N/A | REQ (continuity) | N/A | N/A | REQ (=antibiotic) | REQ | REQ | N/A | N/A |
| Implant integration check | CHART then REQ | CHART then REQ | REQ | REQ | N/A | REQ (continuity — same specialist) | N/A | N/A | REQ (=implant) | REQ | REQ | N/A | N/A |
| Cancel / reschedule (not a service, but tool) | REQ (or name lookup) | OPT | — | — | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A | N/A |

**Reading the matrix as an engineer:**

- Every column marked **REQ** for a service that today's DISCOVER_CONTEXT / NextActionPolicy does NOT gate on is a false-complete waiting to happen.
- The Follow-up row was the audit's original discovery — that's why `docs/product/journey-audit-follow-up-clinic-2026-08-29.md` exists and Gap 5 shipped.
- Consultation is the next branch — three separate REQ subtypes with three different provider constraints.
- Emergency exam's `pain_context` REQ is the ONLY one where triage happens BEFORE any calendar action; it feeds an escalate-or-schedule decision, not a duration.

---

## Ambiguous-request catalog

Caller phrasings the agent will hear + which taxonomy branches they COULD mean + the clarifying ask that disambiguates.

| Caller says | Possible services | Clarify by asking |
|---|---|---|
| "a follow-up" | Follow-up visit (post-filling / post-antibiotic / implant integration check / second-visit-of-two-visit / recheck) | "A follow-up to what — was it after a filling, cleaning, something else?" THEN "who did the work?" THEN "roughly when?" (playbook §4 first bullet; audit doc) |
| "a cleaning" | Adult cleaning / Pediatric cleaning / Deep cleaning (SRP) | "Is this for you or a child?" AND if adult with any gum/bleeding context "regular cleaning, or was the hygienist wanting a deep clean?" |
| "a check-up" | New patient exam with X-rays / Adult recall exam / Pediatric first visit / Emergency exam if pain | "Have you been in before?" (routes new vs recall) then "any pain or is this routine?" (routes routine vs emergency) |
| "an exam" | Same as check-up | Same as above |
| "consultation" or "consult" | Invisalign / Implant / Cosmetic / Second-opinion | "Consultation for what — Invisalign, implants, cosmetic work, or a second opinion?" — DO NOT resolve bare consultation without a topic |
| "an extraction" | Simple extraction (generalist) / Surgical extraction (may refer) / Wisdom teeth (oral surgeon) | "Which tooth — one of the wisdom teeth, or one of the ones up front?" AND "is it hurting right now?" (routes emergency vs planned) |
| "something's hurting" / "my tooth hurts" | Emergency exam MOST LIKELY; potentially root canal / extraction / follow-up-if-post-op | PAIN TRIAGE FIRST — "when did it start? how bad, one to ten? any swelling? any bleeding?" — DO NOT proceed to calendar until triage complete (playbook §4 + §5 "Missed same-day emergency") |
| "just a look" | Almost always New patient exam (first-time) OR Adult recall (returning) | "Have you been in before?" then explain that first visit needs X-rays for baseline (playbook §4) |
| "my kid needs something" / "my child" / "my son/daughter" | Pediatric first visit / Pediatric cleaning / Emergency exam (kid injury) | "How old is your child?" AND "is it a first visit, a check-up, or something happened?" |
| "I want whitening" / "whiten my teeth" | Zoom whitening (in-office) / Take-home whitening trays | "In-office one-and-done, or take-home trays over a couple weeks?" (playbook §7 cross-sell) |
| "Invisalign" / "braces" | Invisalign consultation | Currently fine — Ramanathan is the only ortho provider per fixture. If practice adds traditional braces later, disambiguate. |
| "I want an implant" | Implant consultation (first) → Implant placement (later, needs consult first) | "Have you had the consultation with our doctor first? That'd be the first step." — do not book placement directly. |
| "root canal" | Root canal (generalist) / Root canal referral to endodontist | "Which tooth?" — front teeth generalist, molars often referred to endodontist |
| "I need a crown" | Crown prep (first visit) OR Crown seat (returning after prep) | "Is this a new crown, or are you coming back to have one placed that was prepped already?" — routes to different service |
| "my doctor / dentist referred me" | Almost always specialist branch (endo, oral surgery, periodontist, ortho) | "Referred for what specifically?" — do NOT book with generalist by default (playbook §5 "Missed the referral trigger") |
| "the earliest slot" / "as soon as possible" | Any — this is a time preference, not a service | Do NOT resolve to a service; ask what service they need |
| "same time as last year" | Almost always Adult recall (routine annual) | Chart lookup on phone; confirm service = last year's recall type |
| "I haven't been in five years" | New patient exam (chart is stale) — treat as new patient | Warm reassurance (playbook §2 anxious bullet) then route to new patient exam |
| "do you take Delta Dental?" | NOT a booking — Insurance question archetype | Answer from FAQ; offer to book if they'd like (playbook §2) |
| "cancel my appointment" | `cancel_appointment` tool, not a service | Ask for phone; chart lookup |
| "move / reschedule my appointment" | `reschedule_appointment` tool | Chart lookup; ask new date/time |
| "do you speak Spanish?" | NOT a booking — Language question | Fixture FAQ: yes, Rosa. Route to her if patient wants Spanish provider. |
| "am I talking to a person?" | NOT a booking — AI disclosure trigger | Say the disclosure line (playbook §6) then keep helping |

---

## Recommendations for engineering

### Product-side (belongs in fixture / playbook / alias map)

1. **Fixture gap: 18 services missing** — see reconciliation table. Highest priority for Phase 4 regression coverage: Pediatric cleaning, Deep cleaning, Crown (prep vs seat), Root canal, Simple extraction, Invisalign treatment start. These cover the most-common ambiguous-caller-phrase branches.

2. **Fixture gap: structured price field missing** — audit Gap 6. Prices currently live inside `description` as English text ("One eighty nine dollars"). This is unparseable for the price-quote turn AND for the 30-day-free-window check on Follow-up. Recommend adding `price_cents: int` and `price_display: str` to `ServiceOffering`. Pediatric first visit has NO price at all.

3. **Container-service pattern (audit Gap 5 propagation)** — the `duration_by_original_procedure` map that shipped for Follow-up is a template. Next candidates in priority order:
   - **Consultation** — add `duration_by_consultation_topic` + `provider_by_consultation_topic`. Highest value: "consultation" is a common bare-word phrase and the current alias resolution is non-deterministic.
   - **Cleaning** — add `duration_by_cleaning_type`. Currently pediatric routes to `Pediatric first visit` (45min) not `Pediatric cleaning` (30min, doesn't exist yet).
   - **Extraction** — add `duration_by_extraction_type` + `provider_by_extraction_type`. Currently extraction has zero fixture rows so any request falls through to fuzzy match.

4. **Alias map holes** (`packages/integrations/service_aliases.py`):
   - `"consultation" → ("consultation",)` is dangerous — will pick whichever consult tenant service alphabetizes first. Fix: return an `AMBIGUOUS` result with sub-type prompt, OR require caller to say the topic before resolving.
   - `"braces" → ("invisalign",)` is Plano-fixture-correct but wrong for any tenant with traditional braces. Fix: keep the alias but tag it as tenant-scope-fragile.
   - No aliases for "crown", "root canal", "extraction", "wisdom teeth", "denture", "bridge", "veneers", "sealants", "fluoride", "sedation" — every one is a real caller phrase.
   - No aliases for insurance names ("Delta Dental", "Cigna", "BCBS") — these should route to the insurance-question NON-BOOKING branch, not attempt service resolution.

5. **DISCOVER_CONTEXT branches needed beyond Follow-up** (matrix rows with REQ in non-standard columns):
   - Emergency exam: `pain_context` MUST fire before any calendar action. Today's flow does not.
   - Consultation: `service_subtype` MUST fire before duration is picked.
   - Crown / Extraction / Root canal: `tooth_location` or `visit_number` MUST fire before duration + provider are picked.
   - Pediatric anything: `age` MUST fire before provider assignment.

### Engineering side (belongs in code — for reference, not for this doc to fix)

- `NextActionPolicy` should NOT decide ASK_SLOT(phone) until required-info slots for the RESOLVED service are collected. Applies to all container services, not just Follow-up.
- `find_existing_appointment` (`packages/integrations/clinic_tools.py:99`) exists and works but is only called on the Follow-up path. Adult recall, Cancel, and Reschedule ALL need it too. The chart is authoritative over caller memory (playbook §5 last bullet).
- Cross-sell hooks catalogued above have NO home in code today. A new `packages/dialogue/cross_sell.py` module (or a hook on `book_appointment` post-success) is where they belong. Not urgent for Phase 4 but the taxonomy is here when someone gets to it.
- Provider-day availability (playbook §5 "Provider constraint hidden until booking-time failure") — the tenant fixture needs a `providers[]` field with per-provider `days_worked[]`, and `check_availability` needs to filter by it when the caller names a provider. Fixture currently has no `providers[]` structure.

---

## Playbook maintenance

Playbook was already updated 2026-08-30 by the golden-scripts session and 2026-08-29 by the Christiaan follow-up session. This taxonomy session did not surface any NEW practice patterns that the playbook was missing — the fixture-gap and container-shape findings live in this deliverable, which is where engineering will consume them. Playbook untouched by this session.

## Sources cited

- `.claude/plugins/product-lead/product_playbooks/clinic.md` (canonical service list, ambiguity notes, failure modes, cross-sell)
- `sample-data/clinic/business.json` (current fixture — Smile Dental Plano)
- `sample-data/clinic/business.riverside-old.json` (old GP fixture — kept for shape comparison, not authoritative)
- `packages/integrations/service_aliases.py` (existing caller-phrase → keyword map)
- `packages/integrations/clinic_tools.py` (`check_availability`, `book_appointment`, `find_existing_appointment`, `cancel_appointment`, `reschedule_appointment`)
- `packages/schemas/business.py` (`ServiceOffering` with shipped `duration_by_original_procedure`)
- `docs/product/journey-audit-follow-up-clinic-2026-08-29.md` (Follow-up DISCOVER_CONTEXT spec)
- `docs/product/golden-scripts-clinic-2026-08-30.md` (Phase-4 sweep reference scripts)
- Commit `d099354` (Gap 5 `duration_by_original_procedure` ship)
