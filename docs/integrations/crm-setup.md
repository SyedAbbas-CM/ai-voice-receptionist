# CRM setup — how to find your API token for each provider

**Purpose:** you have accounts in multiple CRMs. This doc tells you the exact click path in each CRM's UI to find the API token I need to wire the sink.

**When you have a token, drop it in this chat.** I'll add it to the box's `.env` + turn on the sink + verify with a test booking.

**All 4 CRM sink classes already exist in `packages/integrations/sinks.py`** — GHLSink, HubSpotSink, PipedriveSink, SheetsSink. They're stubs waiting on credentials. Once tokens land, wiring is env-var config + one restart.

---

## 1. GoHighLevel (GHL) — priority 1

**Two auth modes.** Location API key is fastest for demo; Marketplace App is proper for multi-client.

### Fastest — Location API key (1 tenant, 5 min)

1. Log in to your GHL agency/subaccount at https://app.gohighlevel.com
2. Switch into the sub-account you want to wire (top-left location switcher)
3. Left sidebar → **Settings** (gear icon at bottom)
4. Under Settings, click **Business Profile**
5. Scroll to the **API Key** section
6. Click **Show** / **Generate API Key**
7. Copy the key (starts with `eyJ...` — it's a JWT)

Paste that here. Format: `GHL_LOCATION_API_KEY=eyJ...`

Also grab your **Location ID** while you're there:
- Same Settings page → **Business Info** → **Location ID** field
- Format: `GHL_LOCATION_ID=<10-char alphanumeric>`

### Proper — Marketplace App (multiple tenants, ~30 min setup)

Only do this when we're onboarding a real agency client. For demo, skip.

1. Register at https://marketplace.gohighlevel.com
2. Create a Public App (or Private if for one agency)
3. Configure OAuth redirect URL: `https://agent.eternalconquests.com/integrations/ghl/oauth/callback`
4. Scopes to enable: `contacts.write`, `contacts.readonly`, `calendars.write`, `calendars.readonly`, `conversations.write`, `opportunities.write`, `locations.readonly`
5. After approval, copy `Client ID` + `Client Secret`

Paste: `GHL_CLIENT_ID=...` and `GHL_CLIENT_SECRET=...`

---

## 2. HubSpot — priority 2

**Two auth modes.** Private App is fastest for demo; OAuth is for marketplace listing.

### Fastest — Private App token (1 workspace, 5 min)

1. Log in to https://app.hubspot.com
2. Top-right gear icon (**Settings**)
3. Left sidebar → **Integrations** → **Private Apps**
4. Click **Create a private app** (upper right)
5. **Basic Info** tab → name it "Receptionist Agent"
6. **Scopes** tab → check these:
   - `crm.objects.contacts.write`
   - `crm.objects.contacts.read`
   - `crm.objects.deals.write`
   - `crm.schemas.contacts.read`
   - `sales-email-read` (optional, for context)
   - `tickets` (optional, if we want to escalate)
7. **Create app** button (top right)
8. Confirmation modal shows the **access token** — starts with `pat-na1-...` (or `pat-eu1-...`)
9. Copy it — it's shown ONLY ONCE

Paste: `HUBSPOT_PRIVATE_APP_TOKEN=pat-na1-...`

### OAuth (for HubSpot marketplace)

Only if we're building a listable HubSpot app. Skip for now.

---

## 3. Airtable — priority 3

Simplest of the four. Personal Access Token model.

1. Log in to https://airtable.com
2. Top-right profile menu → **Developer hub**
3. Left sidebar → **Personal access tokens**
4. **Create token**
5. Name it "Receptionist Agent"
6. **Scopes** → check these:
   - `data.records:read`
   - `data.records:write`
   - `schema.bases:read`
7. **Access** → add the specific base(s) that hold your contacts/leads. Leave empty to grant access to none (you can add per-tenant later).
8. **Create token** — copy the value (starts with `pat...`)

Paste: `AIRTABLE_PERSONAL_ACCESS_TOKEN=pat...`

Also need per-tenant:
- **Base ID** — open the base in browser, URL shows `airtable.com/appXXXXXXXXXX/...` — the `appXXX` part is the Base ID
- **Table name** — the tab name where contacts live (or leave to default `Contacts`)

Format:
```
AIRTABLE_BASE_ID=appXXXXXXXXXX
AIRTABLE_TABLE_NAME=Contacts
```

---

## 4. Pipedrive — priority 4

Simplest CRM API — just one token + your company subdomain.

1. Log in to https://<yourcompany>.pipedrive.com
2. Top-right avatar → **Personal preferences**
3. Left sidebar → **API**
4. Click **Generate API key** if none exists
5. Copy the token (32-char hex string)

Paste both:
```
PIPEDRIVE_API_TOKEN=<32-char hex>
PIPEDRIVE_COMPANY_DOMAIN=<yourcompany>  # the subdomain before .pipedrive.com
```

---

## What I do after you paste each set

1. `ssh` into Lightsail box
2. Add env vars to `/home/ubuntu/receptionist-agent/.env`
3. Restart via `./scripts/deploy.sh --skip-rsync` (~10s downtime)
4. Verify sink wired via a health-check log line at startup
5. Test end-to-end: I place a fake booking through the sink chain + confirm the CRM shows the contact

## What per-tenant credentials look like eventually

Right now: process-wide env vars (one CRM account per stack). Fine for demo, breaks at multi-tenant.

Post-B-P0.0 (voice-agent's task #83): `crm_credentials` table stores per-tenant encrypted (Fernet) tokens. Each tenant's sink resolves creds at request time from `TenantRuntimeContextResolver`. This is future work — env vars are the demo path.

## Anti-fake-key checks I'll run before pasting to the box

- GHL token: verify it's a valid JWT + decodes to a location scope
- HubSpot: hit `GET /crm/v3/objects/contacts?limit=1` — should return 200
- Airtable: hit `GET /v0/meta/bases/{id}` — should return 200
- Pipedrive: hit `GET /v1/users/me?api_token={token}` — should return 200

Any 401/403 → I tell you the token doesn't work + no `.env` change happens. Fail-loud, no silent-broken sinks.

## Also — bonus CRMs I skipped

From the Jobs.txt analysis, these were mentioned 1-2 times. Not building sinks unless a real deal asks:
- Zoho, Salesforce, Monday, Notion, Close, Klaviyo, ActiveCampaign, Attio, Folk, Copper, Streak
- Vertical-specific: Vagaro (3 mentions), Nextech (1), Cliniko (1), Jane (1), SimplePractice (1), Halaxy (1), Nookal (1), AppFolio (1)
- Support: Zendesk (2), Intercom (1), Freshdesk (1), Help Scout (1)

Wait for a specific deal → build the adapter → charge for the integration.
