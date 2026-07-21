# n8n workflow archive

These four workflows are original client work owned by us and preserved here as reference. All hardcoded IDs and credentials have been replaced with `<PLACEHOLDER>` tokens so this repo is safe to share.

If you want to actually run one, re-import into n8n and swap the placeholders for real values via **n8n credentials** (never paste API keys into HTTP node bodies).

## Files

| File | Client | Purpose | Status |
|---|---|---|---|
| `subtodealz-outbound.json` | **SubtoDealz** (real estate) | Manual Vapi outbound dialer over a Google Sheet of rental leads. Filters by Florida business hours, dials via Vapi, waits 8 min, GPT-4.1 classifies HOT/COLD/etc, writes back to sheet. | Being ported to Python — see Product A plan below |
| `subtodealz-vapi-assistant-prompt.md` | SubtoDealz | The full Vapi assistant system prompt for "Alex" — persona, conversation flow, objection handling, disposition tags. | Ported into `sample-data/subtodealz/business.json` |
| `ironclad-post-call-router.json` | **IronClad Family** (family safety products / advisor tools) | ElevenLabs post-call fan-out: regex + GPT-4.1 classify segment (SHOP / B2B / B2C / SUPPORT), then route to Google Calendar + Twilio SMS + Outlook + Freshdesk + HubSpot Forms. Also exposes `/calendar-check` for mid-call slot lookups. | Reference — patterns being stolen |
| `vivarays-notion-ingestion.json` | **VivaRays** (circadian coaching) | Batch RAG loader: 7 Notion DBs → GPT-4.1-mini normalizer → OpenAI embeddings → Supabase pgvector. Includes a classifier-router that splits Think Tank content into scoped tables. | Reference — patterns being stolen for `packages/rag/` |
| `vivarays-content-generator.json` | VivaRays | Chat-triggered content factory. User types "day 20" → Supabase hybrid pgvector+BM25 search → 3-stage voice-lock LLM chain → Roudy-voice coaching script. | Reference — voice-rewriter pattern being stolen |

## Placeholder tokens used

Everywhere you see `<...>` in these JSONs, that's a scrubbed value. Full list:

| Token | Was | Where to configure |
|---|---|---|
| `<VAPI_API_KEY>` | Live Vapi bearer (rotated / disabled) | Vapi dashboard → API keys |
| `<VAPI_ASSISTANT_ID>` | SubtoDealz assistant ID | Vapi dashboard → Assistants |
| `<VAPI_PHONE_NUMBER_ID>` | SubtoDealz phone number ID | Vapi dashboard → Phone numbers |
| `<GOOGLE_SHEET_ID>` | Carson sheet ID | Google Drive URL of the sheet |
| `<GOOGLE_DOC_KNOWLEDGE_BASE>` | VivaRays knowledge base doc | Google Drive URL |
| `<GOOGLE_CALENDAR_ID>` | IronClad primary calendar | Usually `primary` or `<email>@group.calendar.google.com` |
| `<NOTION_DB_*>` | 7 VivaRays Notion database UUIDs | Notion → Share → Copy link |
| `<SUPABASE_PROJECT>` | VivaRays Supabase subdomain | Supabase project settings |
| `<HUBSPOT_PORTAL_ID>` + `<HUBSPOT_FORM_*>` | IronClad marketing forms | HubSpot → Marketing → Forms |
| `<TWILIO_FROM_NUMBER>` | IronClad SMS sending number | Twilio → Phone Numbers |
| `<ELEVENLABS_WEBHOOK_SECRET>` | IronClad ElevenLabs post-call signing key | ElevenLabs → Convai → Webhook settings |
| `<FRESHDESK_DOMAIN>` | IronClad Freshdesk subdomain | e.g. `ironcladfamily.freshdesk.com` |
| `<SUPPORT_EMAIL>` / `<ADMIN_EMAIL>` | Team notification addresses | Any mailbox |
| `<CRED_*>` | n8n internal credential IDs | Recreate credentials in your own n8n instance |
| `<WEBHOOK_PATH_*>` | n8n webhook path UUIDs | Any UUID works after re-import |

Full scrubbing report: [`SCRUB_REPORT.md`](./SCRUB_REPORT.md).

## Product A: porting SubtoDealz to the FastAPI repo

The `subtodealz-outbound.json` graph is being replaced by a clean set of Python modules. Mapping:

| n8n node | Python equivalent |
|---|---|
| `test` (Google Sheets read) | `packages/integrations/google_sheets.py::list_rows` |
| `Complete Filtration` (JS filter) | `packages/integrations/dialer_policy.py::decide_can_call` |
| `Loop Over Leads` (SplitInBatches) | `apps/api/app/routes/outbound.py::start_batch` background task |
| `VAPI - Make Call3` (HTTP POST) | `packages/integrations/vapi_client.py::dispatch_call` |
| `Wait5` (8-min blocking wait) | **removed** — replaced by `/vapi/events` webhook |
| `VAPI - Get Call Result1` | **removed** — transcript arrives in end-of-call webhook |
| `OpenAI1/OpenAI3` (transcript extractor) | `packages/core_agent/classifiers/transcript_extractor.py` |
| `OpenAI` (lead classifier) | `packages/core_agent/classifiers/lead_classifier.py` |
| `test1/test2/test3/test4` (4 duplicate sheet updates) | `packages/integrations/google_sheets.py::update_by_match` |
| `Complete Filtration` cooldown/attempts | `packages/integrations/dialer_policy.py` (same module) |

Improvements the Python version gets:
- No blocking 8-minute wait → parallel dispatch, survives restarts
- No wasteful GPT-call-that-returns-literal-`NO_ANSWER` branch
- Adds DNC list check the n8n graph forgot
- Adds legal-safe fallback (unit-tested)
- One `update_by_match` method instead of four duplicate sheet nodes
- Tenant-configurable (not hardcoded to Florida ET)

## Product B & C: patterns stolen, workflows kept as reference

The IronClad and VivaRays workflows are not being rebuilt in Python. Instead, specific patterns from them are being extracted into the repo:

**From IronClad Family (`ironclad-post-call-router.json`):**
- `POST /calendar/check` endpoint returning speakable slot strings
- Guard-LLM (yes/no confirmation) before destructive external writes
- Two-stage classify: cheap regex first, LLM refines only ambiguous cases
- User-only transcript filtering (avoid brand-name false positives)
- Business-hours coercion (silent roll to next weekday)
- Email normalization ("jane at gmail dot com" → "jane@gmail.com")

**From VivaRays (`vivarays-notion-ingestion.json` + `vivarays-content-generator.json`):**
- `packages/rag/ingest.py` — LLM-as-normalizer for messy CMS data
- `packages/rag/retrieve.py` — hybrid pgvector + BM25 retrieval
- `packages/core_agent/voice_rewriter.py` — 3-stage voice-separation chain
- `templates/{vertical}/brand_voice.yaml` — approved-stories / banned-phrases / litmus-test structure
- Priority-ordered classifier prompt template

See `docs/learning/03-repo-mental-model.md` for how these fit into the overall architecture.
