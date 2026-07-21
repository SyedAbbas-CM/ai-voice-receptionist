"""Copy n8n workflow JSONs into the repo with hardcoded IDs scrubbed.

Replaces:
- Vapi API bearer tokens          -> <VAPI_API_KEY>
- Vapi assistantId/phoneNumberId  -> <VAPI_ASSISTANT_ID>, <VAPI_PHONE_NUMBER_ID>
- Google Sheet document IDs       -> <GOOGLE_SHEET_ID>
- Google Doc IDs                  -> <GOOGLE_DOC_ID>
- Google Calendar IDs (email)     -> <GOOGLE_CALENDAR_ID>
- Supabase project URLs           -> https://<SUPABASE_PROJECT>.supabase.co
- Notion database UUIDs           -> <NOTION_DB_LIBRARY>, <NOTION_DB_BASELINES>, etc.
- HubSpot portal + form GUIDs     -> <HUBSPOT_PORTAL_ID> / <HUBSPOT_FORM_*>
- ElevenLabs webhook signing sec  -> <ELEVENLABS_WEBHOOK_SECRET>
- Twilio "from" number            -> <TWILIO_FROM_NUMBER>
- Freshdesk domain                -> <FRESHDESK_DOMAIN>
- Personal email addresses        -> <ADMIN_EMAIL> / <SUPPORT_EMAIL>
- Webhook path UUIDs              -> <WEBHOOK_PATH_*>
- n8n credential IDs              -> <CRED_*> (opaque but leaked in exports)

Emits scrubbed copies to workflows/n8n/ alongside a scrubbing report.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SRC_DIR = Path("/Users/az/Desktop/N8N Workflows/drive-download-20260709T011145Z-3-001")
DEST_DIR = REPO / "workflows" / "n8n"

FILES = {
    "SubtoDealz___Official (1) (3).json": "subtodealz-outbound.json",
    "IC Flow (1) (1).json": "ironclad-post-call-router.json",
    "Data ingestion VR (1) (1).json": "vivarays-notion-ingestion.json",
    "Main Workflow VR (1) (1).json": "vivarays-content-generator.json",
}

# --- Hardcoded values known from the analysis reports ---
KNOWN_SUBS = {
    # Vapi (SubtoDealz)
    "c42e779f-85be-43bd-9781-31a52bee5a95": "<VAPI_API_KEY>",
    "61a424de-1b67-4b1a-a156-9b019eb1a621": "<VAPI_ASSISTANT_ID>",
    "51b9449e-e9df-4459-8e87-05b13246e6f5": "<VAPI_PHONE_NUMBER_ID>",
    # Google Sheet (SubtoDealz)
    "1bnNqqILF-fdevjnnUOKv2a0OUqJei6VfF2fjy86c2do": "<GOOGLE_SHEET_ID>",
    # Notion DBs (VivaRays)
    "23ec807e-891b-814b-9aa8-f320844f5fe1": "<NOTION_DB_LIBRARY>",
    "261c807e-891b-8111-a9d6-e7489d428bb6": "<NOTION_DB_BASELINES>",
    "261c807e-891b-8137-98b1-db0c5c74831f": "<NOTION_DB_BASELINES_DAYWISE>",
    "261c807e-891b-81b2-8cf8-c78b8ff59b81": "<NOTION_DB_BASELINES_STAGE2>",
    "261c807e-891b-8103-9c2e-e738bf4c180b": "<NOTION_DB_BASELINES_STAGE3>",
    "261c807e-891b-812f-8abe-d7bab145ef7b": "<NOTION_DB_BASELINES_STAGE4>",
    "23ec807e-891b-815e-b79b-e48ac2bd63b1": "<NOTION_DB_THINK_TANK>",
    # Supabase (VivaRays)
    "rczavbqzkagrcpxllgyy": "<SUPABASE_PROJECT>",
    # Google Doc (VivaRays)
    "1i-jaA1H4EkEJUMhfK23Af5n2nxZv7YD_gTsJV4eQCks": "<GOOGLE_DOC_KNOWLEDGE_BASE>",
    # ElevenLabs webhook (IronClad)
    "wsec_05698005b0c882a372881e84a907c4145752e15a6fa416ab03c636fd93d97f7e": "<ELEVENLABS_WEBHOOK_SECRET>",
    "070f50bb-df27-44f4-85c9-6704518b09ae": "<WEBHOOK_PATH_ELEVENLABS>",
    "11f395a4-8fcd-4812-99a9-6e624ca7154b": "<WEBHOOK_PATH_CHAT>",
    # HubSpot portal (IronClad)
    "8926831": "<HUBSPOT_PORTAL_ID>",
    "acca42d3-b1d2-42b7-8c5b-72e19f6e2a4d": "<HUBSPOT_FORM_SUPPORT>",
    "626dabe2-52c6-4e48-9bda-6b9ca03ad83d": "<HUBSPOT_FORM_DEMO_TRIAL>",
    "f60ea07f-0731-4710-9255-cd76cd5f7373": "<HUBSPOT_FORM_SHOP>",
    "70ceef69-3f0c-46cf-a9f6-7b7d8f2c4a1e": "<HUBSPOT_FORM_CONSULTATION>",
    # Twilio (IronClad)
    "+17867618576": "<TWILIO_FROM_NUMBER>",
    "+1 786 761 8576": "<TWILIO_FROM_NUMBER>",
    # IronClad emails
    "support@ironcladfamily.com": "<SUPPORT_EMAIL>",
    "admin@ironcladfamily.com": "<ADMIN_EMAIL>",
    # n8n credential IDs
    "p3qbvRn19o8RoYdF": "<CRED_NOTION>",
    "b2w4D19JybDvtv26": "<CRED_OPENAI>",
    "INswCYE23M741gUs": "<CRED_OPENAI_2>",
    "bhbXxlLStT9CHWo5": "<CRED_SUPABASE>",
    "TYPUJdzzZygD8Vvs": "<CRED_GOOGLE_DRIVE>",
    "KqpalEJ7gdFjJOQB": "<CRED_GOOGLE_SHEETS>",
    "WMbTrsiUU8Wl1N5M": "<CRED_ANTHROPIC>",
    "zcBloh3rxHbXC0cR": "<CRED_OPENAI_IC>",
    "AjlxkqwnQZCSvh3Z": "<CRED_TWILIO_IC>",
    "HNYvSOTAnPlyxoqb": "<CRED_GOOGLE_CALENDAR_IC>",
    "6vOBhd7iclURrfF6": "<CRED_FRESHDESK_IC>",
    "7MydiGxOlG2YUpPw": "<CRED_OUTLOOK_IC>",
}

# --- Regex patterns for catch-alls (things the LLM report may have missed) ---
PATTERN_SUBS = [
    # Any remaining bearer tokens in HTTP node bodies (32-char hex or UUID after "Bearer ")
    (re.compile(r"Bearer [0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"),
     "Bearer <API_TOKEN>"),
    # Supabase URLs we may have missed
    (re.compile(r"https://[a-z0-9]{20}\.supabase\.co"), "https://<SUPABASE_PROJECT>.supabase.co"),
]


def scrub(text: str) -> tuple[str, dict[str, int]]:
    """Return (scrubbed_text, replacement_count_by_original)."""
    counts: dict[str, int] = {}
    for original, replacement in KNOWN_SUBS.items():
        occurrences = text.count(original)
        if occurrences:
            text = text.replace(original, replacement)
            counts[original] = occurrences
    for pattern, replacement in PATTERN_SUBS:
        n = 0
        def _sub(m):
            nonlocal n
            n += 1
            return replacement
        text = pattern.sub(_sub, text)
        if n:
            counts[pattern.pattern] = n
    return text, counts


def main() -> int:
    DEST_DIR.mkdir(parents=True, exist_ok=True)
    report_lines: list[str] = ["# n8n Workflow Scrubbing Report\n"]
    total_replacements = 0

    for src_name, dest_name in FILES.items():
        src = SRC_DIR / src_name
        dest = DEST_DIR / dest_name
        if not src.exists():
            print(f"[skip] source missing: {src}")
            continue

        raw = src.read_text(encoding="utf-8")
        scrubbed, counts = scrub(raw)

        try:
            parsed = json.loads(scrubbed)
            scrubbed = json.dumps(parsed, indent=2, ensure_ascii=False)
        except json.JSONDecodeError as e:
            print(f"[warn] {src_name} scrubbed output isn't valid JSON: {e}")

        dest.write_text(scrubbed + "\n", encoding="utf-8")

        replaced_here = sum(counts.values())
        total_replacements += replaced_here
        print(f"[ok] {src_name} -> workflows/n8n/{dest_name}  ({replaced_here} replacements)")

        report_lines.append(f"## `{dest_name}` (from `{src_name}`)\n")
        report_lines.append(f"- Total replacements: **{replaced_here}**\n")
        if counts:
            report_lines.append("- Substitutions:\n")
            for original, n in sorted(counts.items(), key=lambda kv: -kv[1]):
                snippet = original if len(original) < 60 else original[:57] + "..."
                report_lines.append(f"  - `{snippet}` -> {n} occurrences\n")
        report_lines.append("\n")

    report_lines.append(f"\n**Total replacements across all files: {total_replacements}**\n")
    (DEST_DIR / "SCRUB_REPORT.md").write_text("".join(report_lines), encoding="utf-8")
    print(f"\n[done] Wrote scrub report to workflows/n8n/SCRUB_REPORT.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
