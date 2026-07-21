# SubtoDealz outbound dialer — end-to-end runbook

This is Product A of the three products in this repo. It replaces the SubtoDealz n8n workflow with a clean FastAPI backend. Follow this to get an end-to-end demo dialing real numbers.

**End state:** you `curl` a single endpoint, it reads a Google Sheet of rental leads, filters to business-hours + cooldown + max-attempts + DNC survivors, dispatches Vapi outbound calls with per-lead variable overrides ({{lead_name}}, {{property_address}}, {{rent_amount}}), and when each call ends, GPT-4.1 classifies HOT/COLD/etc and writes the disposition back to the sheet.

## Prerequisites

- Repo cloned, `.venv` created, deps installed (`pip install -r apps/api/requirements.txt`)
- A Vapi account with:
  - An API private key (Dashboard → API keys)
  - A phone number (Dashboard → Phone Numbers → buy one)
  - An assistant configured to use `workflows/n8n/subtodealz-vapi-assistant-prompt.md` as the system prompt, with three variables declared: `lead_name`, `property_address`, `rent_amount`
- A Google Sheet with the exact columns from the sample below
- A Google service account JSON file with Sheets read+write permission on that sheet
- An OpenAI or Groq key (LLM for the classifier)

## Sheet format

Row 1 must be the header. Column order doesn't matter, but names must match:

| Name | Phone | Property address | Rent Amount | Total Calls | Status | Last Called | Notes |
|---|---|---|---|---|---|---|---|
| Bob Owner | +15551234567 | 123 Elm St | 1500 | 0 | | | |
| Alice Owner | +15559999999 | 456 Oak Ave | 1800 | 0 | | | |

Extra columns are ignored. Missing columns cause specific fields to be dropped from writes (not a crash).

Share the sheet with your Google service account email as **Editor**.

## `.env`

```
# LLM for disposition classification
LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
# or
LLM_PROVIDER=groq
GROQ_API_KEY=gsk_...

# Vapi
VAPI_PRIVATE_KEY=vapi_pk_...
VAPI_ASSISTANT_ID=asst_...
VAPI_PHONE_NUMBER_ID=pn_...
VAPI_PUBLIC_URL=https://your-tunnel.trycloudflare.com
VAPI_SECRET=<any random string>

# Google Sheets (leads source + disposition writeback)
GOOGLE_SERVICE_ACCOUNT_JSON=/absolute/path/to/service-account.json
GOOGLE_SHEET_ID=<from the sheet's URL>

# Business profile — points the brain at the wholesaler vertical
BUSINESS_PROFILE_PATH=./sample-data/subtodealz/business.json
```

## Configure the Vapi assistant (one-time)

In the Vapi dashboard, edit the assistant that will make the calls:

1. **Model → System prompt**: paste the full text from `workflows/n8n/subtodealz-vapi-assistant-prompt.md`.
2. **Model → Variables**: declare `lead_name`, `property_address`, `rent_amount` (empty defaults are fine).
3. **Server URL**: `${VAPI_PUBLIC_URL}/vapi/events` with the secret set to `VAPI_SECRET`.
4. (Optional) **Tools**: point to `${VAPI_PUBLIC_URL}/vapi/chat/completions` if you want the brain in this repo to own conversation logic instead of Vapi's LLM.

## Boot the server

```bash
source .venv/bin/activate
cd apps/api
uvicorn app.main:app --reload --port 8000
```

Expose it to the internet:
```bash
cloudflared tunnel --url http://localhost:8000
```
Copy the tunnel URL and set `VAPI_PUBLIC_URL` before booting.

## Step 1 — dry run (verify without dialing)

```bash
curl -s -X POST http://localhost:8000/outbound/dry_run \
  -H 'Content-Type: application/json' \
  -d '{
    "business_id": "demo-subtodealz-001",
    "timezone": "America/New_York",
    "cooldown_hours": 24,
    "max_attempts": 3,
    "max_calls_per_batch": 10
  }' | python -m json.tool
```

Expected: a JSON payload with `dialable` and `skipped` arrays. `skipped[*].reason` will be one of `out_of_hours`, `cooldown`, `max_attempts`, `dnc`, `no_phone`, `terminal_status`.

**If everything is skipped with `out_of_hours`** and it's business hours where you are, remember the policy is in the callee's timezone. Set `"timezone": "America/Los_Angeles"` or whatever matches your test scenario.

## Step 2 — real batch (dials for real)

Same body, different endpoint. Start with `max_calls_per_batch: 1` for the first live test.

```bash
curl -s -X POST http://localhost:8000/outbound/start_batch \
  -H 'Content-Type: application/json' \
  -d '{
    "business_id": "demo-subtodealz-001",
    "max_calls_per_batch": 1
  }' | python -m json.tool
```

Response includes a `dispatched[*].vapi_call_id` per outbound call. Vapi calls the number in the sheet.

## Step 3 — disposition writeback

When each call ends, Vapi POSTs an `end-of-call-report` to `${VAPI_PUBLIC_URL}/vapi/events`. Our handler:

1. Looks up the outbound context by `vapi_${call_id}`
2. Runs `extract_transcript_signals` (GPT extractor: rent change, callback time, availability)
3. Runs `classify_lead` (GPT single-word: `HOT_LEAD`/`COLD_LEAD`/`PROPERTY_UNAVAILABLE`/`NO_ANSWER`/`CALLBACK_REQUESTED`)
4. Writes back to the sheet row: `Status`, `lead_status`, `Last Called`, `Total Calls`, `Notes`, `Timestamp`, and `Rent Amount` (if updated)

Check the sheet — the row should update within seconds of the call ending.

## What n8n was doing that this replaces

| n8n workflow node | This repo |
|---|---|
| `test` (Google Sheets read) | `google_sheets.list_rows` inside `/outbound/start_batch` |
| `Complete Filtration` JS | `packages/integrations/dialer_policy.decide_can_call` |
| `Loop Over Leads` | Python for-loop |
| `VAPI - Make Call3` | `packages/integrations/vapi_client.dispatch_call` |
| `Wait5` (8 min blocking) | **removed** — `/vapi/events` handles disposition |
| `VAPI - Get Call Result1` | **removed** — transcript is in the webhook body |
| `OpenAI1`/`OpenAI3` (extractor) | `packages/core_agent/classifiers/transcript_extractor.py` |
| `OpenAI` (classifier) | `packages/core_agent/classifiers/lead_classifier.py` |
| `test1/test2/test3/test4` (4 duplicate updates) | `google_sheets.update_by_row` in `disposition_handler.py` |

## Bugs fixed vs the original workflow

1. **`Total calls` casing bug** — n8n silently disabled the max-attempts guard when the column was lowercase. `lead_from_sheet_row` now tolerates any casing.
2. **Missing DNC list check** — original had no DNC. Now first-class in `DialerPolicy.dnc_numbers`.
3. **Wasteful GPT call for literal "NO_ANSWER"** — the `lead_classifier` short-circuits on empty/trivial transcripts without touching the LLM.
4. **8-minute blocking Wait** — removed. Disposition writes back within seconds of the call ending, not 8 minutes later.
5. **Four duplicate sheet update paths** — collapsed into one `update_by_row` call.
6. **Leaked API key in JSON** — `.env` reads the key at runtime, never checked in.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `500: VAPI_PRIVATE_KEY not configured` | `.env` missing key or uvicorn not restarted after edit |
| `400: assistant_id and phone_number_id are required` | Neither env nor request body specified them |
| Sheet reads work but writes silently fail | Service account is Viewer, not Editor |
| `dispatched` returns but the sheet never updates | Vapi's `serverUrl` isn't your tunnel URL, or `VAPI_SECRET` mismatch |
| All leads skipped with `out_of_hours` | The `timezone` field is wrong for your test scenario |
| Rent stays the same after a call where the owner said a new rent | The extractor prompt only trusts explicit rent statements. Try clearer test dialogue |

## Cost per call (rough)

- Vapi: ~$0.05/min inbound + ~$0.10/min outbound + telephony (~$0.008/min US)
- OpenAI GPT-4.1 for extractor + classifier: ~$0.005 per call (2 calls, ~2k input + 100 output tokens each)
- Google Sheets: free

For a 3-min call: ~$0.35 + LLM ~$0.005 = **~$0.36 per attempted call**.

For 100 attempts/day: **~$36/day** or ~$1100/month. Charge the client $2-5k setup + $500-1000/month retainer.
