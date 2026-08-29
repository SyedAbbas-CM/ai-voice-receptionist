# Restaurant — product playbook

Domain-knowledge reference for the product-lead agent when reviewing
receptionist features for restaurant tenants.

Status: **STUB — needs research + client interview before production
use**. Filling in requires either a real restaurant client
conversation or industry-standard receptionist-handbook research.
The product-lead agent SHOULD flag missing sections when invoked
against this vertical.

Last updated: 2026-08-29 (initial stub) by voice-agent session.

## 1. Business shape

- **Staffing:** owner-operator OR GM + host/hostess + servers + kitchen. Reception line usually rings to host stand or a shared cordless.
- **Hours:** varies wildly — brunch (weekend AM only), lunch, dinner service, late-night. Multiple day-parts on same day.
- **Physical footprint:** single vs multi-location group. Chain vs independent behaves very differently.
- **Payment:** at-table card + cash still common. Deposit for large parties. Prepay for tasting menus / private events.
- **Regulatory:** none universal. Alcohol licensing (state-specific), health inspection posting, allergen disclosures where required.

## 2. Real caller archetypes

- **Reservation booking** — party size, date, time, seating preference (booth/patio/window/bar).
- **Reservation modify / cancel** — has existing reservation, needs to change.
- **Menu / ingredient question** — allergies (gluten, nut, dairy, shellfish), dietary (vegan, halal, kosher), specific dish availability today.
- **Private event / large party** — 8+ people, private room, buyout, catering off-site.
- **Directions / parking / accessibility** — logistical only, no booking intent.
- **Order-ahead / takeout** — food-ordering, not reservation. Different flow (menu items, quantities, pickup time, payment).
- **Delivery** — third-party (DoorDash/UberEats) OR house delivery. Usually redirect to app.
- **Gift card purchase** — increasingly phone-based, needs payment.
- **Job inquiry** — "are you hiring?" — pass to manager or FAQ.
- **Complaint** — service or food quality. Escalate to manager, never resolve on phone as an AI.

## 3. Full service catalog

To fill in per-restaurant tenant. General shape:

- **Reservations**
  - 2-top, 4-top, 6-top, 8+ = "large party" flow
  - Seating: booth, banquette, patio, window, bar, chef's counter
  - Time slots: peak-hour (7-9pm) usually full; agent should offer 5:30 or 9:30
- **Special occasions**
  - Birthday (cake / candle service)
  - Anniversary
  - Business meal (quieter table, no music)
- **Private events**
  - Semi-private (curtained section)
  - Full private room
  - Full buyout (restaurant-wide, off-hours)
- **Takeout / order-ahead**
  - Menu items with modifiers
  - Pickup time slot
- **Gift cards**
  - Amount, recipient info, delivery method

## 4. Ambiguous requests → clarification

- **"A table for 4"** → date? time? tonight or specific date? Any seating preference?
- **"Do you have anything vegan?"** → clarify if they want to book AROUND this question, or just checking. If booking, note the dietary flag on the reservation for the kitchen.
- **"Can I book for a birthday?"** → confirm date, party size, cake service? candle? private area?
- **"Do you deliver?"** → in-house vs third-party; redirect to app if third-party.
- **"Are you open?"** → for lunch? dinner? today? Now? Different day-parts.

## 5. Real failure modes

- **Overbooked a peak slot** — didn't check availability before confirming.
- **Missed the "large party" upshift** — booked 8 people as a regular reservation, kitchen isn't warned.
- **Missed the dietary flag** — didn't put allergy note on the reservation, kitchen doesn't know.
- **Wrong day-part** — booked lunch reservation for a restaurant that only does dinner.
- **Didn't take a deposit** — private event booked without deposit collected, no-show cost the restaurant $2k.
- **Confirmed a special-request accommodation the restaurant can't actually do** — "we can absolutely do a nut-free kitchen" — but the kitchen has nuts everywhere.
- **Missed cancellation window** — didn't warn caller they're inside the 24hr cancellation window and will be charged.
- **Delivered a diner to a location that changed** — restaurant moved, agent didn't know.

## 6. Regulatory + safety

- Allergen questions — refer to actual kitchen policy, don't guess. If unclear, offer to have manager call back.
- Alcohol availability — some jurisdictions require patrons to be present + ID'd before confirming.
- Health / illness — anyone calling asking about a suspected food-borne illness → escalate to manager immediately, never dismiss.

## 7. Cross-sell / upsell opportunities

- Special occasion detected → offer cake / champagne / chef's tasting menu.
- Large party → offer private room upgrade.
- First-time caller → offer email signup for events / prix-fixe nights.
- Peak-time asked → suggest early or late slot with a small perk (complimentary appetizer).

## 8. Sources

- **TODO:** real restaurant transcripts (not in project yet)
- **TODO:** restaurant-industry receptionist handbook / OpenTable operator training material
- **TODO:** interview with a client restaurant when we sign one
