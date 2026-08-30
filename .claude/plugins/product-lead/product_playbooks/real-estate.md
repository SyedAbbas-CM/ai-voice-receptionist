# Real estate — product playbook

Domain-knowledge reference for the product-lead agent when reviewing
receptionist features for real-estate brokerage / property-management
tenants.

Status: **PARTIAL — clinic-quality on EU (Portugal-centric) content.**
Filled to Ribeira Prime demo depth using the tenant fixture at
`sample-data/real-estate/business.json` + the demo script at
`docs/DEMO-SCRIPT-REAL-ESTATE-2026-08-25.md`. US content still sketch-
level; needs a US broker interview to reach the same depth. All EU
legal / tax claims that touch statute are marked
`[VERIFY WITH LOCAL COUNSEL]` — the receptionist must never quote them
to a caller as advice, and this playbook is not legal reference.

Last updated: 2026-08-30 by real-estate playbook enrichment session.
Previous: 2026-08-29 by voice-agent session (initial draft).

## 1. Business shape

### Staffing patterns

- **EU independent boutique (Ribeira Prime shape):** broker/owner +
  3-8 licensed agents. One office manager or front-desk lead (Sofia
  in the fixture) fields inbound calls when agents are showing
  property. Common in Lisbon, Porto, Barcelona, Madrid, Milan,
  Paris intra-muros. Weekends peak because expats + investors fly
  in for viewing weekends.
- **EU franchise office (Century 21 / RE/MAX / ERA / Engel & Völkers
  European branches):** 15-40 agents, dedicated reception + admin,
  MLS-adjacent internal listing pool. Reception routes by
  geography + specialty (residential vs commercial vs luxury vs
  rental).
- **EU property-management-only:** landlord-facing, no showings —
  reception handles maintenance requests, rent-collection
  questions, lease-renewal calls. Different flow entirely (see §3
  Landlord services).
- **US independent brokerage:** broker/owner + 5-30 agents. Admin /
  office manager fields inbound calls. US MLS access is a hard
  divide from EU — every listing in a market is in the shared MLS,
  which changes the "do you have anything in X" answer.
- **US teams-within-brokerage (Keller Williams / Compass model):**
  each team has its own inbound line + team admin. Reception may be
  outsourced to a call-center like Smith.ai or Ruby.

### Hours

- **EU:** weekends are peak (showings), weekday evenings second.
  Weekday morning is admin / lead-callbacks. August is DEAD in
  southern Europe (Lisbon, Madrid, Milan, Athens — most agents on
  holiday, but the caller doesn't know that). Fixture hours 09:00-
  19:00 Mon-Fri, 10:00-16:00 Sat, closed Sun.
- **US:** weekends peak, weeknight evenings solid. No August dead-
  zone (US market runs year-round). Sunday open-house culture is
  much stronger than in EU.

### Physical footprint

Office + agents-in-the-field. Reception often at the office, agent-
in-car needs mobile forwarding. EU offices frequently on street-
front retail — walk-ins for shortlist browsing are common,
especially in tourist-adjacent neighborhoods (Chiado, Barrio de las
Letras, Trastevere, Le Marais).

### Payment + commission conventions

- **EU commission:** typically 3-6% of sale price + VAT (IVA), paid
  by the SELLER. Commission is disclosed at the listing agreement
  (mandato) stage, not to buyers on the phone. Ribeira Prime
  fixture: 5% + VAT. Buyers pay nothing directly to the brokerage
  (their side is agent-of-record via the mandato). Watch: "how
  much is your commission" from a buyer is either a mis-framed
  question or a due-diligence question about the total transaction
  cost — the answer is "buyers pay nothing to us."
- **VAT on commission:** must be quoted VAT-inclusive OR VAT-
  exclusive with clear labeling. Portugal IVA is 23% on brokerage
  service, Spain IVA 21%, Italy IVA 22%, France TVA 20%, Germany
  MwSt 19%. Quoting "5%" without disclosing "+ VAT" IS a UX bug
  that produces a €X thousand disagreement at contract stage.
  [VERIFY WITH LOCAL COUNSEL] for current rates — they shift.
- **US commission:** typically 5-6% total split ~50/50 between
  listing agent and buyer's agent (though post-NAR-settlement 2024
  the buyer-side split is now negotiable / caller-visible in more
  markets). US commissions are baked into sale price by market
  convention; a US caller asking "how much do you charge me as a
  buyer" now sometimes has a real answer (buyer's agent commission
  agreement).
- **Reservation fees:** Portugal + France + parts of Spain — a
  reservation deposit on the property (not commission) can be
  requested before viewing high-demand listings. Rare in the
  Ribeira Prime tier but common in luxury / off-market. Never ask
  the caller to pay over the phone.

### Regulatory shape (EU)

- **Licensing:** national or provincial. Portugal: AMI (Agência de
  Mediação Imobiliária) number, issued by IMPIC. Spain: agent must
  be registered in the region's mediador inmobiliario registry
  (Cataluña, Andalucía have separate rules). France: carte
  professionnelle transaction, issued by CCI. Germany: § 34c
  Gewerbeordnung licence. Italy: agente immobiliare abilitato
  registered with the local Camera di Commercio.
  [VERIFY WITH LOCAL COUNSEL] before quoting requirements to a
  caller.
- **Cross-border:** an agent licensed in Portugal cannot legally
  broker a Spanish property. Cross-border referrals go to a
  partner licensed in the target country.
- **GDPR:** hard requirement, not the soft "yeah data protection"
  US posture. See §6.

### Regulatory shape (US)

- **Licensing:** per-state. NY, CA, TX all have different pre-
  licensing hours + exam. Reciprocity is patchy. Reception should
  never book cross-state without checking.
- **Fair Housing Act (FHA 1968 + amendments):** 7 federally
  protected classes (race, color, national origin, religion, sex,
  familial status, disability). State + local extensions add more
  (sexual orientation, gender identity, source of income, immigration
  status). Steering violations are $10k+ per instance.
- **NAR settlement (Aug 2024):** buyer's agent commissions can no
  longer be published on the MLS; buyer must sign a Buyer
  Representation Agreement before touring. This has changed the
  "let's just go see it" call flow materially — receptionist now
  needs to gate viewings on BRA-signed status. [VERIFY WITH LOCAL
  COUNSEL] for the current state of implementation, which is
  still shifting.

## 2. Real caller archetypes

### Buyer-side

- **Local buyer with a specific listing** — has a URL / MLS ref /
  street address, wants to book a showing. Needs: property_ref,
  agent availability, quick buyer-qualification snapshot (budget +
  timeline + financing status). Fastest-turn caller.
- **Local browser (open search)** — no specific listing, wants
  "anything in Chiado around 700k". Needs preferred_areas + budget
  band + property_type + bedrooms_min before any agent time is
  worth booking. Should nudge to buyer qualification call, not to
  a viewing.
- **International buyer (relocating for work / lifestyle)** — most
  likely archetype for Ribeira Prime tier. Non-resident, needs the
  full non-resident process explained (NIF, PT bank, financing
  route, closing timeline, IMT tax). Currency preference is often
  their home currency (GBP, USD, CHF, BRL) — quoting only EUR
  without acknowledging conversion sensitivity is a UX miss. Common
  origins for Portugal: UK, France, Brazil, US, Germany, South
  Africa. Common concerns: currency-transfer mechanics, tax
  residency implications, timeline vs their visa / job start.
- **Diplomat / expat relocation** — corporate-sponsored or
  diplomatic relocation, often via a relocation agency. Different
  questions: proximity to embassy district, international-school
  district (Carlucci / St. Julian's / German School / French Lycée
  / American School), lease-vs-buy for a fixed posting length,
  furnished vs unfurnished. Cost is often not the primary driver —
  timeline + school + safety are. Very likely to book a
  neighborhood tour before a specific viewing.
- **Retirement-visa candidate** — Portugal D7 (passive-income
  visa), Spain non-lucrative visa (NLV), France long-stay VLS-TS,
  Italy elective residence, Greek golden-visa-adjacent routes.
  These visas interact with property purchase but do NOT require
  it (Portugal's Golden Visa real-estate route closed Oct 2024;
  fixture FAQ correctly says so). Caller often asks the wrong
  question — "does buying a house get me a visa" — and needs a
  gentle redirect: property is a lifestyle choice, visa
  qualification is separate (proof of income, health insurance,
  criminal record certificate). Refer to immigration lawyer, do
  NOT advise on visa mechanics. [VERIFY WITH LOCAL COUNSEL]
- **Foreign investor / cash buyer** — bulk-buy, buy-to-let,
  fix-and-flip, or short-term-rental (Alojamento Local /
  Vivienda de Uso Turístico) portfolio. Needs: NIF/NIE tax number
  status, wire-transfer + AML source-of-funds documentation
  (Portugal 15k+ EUR triggers AML reporting; US patriot-act-
  equivalent), portfolio-tour scheduling, investor consultation
  before individual viewings. Never process the wire on the phone.
- **Buyer-agent-represented shopper** — already working with a
  buyer's agent from a different brokerage, calling to view one
  of YOUR listings. Fixture correctly has a
  `working_with_other_agent` boolean on `book_viewing`.
  Receptionist must ask and record — showing a represented buyer
  without disclosure creates a commission-dispute risk.
- **Just-browsing** — no intent, curious about the market, may
  become a lead in 6-24 months. Job-brief edge case #3. Do NOT
  force qualification. Offer the shortlist by SMS/WhatsApp, take
  a light-touch message, let them go warmly.

### Seller-side

- **Valuation curious** — homeowner wondering what their property is
  worth. Ambiguity: for selling now / D7 visa net-worth requirement
  / divorce settlement / probate / just curious. Free consult for
  lead generation. NEVER quote a valuation on the phone (fixture
  persona rule + regulatory best practice — desktop-valuation-only
  quotes are frequently wrong by 15-30% in inner-city Lisbon).
- **Ready-to-list seller** — has decided to sell, wants to
  interview brokerages. Needs listing appointment (home visit +
  CMA + commission + timeline). Often shopping 2-3 agencies —
  receptionist should book quickly and confirm what agents
  compete on (marketing budget, exclusivity terms).
- **Off-market inquiry** — high-value property owner who doesn't
  want a public listing. Confidential. Should route directly to
  broker/owner, not round-robin.
- **Landlord (rental listing)** — wants to list a rental. Needs
  qualification: furnished / unfurnished, long-term vs AL
  (Portugal short-term rental licence), current tenant status,
  desired monthly rent, management-included vs listing-only.
- **Landlord (management pain)** — existing rental, tenant issue
  (non-payment, damage, wants to break lease). Escalate — not a
  receptionist decision.

### Rental-side

- **Local long-term rental applicant** — needs listing +
  qualification (income multiple typically 3x rent EU-standard,
  though PT landlords often require a *fiador* guarantor OR 3-12
  months rent upfront especially from foreign applicants).
  Different flow from buyer.
- **Rental applicant with foreign income** — remote-worker on
  foreign payroll, retiree on foreign pension. Portugal +
  Spain landlords are often uneasy — will typically demand
  fiador guarantor OR 6-12 months rent upfront OR a rental
  insurance product (Portugal: seguro de renda). Receptionist
  must set this expectation before viewing, not after.
- **Short-term / mid-term rental** — Airbnb-style stays. In
  Portugal (esp Lisbon central parishes), the property either has
  an active AL licence or it doesn't — brand new AL registrations
  are restricted in Lisbon's absolute-containment parishes. Very
  different question from long-term rental. Route to investment
  consultation if the caller wants to BUY for STR.
- **Corporate relocation rental** — company pays, needs an
  invoice / VAT-compliant billing, often furnished + serviced.
  Different flow again.

### Other

- **Neighborhood question** — "what's the noise like on Rua da
  Prata" — factual. In EU, do NOT wander into demographic /
  religious / ethnic characterizations (steering-adjacent even in
  EU; Portugal + Spain + France all have anti-discrimination
  housing law). Redirect to city website / walk-the-street
  suggestion / agent for listing-specific.
- **Existing client callback** — already working with a specific
  agent by name. Fixture rule: `always_transfer_on` includes
  `asks_for_specific_agent_by_name`. Correct.
- **Legal / compliance** — landlord-tenant disputes, HOA
  (condomínio) questions, tax questions. Escalate. Refer to
  attorney (advogado) / accountant (contabilista certificado) as
  appropriate. Never advise.
- **Complaint / angry caller** — missed viewing, agent no-show,
  price disagreement. Fixture rule: `always_transfer_on` includes
  `complaint`. Correct. Job-brief edge case #2.
- **Prompt-injection / red-team** — imported from clinic playbook.
  Rare in production but always a possibility. Stay in scope,
  refuse the meta-question, snap back to booking.

## 3. Full service catalog

Real-estate brokerage menu at receptionist granularity. Durations
mirror the Ribeira Prime fixture; adjust per tenant.

### Buyer services

- **Buyer qualification call** — 20min, phone or video. Budget,
  financing, timeline, must-haves, non-resident status. Ribeira
  fixture: `qualify_buyer_lead`. This is the correct FIRST
  service for any buyer without a specific listing in hand.
- **Property viewing** — 45min, in-person at specific listing.
  Requires property_ref. Ribeira fixture: `book_viewing`.
- **Portfolio tour** — 3-4 hours, multi-property same day.
  Common for out-of-country buyers on a viewing weekend.
  Needs pre-qualification + pre-selected shortlist.
- **Virtual tour** — 30min, WhatsApp video or Zoom.
  Good for buyers abroad, tight schedule, or filter-before-fly.
  Ribeira fixture: `book_virtual_tour`.
- **Virtual tour WITH agent live** — agent physically at property,
  buyer joins on video. Different from pre-recorded walkthrough
  video. Highest-conversion virtual format.
- **Neighborhood tour** — 90min, buyer + agent walk / drive.
  Common for relocators. School district, commute, amenities.
- **Broker introduction call** — 15min, get-to-know-you before a
  serious commitment.
- **Pre-approval mortgage intro** — 15min, warm handoff to
  preferred mortgage broker. High-value for non-resident buyers
  who don't know PT lenders.
- **Notary appointment coordination** — no fixture tool yet, but
  a real service. Escritura pública (Portugal) / Escritura de
  compraventa (Spain) / Acte authentique (France) requires
  scheduled notary time. Receptionist coordinates 3-way calendar
  (buyer, seller-agent, notary).

### Seller services

- **Home valuation visit** — 60min, seller-side visit + CMA
  discussion. Ribeira fixture: `book_valuation_visit`.
  Follow-up to `qualify_seller_lead`.
- **Listing appointment** — 90min, property visit + market
  analysis + mandato (listing agreement) discussion. Longer than
  valuation because the agent is pitching to WIN the listing.
- **Exclusivity signing (mandato de venda exclusivo, PT)** — 30-45
  min at office. Signing an exclusive listing agreement. Legal
  document; broker/owner or senior agent, not junior. Portugal
  6-month exclusivity typical. [VERIFY WITH LOCAL COUNSEL]
- **Pre-listing walkthrough / staging consult** — 45min. Advice
  on cleaning, decluttering, minor repairs, professional
  photography.
- **Home valuation (phone / desktop, remote)** — 30min consult.
  Ribeira persona explicitly REFUSES this — always requires visit.
  Different tenants may allow it with a strong "estimate only"
  caveat.

### Rental services

- **Rental viewing** — 30min, in-person at specific rental.
- **Rental inquiry / application info** — 15min consult over
  phone / email. Ribeira fixture: `qualify_rental_lead`.
- **Rental application submission coordination** — not a fixture
  tool yet. Real service: gather docs (NIF, employment letter,
  bank statements, prior-landlord reference, fiador contact),
  package for landlord. Portugal: seguro de renda alternative to
  fiador.
- **Corporate rental setup** — furnished + serviced, VAT invoice.
  Different pricing tier.

### Investment services

- **Investment consultation** — 45min. Yield expectation, target
  neighbourhoods, AL licensing status, financing structure.
  Ribeira fixture: `qualify_investor_lead` +
  `book_investor_consultation`.
- **AL licence status check / AL-licensable property search** —
  specialty subtype. Portugal Lisbon central parishes have new-AL
  restrictions; buying WITH an existing AL is very different from
  buying without. Ribeira FAQ correctly flags this.
- **Portfolio review** — 60-90min for existing investor with 3+
  properties, portfolio-level yield + rebalance discussion.

### Landlord / property-management services

- **Property-management onboarding** — landlord signs a management
  contract, brokerage takes over tenant relations, rent
  collection, maintenance coordination. Monthly fee typically
  6-10% of collected rent + tenant-placement fee equivalent to
  1 month rent. Real service, not in fixture tools yet.
- **Short-term rental management** (STR / AL) — separate service
  tier from long-term management. Common margin 15-25% of gross
  rental. Requires AL licence on the property.

### Utility / non-service tool calls

- **Lookup FAQ** — Ribeira fixture: `lookup_faq`. Topics: areas
  served, commission, financing, non-resident, golden visa, CPCV,
  IMT, closing timeline, AL, school district, language.
- **Take message** — Ribeira fixture: `take_message`. Off-hours,
  caller declines callback now, or agent unavailable.
- **Escalate to human** — Ribeira fixture: `escalate_to_human`.
  Complaint, offer > 500k, legal question, specific-agent request.

## 4. Ambiguous requests → clarification

Concrete ambiguity examples the receptionist MUST clarify — each is
a DISCOVER_CONTEXT branch before any tool call.

- **"I want to see a property"** → which property?  URL / MLS
  reference / listing number / street address? If none, "no
  specific property in mind" → route to buyer qualification call,
  NOT a viewing.
- **"Do you have anything in X neighborhood"** → price range,
  bedrooms, buy vs rent, timeline. If EU: is EUR the working
  currency? If US: what price range without asking anything
  protected-class (avoid "for you and your family", "who else
  will be living there" — those are FHA trip-wires).
- **"What's my house worth"** → for selling NOW / for a D7 visa
  net-worth requirement / divorce settlement / probate / just
  curious? Different urgency, different service. Never quote a
  number on the phone regardless.
- **"I want to rent"** → short-term (AL-licensed / Airbnb, PT
  central Lisbon has restrictions) / mid-term (3-6 mo, corporate
  / digital nomad) / long-term (12+ mo, standard tenancy)?
  Different services, different fixtures, different regulatory
  regime.
- **"I'm relocating"** → from where (currency + tax residency
  implication), timeline, remote-first or coming to look, needs
  airport pickup / temporary housing, family size (school
  district — but ASK about schools, do not infer family
  composition from the question).
- **"Can I buy a house and get a visa"** → AMBIGUOUS +
  REGULATORY-SENSITIVE. Portugal Golden Visa real-estate route
  is closed since Oct 2024. Spain non-lucrative visa doesn't
  require property. D7 requires stable passive income (property
  rental can count). Never advise on visa mechanics — refer to
  immigration lawyer. Ribeira FAQ has the correct short answer
  for the golden-visa question specifically. [VERIFY WITH LOCAL
  COUNSEL]
- **"What's the commission"** → from a buyer usually means "what
  is the total cost of the transaction to me" not literally the
  brokerage commission. Answer: "buyers pay nothing to us — the
  seller pays commission. Your costs are IMT (transfer tax), IS
  (stamp duty), notary + registration (~1-2k EUR), legal fees if
  you use a lawyer." Then offer to route to FAQ or take-message
  for a written breakdown.
- **"Is this VAT included"** — critical clarification on any
  commission or fee quote. Portugal IVA 23% on brokerage; a "5%
  commission" is genuinely different from "5% + IVA" (6.15%
  effective). ALWAYS quote VAT-inclusive OR VAT-exclusive with
  explicit label. Never ambiguous.
- **"Can I make an offer"** → not a receptionist decision.
  Always escalate to the listing agent. Fixture rule:
  `offer_over_500k` → auto-transfer. Below that threshold, take
  a message with clear "you're making an offer of X on property
  Y" summary and route to listing agent.
- **"Just a look"** → probably new-buyer just-browsing.
  Job-brief edge case #3. Do NOT push qualification. Offer
  shortlist SMS + take a light-touch message.

## 5. Real failure modes (ordered by frequency + severity)

### High-severity, high-frequency

- **Booked showing without confirming agent calendar** — showing
  conflicts with agent's other appointment. Fixture mitigates
  via `check_viewing_availability` before `book_viewing`.
  Regression: if the agent skips the check and pattern-matches to
  a time the caller proposed, this fires. Guard: booking policy
  must gate on prior availability-check result.
- **Quoted commission % but caller was asking VAT-inclusive vs
  VAT-exclusive** — €X thousand disagreement at closing.
  Detection: any commission quote missing an explicit
  "+ IVA / + VAT" / "IVA-inclusive" tag. Prevention: reception
  script MUST include the VAT frame every time.
- **Booked viewing without asking if buyer has NIF/NIE** —
  Portugal + Spain require the tax ID before any offer is
  legally submitted. If a non-resident shows up to a viewing,
  falls in love, wants to offer, and has no NIF, the deal
  stalls 1-4 weeks while they get one. Fixture correctly asks
  `has_portuguese_nif` on `qualify_buyer_lead` — but no gate on
  `book_viewing`. Gap: reception should set expectation
  ("you'll need a NIF before we can process an offer — want us
  to help set that up?") when booking a non-resident viewing.
- **Missed the AL licence question on investment intent** —
  buyer wants a Chiado studio "for Airbnb" without knowing the
  parish is in Lisbon's AL absolute-containment zone. Books a
  viewing on a property with no AL potential. Wasted time.
  Fixture `qualify_investor_lead.wants_al_licence` captures
  intent but there's no PROPERTY-side field yet on
  `book_viewing` for `property_has_al_licence`. Gap for
  engineering.
- **Currency assumption wrong** — told EUR when the buyer was
  thinking in USD / GBP / CHF / BRL. High-value buyers
  frequently think in home currency; agent quotes 750k EUR,
  buyer heard 750k USD, offer comes in low. Prevention: on
  non-resident flow, explicitly confirm "budget in EUR, or in
  your home currency?" Gap: no `currency_preference` field on
  buyer lead.
- **False-complete on Portuguese-name pronunciation** — imported
  clinic pattern. Caller says "Joana Fernandes", agent hears
  "Johanna Fernandez", CRM has the wrong name, follow-up SMS
  gets the wrong salutation. High trust-erosion. Prevention:
  same as clinic — tenant-level `caller_name_phonetic[]` keyed
  by phone.
- **Steering violation** — recommending a neighborhood based on
  what the receptionist infers about caller ethnicity /
  religion / family status. Illegal US Fair Housing violation
  ($10k+ per instance). Illegal in EU under equal-treatment
  directives + national anti-discrimination housing law
  (Portugal Lei 27/2016, Spain Ley 12/2023, France Code de la
  Construction). Prevention: never volunteer demographic
  characterization; only quote factual neighborhood data
  (school proximity, transport, price range) when caller asks.

### Medium-severity

- **Didn't qualify buyer before showing high-end property** —
  agent shows €2M house to unqualified buyer, wasted 3 hours.
  Fixture mitigates via `qualify_buyer_lead` before
  `book_viewing` for high-value listings. Gap: no auto-gate on
  listing value threshold.
- **Wrong agent** — routed a listing inquiry to a buyer's agent,
  or vice versa. Some agents ONLY do buy-side or ONLY do
  sell-side. Gap: no `agent_specialty[]` field to route on.
- **Missed the rental application deposit question** — didn't
  warn about fiador / 3-12 months upfront / rental insurance
  BEFORE viewing. Applicant sees flat, applies, gets shocked by
  the deposit demand.
- **GDPR slip** — collected personal data on an EU call without
  disclosing purpose + retention. Fixture correctly opens with
  `recording_notice_script_en/pt`.
- **No-show without deposit** — booked viewing that the buyer
  never showed for; agent lost billable time. Some EU markets
  do reservation fees for viewings; most don't. Prevention:
  SMS reminder + optional confirmation-reply pattern.
- **Ghost lead** — leads collected but never fed into the CRM.
  Fixture writes to HubSpot EU + local SQLite outbox for
  retry.
- **Offer-over-threshold not escalated** — fixture
  `always_transfer_on: offer_over_500k` handles this. If
  threshold not tuned per tenant, might fire too often or
  never.
- **Language-scope faked** — non-EN / non-PT caller opens
  (Mandarin, Ukrainian, Arabic), agent guesses intent from
  tone. Same failure as clinic playbook. Fix: honest
  three-option offer.

### Low-severity but corrosive

- **Called back at wrong time** — international caller in
  different timezone. Prevention: capture
  `preferred_callback_time` + timezone on the message.
- **Provider constraint hidden until booking-time failure** —
  caller asks for a specific agent on a day the agent isn't
  working. Same shape as clinic pattern.
- **Cross-border referral not disclosed** — caller asked about
  Cascais, brokerage only covers central Lisbon (fixture FAQ
  says "we refer to a partner agency"). Reception should
  disclose immediately, not after 5 minutes of chat.
- **Neighborhood question answered from stale knowledge** —
  agent says "very quiet" about a street that got a nightclub
  last month. Prevention: never volunteer subjective
  neighborhood claims; route to listing agent for street-level
  intel.

## 6. Regulatory + safety

Real-estate regulatory surface is bigger than clinic because
international buyers cross borders + brokerage handles money-
adjacent conversations. Treat every rule below as
[VERIFY WITH LOCAL COUNSEL] before deploying to a new market.

### GDPR (EU-wide, hard)

- **Purpose disclosure at call open:** required. Ribeira fixture
  opens with `recording_notice_script_en/pt` — meets the bar.
- **Right to erasure (RGPD Art. 17):** caller can demand
  deletion of their CRM record. Receptionist must know the
  path: escalate to DPO email
  (`dpo@ribeiraprime.example` in fixture).
- **Data portability (Art. 20):** caller can demand a copy of
  their data in machine-readable form. Same escalation path.
- **DPO contact:** if the business is designated as processing
  personal data at scale (brokerages typically qualify), a DPO
  contact is required and must be published + reachable.
  Fixture has `dpo_email`.
- **Sub-processor disclosure:** every third party that touches
  caller data (Twilio, Deepgram, OpenAI, ElevenLabs, HubSpot)
  must be listed. Fixture has `sub_processors` array. This IS
  what a DPO will ask for at contract sign.
- **Retention:** default in Ribeira fixture is 90 days for call
  recording. Legal minimum varies (some markets require longer
  for KYC-adjacent data). [VERIFY WITH LOCAL COUNSEL]
- **Data hosting region:** EU-hosted where possible. Fixture
  `data_hosting_region: EU`. ElevenLabs is US-hosted with a
  signed DPA — this is the one flag a DPO will interrogate.
- **International transfer:** any transfer to a non-adequacy
  country (US pre-DPF, non-EEA generally) requires SCCs
  (Standard Contractual Clauses) + transfer impact
  assessment.

### EU AI Act Article 50 (in force 2 Aug 2026)

- **Disclosure of AI interaction:** callers interacting with an
  AI system must be informed unless it is obvious.
  Voice-agent = not obvious. Ribeira fixture discloses "you are
  the virtual receptionist the first time someone directly asks
  'am I speaking to a person' or 'are you a bot'" — persona
  rule. This is a legally defensible design (disclosure on
  request rather than volunteered) but the safer bet under
  Article 50 is the greeting-line disclosure the fixture
  actually uses in `recording_notice_script_en/pt`: "this call
  is being handled by an AI assistant." Compliant.
- **Article 50 non-compliance = up to €15M or 3% global
  turnover.** Not a soft rule.

### Portugal-specific

- **RJALA (Regime Jurídico de Arrendamento Local):**
  short-term rental (AL) regulatory framework. Decreto-Lei
  128/2014 as amended by Lei 62/2018 + subsequent. Central
  Lisbon parishes designated "áreas de contenção" have
  suspended new AL registrations. Buying a property WITH an
  active AL registration is materially different from buying a
  property that CAN'T get one. Fixture FAQ handles the
  question correctly. [VERIFY WITH LOCAL COUNSEL]
- **NIF (Número de Identificação Fiscal):** tax ID required for
  any property transaction. Non-residents get it through a
  fiscal representative. Fixture flow prompts for it via
  `has_portuguese_nif`.
- **CPCV (Contrato-Promessa de Compra e Venda):** promissory
  purchase contract. Legally binding on both sides. Deposit
  usually 10-20% of purchase price. Fixture FAQ has correct
  short answer.
- **IMT (Imposto Municipal sobre Transmissões):** property
  transfer tax. Sliding scale based on price + primary vs
  secondary residence + resident vs non-resident. Never quote
  a specific number on the phone.
- **Cross-border AML thresholds:** wire transfers over
  €15,000 trigger reporting to Banco de Portugal / BdP under
  Lei 83/2017. Source-of-funds documentation typically
  demanded on 500k+ EUR deals. Receptionist NEVER handles the
  money; escalate any wire question to broker/owner.
  [VERIFY WITH LOCAL COUNSEL]

### Spain, France, Germany, Italy (adjacencies)

- **Spain NIE (Número de Identificación de Extranjero):** analog
  to Portuguese NIF, obtained through consulate or police.
- **France TVA + notaire:** notary is a public officer, not a
  private lawyer; scheduling is different.
- **Germany Grunderwerbsteuer:** property transfer tax 3.5-6.5%
  depending on Bundesland.
- **Italy Imposta di Registro / IVA on new-build:** different
  tax regime for new construction vs resale.
- **Cross-border AML:** EU-wide AMLD5 + AMLD6 apply.

### US (Fair Housing + state licensing)

- **FHA-protected classes:** race, color, national origin,
  religion, sex, familial status, disability. NEVER ask, NEVER
  volunteer a neighborhood recommendation based on them.
- **Steering:** recommending a neighborhood based on protected
  class is a violation. $10k+ per instance.
- **BRA (Buyer Representation Agreement):** post-NAR-settlement
  Aug 2024, buyer must sign a BRA before touring. Reception
  should confirm signed status before booking. [VERIFY]
- **State licensing:** cross-state referrals go to a partner
  licensed in the target state.

### Universal rules

- **NEVER quote:** legal terms of a purchase contract, tax
  liability numbers, mortgage rates today, valuation
  numbers, currency conversion rates. Refer to
  attorney / accountant / lender / broker.
- **AI disclosure on direct question:** covered by AI Act +
  Utah/Texas AI-disclosure laws + Ribeira persona rule.
- **Recording consent:** two-party-consent states in US
  (California, Florida, most of New England) require notice
  at call open. EU covered by GDPR notice. Ribeira fixture
  correct on both.

## 7. Cross-sell / upsell opportunities

Legitimate value-adds a real receptionist raises. All disclosed as
optional; never pressure.

### Buyer-side

- **Viewing booked, remote buyer** → offer virtual tour first to
  save travel. Ribeira fixture supports both.
- **Non-resident buyer without financing** → offer preferred
  mortgage broker intro. Fixture FAQ mentions "three preferred
  mortgage brokers who cover both Portuguese and non-resident
  buyers". Warm handoff = high value.
- **Non-resident buyer without NIF** → offer NIF-setup help.
  Fixture references it in the non-resident FAQ.
- **Ready buyer, high-value (500k+)** → offer buyer
  qualification call BEFORE viewing to pre-vet, and set the
  automatic-transfer threshold flag.
- **Relocation caller** → offer neighborhood tour first + email
  the international-schools shortlist.
- **Post-offer buyer** → warm handoff to notary appointment
  scheduling.

### Seller-side

- **Valuation booked** → offer full listing appointment upgrade
  (pitch to WIN the mandato).
- **High-value seller** → offer pre-listing staging consult.
- **Absentee owner** → offer property-management onboarding
  during the vacancy between listing and sale.

### Investor-side

- **Investor consultation booked** → mention AL-licence check
  service if they intend STR.
- **STR-intending investor** → offer short-term rental
  management as a monthly service post-purchase.
- **Portfolio owner (3+ properties)** → offer portfolio review.

### Rental-side

- **Rental applicant with foreign income** → offer to explain
  fiador / rent-insurance / upfront-months options before
  viewing, so the applicant isn't shocked.
- **Corporate rental caller** → offer serviced-apartment
  furnished tier if inventory allows.

### Universal

- **Any lead** → offer to send shortlist by SMS or WhatsApp
  (Ribeira operates WhatsApp per FAQ). Non-pushy way to keep
  them warm.
- **Any high-friction / regulatory caller** → offer the
  preferred immigration lawyer, tax accountant, or fiscal
  representative referral. Disclose it's a partner network,
  not a paid endorsement.

## 8. Sources

### Direct sources (used to populate this playbook)

- **Ribeira Prime fixture:**
  `sample-data/real-estate/business.json` — canonical tenant
  shape, GDPR structure, service catalog, FAQ, persona.
- **Real-estate tool definitions:**
  `packages/integrations/real_estate_tools.py` — actual tool
  contract wired into the brain, including lead-scoring bands.
- **Demo script:**
  `docs/DEMO-SCRIPT-REAL-ESTATE-2026-08-25.md` — 7 flow
  transcripts including 4 job-brief edge cases.
- **Job application text:**
  `docs/APPLICATION-REAL-ESTATE-2026-08-25.md` — customer-
  visible framing of GDPR + turn-taking + integration story.

### Gaps to close (still needs a real interview)

- **TODO: real Ribeira Prime / EU broker interview.** All EU
  legal / tax / regulatory content in §6 is marked
  [VERIFY WITH LOCAL COUNSEL] and needs a Portuguese real-
  estate lawyer + broker to validate before ANY customer sees
  it in production copy. Do not use this playbook as legal
  reference.
- **TODO: real EU real-estate call transcripts.**
  `docs/transcripts/` currently has clinic-only. Real caller
  data would compress the gap between fixture and reality
  fast.
- **TODO: US broker interview.** US §1 + §6 content is
  sketch-level. Post-NAR-settlement BRA flow specifically
  needs a currently-practicing US buyer's agent.
- **TODO: NAR code of ethics reference for the US US-specific
  playbook subsection.**
- **TODO: EU AI Act Article 50 disclosure text** as of the
  2 Aug 2026 in-force date — verify the exact wording
  required. Fixture greeting probably meets it but should
  be legally reviewed.
- **TODO: Portugal AMI licensing detail** — reception should
  never quote licence numbers but should know what they look
  like (format, issuing authority IMPIC).
- **TODO: rental-side flows in more detail** — fixture
  `qualify_rental_lead` is thin; real long-term rental
  qualification in PT/ES is more involved (fiador docs,
  seguro de renda, prior-landlord reference format).

### External references (not yet fetched)

- Portugal IMPIC — brokerage licensing rules
- Portugal RJALA / Decreto-Lei 128/2014 (AL licensing)
- Portugal Lei 27/2016 (housing anti-discrimination)
- Spain Ley 12/2023 (housing anti-discrimination)
- France Code de la Construction (housing)
- EU GDPR RGPD full text + national implementations
- EU AI Act Regulation 2024/1689 Article 50
- NAR (US) Code of Ethics + Standards of Practice
- US HUD Fair Housing complaint procedure

## Product gaps flagged for engineering

Concrete deficiencies in the current codebase (as of 2026-08-30,
`packages/integrations/real_estate_tools.py`) revealed by walking
the playbook against real EU-market use. Pass to engineering as
spec candidates.

1. **`nif_status` gate on `book_viewing` for non-resident
   buyers.** `qualify_buyer_lead` captures `has_portuguese_nif`
   but `book_viewing` doesn't check it. A non-resident who
   books a viewing, falls in love, wants to offer, and has no
   NIF stalls 1-4 weeks. Fix: policy layer that surfaces "just
   so you know, you'll need a NIF before we can process an
   offer — want us to help set that up?" on any non-resident
   viewing booking. Same shape for Spain NIE.

2. **`currency_preference` field on buyer / investor lead.**
   Currently every quote is implicitly EUR. High-value non-
   resident buyers often think in GBP / USD / CHF / BRL and
   agree to a price that later parses to a different absolute
   figure. Add `currency_preference` (ISO 4217) to
   `qualify_buyer_lead` + `qualify_investor_lead` schemas and
   include it in the CRM sink so the assigned agent can frame
   follow-up correctly.

3. **AL-licence status as a property-side field on
   `book_viewing`.** Investors ask "does this have AL" and the
   receptionist has no source of truth in the tool contract.
   Add `property_has_al_licence: bool | "unknown"` to
   `book_viewing` schema (defaults unknown) and route to
   `qualify_investor_lead` + FAQ if the caller flagged STR
   intent but the property is AL-restricted. Ties to the
   Ribeira `alojamento local` FAQ topic.

4. **VAT-inclusive vs VAT-exclusive tagging on every commission
   / fee response.** `lookup_faq(topic="commission")` returns
   the fixture string "five percent plus VAT" (safe). But any
   downstream response synthesis that mentions commission
   MUST retain the "plus VAT / IVA" tag. Add a policy check
   in the response synthesizer that flags any bare commission
   percentage without the VAT frame.

5. **`working_with_other_agent` capture across CRM sinks.**
   Fixture has the boolean on `book_viewing` but downstream
   HubSpot sink doesn't have a matching custom field
   documented. Confirm the field lands in HubSpot on
   `HUBSPOT_CONTACT_UPSERT` so the listing agent knows before
   the viewing that the caller is represented, avoiding
   commission-dispute risk.

6. **Bonus (imported from clinic):
   `caller_name_phonetic[]` keyed by phone.** Portuguese +
   French + German names get mangled by STT and the
   correction currently sticks only to one booking's `notes`.
   Same tenant-level phonetic dictionary the clinic playbook
   flagged.
