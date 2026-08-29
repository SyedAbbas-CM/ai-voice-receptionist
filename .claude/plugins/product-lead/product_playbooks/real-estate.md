# Real estate — product playbook

Domain-knowledge reference for the product-lead agent when reviewing
receptionist features for real-estate brokerage / property-management
tenants.

Status: **PARTIAL — clinic-quality after real client validation**.
Some content inferred from generic real-estate operations knowledge
plus the Ribeira Prime demo package (docs/ context). Filling in
production-grade requires a real broker or agent interview.

Last updated: 2026-08-29 by voice-agent session.

## 1. Business shape

- **Staffing:** broker/owner + 5-30 agents. Admin / office manager fields inbound calls when agents are showing property. Independent agent brokerages have every agent on their own line.
- **Hours:** weekends are peak (showings), weekday evenings second. Agents work non-standard hours.
- **Physical footprint:** office + agents-in-the-field. Reception often at the office, agent-in-car needs mobile forwarding.
- **Payment:** commission, not per-transaction. Reception rarely handles money except reservation fees for viewings in some markets (Portugal, France common).
- **Regulatory:** licensing state-by-state (US) or nationally (EU). Fair Housing Act (US) — cannot ask about protected classes when qualifying. GDPR (EU) — data-handling stricter than US.

## 2. Real caller archetypes

- **Property viewing request** — has a listing URL / reference, wants to book a showing. Needs: property_ref, agent availability, buyer qualification snapshot.
- **Listing inquiry** — asking about a listing but not ready to view. Needs price / availability / basic property info. Should nudge toward viewing.
- **Rental inquiry** — different flow (tenant screening, income/employment questions, rental start date, pet policy).
- **Valuation request** — homeowner wondering what their property is worth. Free consult for lead generation.
- **Buyer registration** — actively looking, wants an agent assigned + email alerts for matching listings.
- **Selling inquiry** — wants to list. Needs listing appointment with an agent, market analysis, timeline.
- **Neighborhood question** — "what's the school district" / "any noise complaints" — factual, redirect to city website / agent if listing-specific.
- **Existing client callback** — already working with an agent, needs to reach that specific agent.
- **Legal / compliance** — landlord-tenant disputes, HOA questions. Escalate to office manager or refer to attorney.

## 3. Full service catalog

Real-estate tenant menu at receptionist granularity:

- **Buyer services**
  - Property viewing (single property) — 45min
  - Portfolio tour (multi-property, same day) — 3-4 hours
  - Virtual tour (video call walkthrough) — 30min
  - Buyer consultation (goals, budget, area) — 60min
- **Seller services**
  - Listing appointment (property visit + market analysis) — 90min
  - Home valuation (comparative market analysis, no visit) — 30min consult
  - Pre-listing walkthrough / staging consult — 45min
- **Rental services**
  - Rental viewing — 30min
  - Rental inquiry / application info — 15min consult
- **Investment**
  - Investment property consult (yield, cap rate, area) — 60min
- **General**
  - Neighborhood tour (buyer relocating) — 90min
  - Broker introduction call — 15min

## 4. Ambiguous requests → clarification

- **"I want to see a property"** → which listing? URL or reference number? Any specific day/time?
- **"Do you have anything in X neighborhood"** → price range, bedrooms, timeline, buy vs rent?
- **"What's my house worth"** → for selling now, or curious? Free valuation vs paid appraisal?
- **"I want to rent"** → which listing, or open search? Budget, area, move-in date, pets, income requirement acknowledgment?
- **"I'm relocating"** → from where, timeline, remote or coming to look, needs airport pickup, temp housing option?

## 5. Real failure modes

- **Booked showing without checking agent calendar** — showing conflicts with agent's other appointment.
- **Didn't qualify buyer before showing high-end property** — agent shows $2M house to unqualified buyer, wasted 3 hours.
- **Missed the fair-housing trip-up** — asked about family status / religion / origin during qualification (illegal in US under Fair Housing Act).
- **Wrong agent** — routed a listing inquiry to a buyer's agent, or vice versa. Some agents ONLY do buy-side or ONLY do sell-side.
- **Missed the rental application question** — didn't warn about income multiple (3x rent common) / credit check / deposit before the applicant applied.
- **GDPR slip** — collected personal data over an EU call without disclosing purpose + retention.
- **No-show without deposit** — booked a viewing that the buyer never showed for; agent lost billable time.
- **Ghost lead** — leads collected but never fed into the CRM, agents never contacted.

## 6. Regulatory + safety

- **Fair Housing (US):** NEVER ask about race, color, national origin, religion, sex, familial status, disability, or associations with any of these. Steering (recommending neighborhoods based on protected class) is a violation.
- **GDPR (EU):** personal data collection needs a purpose disclosure at call open. Right-to-erasure applies to CRM records.
- **Licensing:** agent must be licensed in the state/country of the property. Cross-border referrals to a licensed agent, not direct handling.
- **AI disclosure:** where required by law (US: some states; EU: AI Act Article 50 as of Aug 2026).
- **Never quote:** legal terms of a purchase contract, tax implications, mortgage rates. Refer to attorney / accountant / lender.

## 7. Cross-sell / upsell opportunities

- Viewing booked → offer virtual tour first if remote buyer to save travel.
- Rental inquiry → offer buyer consultation if the price fits their buying budget (rent vs buy math).
- Valuation caller → offer full listing appointment.
- Investment interest → mention agent's investment specialists.
- Relocation → mention preferred lender / mover / attorney partnerships (with proper disclosures).

## 8. Sources

- **TODO:** real broker call transcripts
- **TODO:** Ribeira Prime interview (Portugal EU vertical demo target)
- **TODO:** NAR (US National Association of Realtors) code-of-ethics reference
- **TODO:** EU AI Act Article 50 disclosure text
