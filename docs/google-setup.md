# Google Calendar + Sheets setup

Service-account auth. Simpler than OAuth for a self-hosted receptionist because there's no per-caller consent flow.

## Create a service account

1. **console.cloud.google.com → IAM & Admin → Service Accounts → Create**.
2. Name it `voiceops-receptionist`.
3. Skip the role assignment (we grant access per-resource, not project-wide).
4. Open the created SA → **Keys → Add key → Create new key → JSON**. Download the JSON.
5. Note the service-account email: `voiceops-receptionist@<project>.iam.gserviceaccount.com`.

## Enable APIs

In the same project, enable:
- Google Calendar API
- Google Sheets API

## Share resources with the service account

- **Calendar**: open Google Calendar → the target calendar's settings → *Share with specific people* → paste the SA email → give **Make changes to events**.
- **Sheet**: open the sheet → Share → paste the SA email → give **Editor**.

## Configure

```
GOOGLE_SERVICE_ACCOUNT_JSON=/absolute/path/to/service-account.json
GOOGLE_CALENDAR_ID=primary               # or the long @group.calendar.google.com id
GOOGLE_SHEET_ID=<from the sheet URL>
GOOGLE_SHEET_TAB=calls
CALENDAR_BACKEND=google
CRM_SINK=sheets
```

To use both GHL and Sheets:
```
CRM_SINK=ghl+sheets
```

## What the Sheets sink does

Ensures the first row of the `calls` tab contains the standard header. On every call end, appends a row with:

`timestamp, session_id, caller_name, phone, intent, service, preferred_date, preferred_time, urgency, lead_score, status, escalated, summary`

Point Zapier, n8n, or Make at this sheet if you need downstream automation.

## What the Google Calendar backend does

Replaces the fake calendar. The brain calls the same `check_availability` and `book_appointment` tools; internally they hit Google Calendar. The event summary is `<service> — <caller_name>`; the description contains the full booking details.

## Deps

Uncomment in `apps/api/requirements.txt`:
```
google-api-python-client>=2.140
google-auth>=2.32
```
Then `pip install -r apps/api/requirements.txt`.
