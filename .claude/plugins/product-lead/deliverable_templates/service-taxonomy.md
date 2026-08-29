# Service taxonomy — {vertical}

Author: product-lead (subagent)
Vertical: {vertical}
Date: {yyyy-mm-dd}

## Method

Extracted the full service catalog from:
- Playbook Section 3 (`.claude/plugins/product-lead/product_playbooks/{vertical}.md`)
- Fixture data: `sample-data/{vertical}/business.json`
- Any real-tenant business.json in `tenants/` (if present)

Reconciled differences between playbook (canonical) and fixture
(what our code sees) — gaps noted below.

## Taxonomy tree

- **{Category 1}**
  - **{Service 1a}**
    - Duration: X min
    - Price: $Y (or range, or "insurance-dependent")
    - Provider(s): specific staff role(s)
    - Required-info-before-booking: {list of slots the receptionist MUST collect FIRST}
    - Cross-sell hooks: {upsells a real receptionist would raise}
    - Ambiguity notes: caller phrases that map here + phrases that DON'T (route elsewhere)
  - **{Service 1b}**
    - [repeat...]
- **{Category 2}**
  - [repeat...]

## Fixture reconciliation

| Playbook says | Fixture has | Gap |
|---|---|---|
| Service X | Present | ✓ |
| Service Y | Present under different name | Rename fixture OR add alias |
| Service Z | Missing | Add to fixture |
| — | Service W (fixture only) | Playbook doesn't mention — is this real or wrong? |

## Required-info slot matrix

Slots the agent MUST collect before calling `book_appointment`
per service. Rows = services. Cols = slots. Cell = required/optional/N/A.

|  | phone | name | date | time | service_subtype | provider | insurance | referral | ... |
|---|---|---|---|---|---|---|---|---|---|
| Service 1a | REQ | REQ | REQ | REQ | REQ | OPT | OPT | N/A | ... |
| ... |  |  |  |  |  |  |  |  | ... |

## Ambiguous-request catalog

Caller phrasings → which services they COULD mean → what to ask.

| Caller says | Possible services | Clarify by asking |
|---|---|---|
| "a follow-up" | Follow-up A / B / C | "follow-up to what? which doctor? when was original visit?" |
| ... |  |  |

## Recommendations for engineering

- `packages/integrations/service_aliases.py` needs entries for every ambiguous phrase above.
- Fixture data at `sample-data/{vertical}/business.json` needs the gap items added.
- `NextActionPolicy` should NOT decide ASK_SLOT(phone) until required-info slots for the RESOLVED service are collected. (Right now it may ask for phone before knowing what service, then have to re-ask about service-specific info later — bad UX.)
- Cross-sell hooks belong in a new `packages/dialogue/cross_sell.py` (doesn't exist yet — this doc is where you catch that).
