# GoHighLevel setup

We use a **Private Integration** token — the simplest auth for a self-hosted receptionist serving one GHL sub-account. No OAuth callback, no marketplace app.

## Get a token

1. Log into the GHL sub-account (Location).
2. **Settings → Private Integrations → Create New Integration**.
3. Name it "voiceops-ai-agent".
4. Grant these scopes:
   - `contacts.write`, `contacts.readonly`
   - `opportunities.write`
   - `calendars.readonly`, `calendars/events.write`
   - `conversations/message.write` (optional, if you want SMS follow-up)
5. Copy the token. It starts with `pit-`.

## Find your Location ID

**Settings → Company → API Key section** or the URL path `.../v2/location/<location_id>/...`.

## Find your Calendar ID (optional)

**Settings → Calendars →** click the calendar → copy the ID from the URL.

## Configure

```
GHL_API_TOKEN=pit-...
GHL_LOCATION_ID=<location_id>
GHL_CALENDAR_ID=<calendar_id>   # optional
CRM_SINK=ghl
```

## What the sink does

**On every successful `book_appointment` tool call:**
- Upserts the contact by phone (creates if new, updates if exists).
- Adds a note to the contact with the booking details.
- If `GHL_CALENDAR_ID` is set, creates a matching GHL calendar event.

**On call end:**
- Upserts the contact by phone.
- Adds a summary note (intent, urgency, lead score, transcript summary).

## Which calendar owns bookings?

Two options:

- **Fake / Google calendar as source of truth, GHL as log**: keep `CALENDAR_BACKEND=fake` (or `google`). The brain calls `check_availability` against the local calendar. The GHL sink mirrors the booking as a note + event afterward. Best for hybrid clients.
- **GHL as source of truth**: not yet wired. If you need this, tell me and I'll add a GHLCalendar backend that queries free-slots via GHL's API directly.

## Cost

Free with any GHL sub-account subscription. GHL itself starts at $97/mo (Starter). No per-request charge from GHL for standard API usage under normal call volumes.
