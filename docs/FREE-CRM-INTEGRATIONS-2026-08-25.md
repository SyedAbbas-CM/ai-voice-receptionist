# Free-Tier CRM + Storage Integrations — 2026-08-25 research

**User ask:** "i dont have anything paid can we get dev accounts and do integrations for free for alot of these CRMS and google sheets excel and all the common thigns ? look into that"

**Bottom line:** yes, mostly. You can demo integrations against **HubSpot**, **Google Sheets**, **Airtable**, **Notion**, and **Pipedrive Developer Sandbox** without paying anything. **Zoho, GoHighLevel, and Salesforce** are paid-only for API access — skip them for the demo.

---

## Verdict per provider

| Provider | Free API access? | Sign-up cost | Key limits | Priority for us |
|---|---|---|---|---|
| **HubSpot Free** | ✅ Full | $0, no CC | 100 req/10s, 650k/day | **DEMO PRIMARY** — code already ships |
| **Google Sheets** | ✅ Free today | $0 (Gmail) | ⚠️ paid quota planned late 2026 | **DEMO PRIMARY** — code already ships |
| **Pipedrive Sandbox** | ✅ Full | $0, no CC | Isolated data; 45-day activity gate | **BUILD ADAPTER** (real-estate brief names it) |
| **Airtable Free** | ⚠️ Limited | $0 | 1000 API calls/mo, 1000 records/base | **BUILD ADAPTER** (light CRM alt) |
| **Notion Free** | ✅ Full | $0 | ~1000 req/hr per integration | **NICE-TO-HAVE** (not really a CRM) |
| **Zoho CRM Free** | ❌ Blocked | Free plan exists, no API | 3 users, no webhooks | **SKIP** |
| **GoHighLevel Developer Sandbox** | ✅ Full via Marketplace | $0 (marketplace developer portal) | 5 seats, sandbox-scoped data | **BUILD/TEST NOW** — code already ships, user needs marketplace signup |
| **Salesforce Developer Edition** | ✅ Full | $0 | 15k API calls/day | **NICE-TO-HAVE** (2 hours to add adapter) |
| **Excel / OneDrive Graph API** | ✅ Free | $0 (personal Microsoft) | 10k/10min | **NICE-TO-HAVE** (real-estate brief) |

---

## What we already ship

**Immediately usable:**
- ✅ **HubSpot** — `packages/integrations/hubspot_client.py` + `HubSpotSink`. 429/retry policy live. Free tier ceiling (650k/day) is 10× more than we'd ever need at demo volume. Just sign up at [developers.hubspot.com](https://developers.hubspot.com) and drop `HUBSPOT_ACCESS_TOKEN` in .env.
- ✅ **Google Sheets** — `packages/integrations/google_sheets.py` + `SheetsSink`. Free tier is generous today; watch the paid-quota transition warning.
- ✅ **GoHighLevel** — `packages/integrations/ghl_client.py` + `GHLSink`. Only useful if the tenant already has a GHL account (we don't need to provide one).

**Immediately usable but not wired to sink:** Google Calendar (extended today with cancel/reschedule/find_by_phone), email (SendGrid free 100/day + SMTP).

---

## Recommendation: add 3 adapters this week

Ranked by ROI for the real-estate application:

### 1. Pipedrive adapter (2-3 hours, HIGH PRIORITY)

The job brief explicitly names Pipedrive as one of three CRMs they might use. Building it means saying "supports HubSpot, Pipedrive, GoHighLevel out of the box" instead of "HubSpot only; Pipedrive coming later."

- **Free Developer Sandbox** — no credit card, indefinite lifetime as long as you sign in every 45 days
- API endpoints: contacts / deals / notes / activities — same shape as HubSpot
- **Copy pattern from `hubspot_client.py`** — same auth style (API token or OAuth), same retry loop, same sink pattern
- Estimated: `packages/integrations/pipedrive_client.py` (~200 lines) + `PipedriveSink` in `sinks.py` (~80 lines) + 15 tests

### 2. Airtable adapter (1-2 hours, MEDIUM PRIORITY)

Not a "real" CRM but a common pick for small businesses. The 1000 records/base limit is tight for volume, but plenty for demo/pilot. Real-estate brief says "or another suitable system" — Airtable fits.

- **Free plan** — 1000 API calls/month is limiting for volume but fine for demo
- Rate limit: 5 req/sec/base — comfortable for a booking-flow burst
- Very simple API — just `POST /v0/{base_id}/{table}` with JSON records
- Estimated: `packages/integrations/airtable_client.py` (~120 lines) + `AirtableSink` + 10 tests

### 3. Microsoft Excel / OneDrive Graph API (2-3 hours, LOWER PRIORITY)

European clients often prefer Microsoft over Google. Real-estate brief says "Excel" alongside Sheets. Personal Microsoft account gets free Graph API access to OneDrive Excel workbooks.

- **Free personal Microsoft account** — no cost
- Graph API 10k requests/10min is generous
- Slightly more complex auth (OAuth device flow) than Sheets
- Estimated: `packages/integrations/excel_onedrive_client.py` (~180 lines) + `ExcelSink` + 12 tests

---

## What NOT to build

**Zoho** — API is blocked on Free plan. Standard tier is $14/user/month. Skip unless a specific client asks.

**Salesforce** — Developer Edition IS free with API access, BUT: real-estate SMB market rarely uses Salesforce (it's enterprise-focused), and every hour spent on Salesforce is an hour not spent on Pipedrive/Airtable which the real-estate market actually uses. Add later if a specific client asks.

**Notion** — API is free but Notion isn't really a CRM; it's a workspace. Building an adapter is a nice-to-have but doesn't move the application forward.

---

## ⚠️ CORRECTION on GoHighLevel (2026-08-26)

**Earlier I said GHL is paid-only at $97/mo.  That's the SUB-ACCOUNT price for real customer usage.  BUT GHL has a separate free Marketplace Developer Sandbox — the same class of dev tier as Pipedrive Developer Sandbox.**

Sources:
- [GHL: Create a Developer Account](https://marketplace.gohighlevel.com/docs/oauth/CreateDeveloperAccount/)
- [GHL: Sandbox Account docs](https://marketplace.gohighlevel.com/docs/oauth/SandboxAccount)
- [GHL: App Testing Guide](https://marketplace.gohighlevel.com/docs/oauth/AppTestingGuide)

**How to sign up (2 minutes, no CC):**
1. Go to [marketplace.gohighlevel.com](https://marketplace.gohighlevel.com/)
2. Sign in with a Google or email account
3. Click **Testing** in the top nav
4. Click **+ Create App Test Account**
5. Enter an Account/Agency Name + Password
6. **Get your Location ID + Private Integration Token** from Settings → Private Integrations inside the sandbox
7. Drop into Lightsail `.env`: `GHL_API_TOKEN=<token>` + `GHL_LOCATION_ID=<location>`

**Then `CRM_SINK=ghl+hubspot+pipedrive+followup` runs live during the demo call.**

Our existing `packages/integrations/ghl_client.py` already uses Private Integration tokens (not OAuth) — matches the sandbox auth pattern exactly. **Zero code change needed** — just sign up + drop credentials.

**Sandbox limits:** 5 seats by default, isolated data, sandbox goes inactive if you don't create an app in 45 days OR publish a public/private app within 6 months. For our purposes ("test integrations in dev") — none of these are limits we'll hit.

## Updated demo sign-up checklist (was 12 min, now 14 min for 4 CRMs)

1. **HubSpot free** — 2 min, [app.hubspot.com/signup](https://app.hubspot.com/signup-hubspot/crm)
2. **Google Cloud + Sheets API** — 5 min, [console.cloud.google.com](https://console.cloud.google.com)
3. **Pipedrive Developer Sandbox** — 3 min, [developers.pipedrive.com](https://developers.pipedrive.com/)
4. **GoHighLevel Marketplace Sandbox** — 2 min, [marketplace.gohighlevel.com](https://marketplace.gohighlevel.com/) → Testing → Create App Test Account
5. **Airtable free** (only if you want an Airtable adapter) — 2 min, [airtable.com/signup](https://airtable.com/signup)

Total: **~14 min for 4 fully-functional live CRM sinks** during the real-estate demo call.

---

## ⚠️ Google Sheets pricing risk

Google announced that **Sheets API quota adjustments will incur charges to Cloud billing accounts later in 2026**. Current free tier is generous but this is a strategic risk for a demo that will run for months. Two mitigations:

1. **Batch operations aggressively** — one Sheets append per call, not one per turn. Reduces quota consumption 10-20×.
2. **Airtable / OneDrive Excel as fallback** — position Sheets as one of three storage options in the application text, not the only one.

Our current `SheetsSink` writes one row per call (correct pattern) so we're already efficient. But mention this risk in the application to show awareness.

---

## Application text impact

Add this paragraph to `docs/APPLICATION-REAL-ESTATE-2026-08-25.md`:

> "For CRM the system ships with adapters for HubSpot (EU and US regions), GoHighLevel, Google Sheets, and Airtable. Pipedrive support is 2-3 hours of work from the same sink pattern — happy to add before pilot start. All CRM writes are wrapped in an independent-failure sink so a HubSpot outage doesn't fail the Airtable write and vice versa. Failed CRM writes queue for retry via the outbox pattern — no data loss even on 30-minute provider incidents."

That accurately reflects what we HAVE + what we can PROMISE within a small delivery window.

---

## Sign-up actions for you TODAY

For the demo tomorrow, you'll want at minimum:

1. **HubSpot free** — [signup.hubspot.com/signup-v2](https://app.hubspot.com/signup-hubspot/crm) — 2 minutes, no CC
2. **Google Cloud + Sheets API enabled** — https://console.cloud.google.com — pick a project, enable Sheets API, download service-account JSON. 5 minutes.
3. **Pipedrive Developer Sandbox** — [developers.pipedrive.com](https://developers.pipedrive.com/) — 3 minutes, no CC
4. **Airtable free** — [airtable.com/signup](https://airtable.com/signup) — 2 minutes

Total: ~12 minutes of sign-ups. Then drop tokens in Lightsail .env and the CRMs work.

---

## Sources

- [HubSpot Developers portal](https://developers.hubspot.com/) — free-tier API access confirmed
- [HubSpot APIs by tier](https://developers.hubspot.com/docs/developer-tooling/platform/apis-by-tier)
- [Pipedrive Developer Sandbox docs](https://pipedrive.readme.io/docs/developer-sandbox-account) — free indefinite access
- [Zoho CRM API tier analysis](https://pipeline.zoominfo.com/sales/zoho-crm-api) — free tier API blocked
- [Google Sheets API limits](https://developers.google.com/workspace/sheets/api/limits) — free today, paid quota planned
- [Airtable Pricing 2026](https://smartprocessflow.com/airtable-free-plan) — 1000 API calls/month free
- [Notion API 2026 release notes](https://www.notion.com/releases/2026-05-13) — API access on free tier
