"""Annotation Dashboard Phase 1 (task #94).

Human QA feedback loop. Reviewer opens `/admin/annotate/{call_id}`,
sees the full transcript, tags turns as win/fail/etc., leaves free-text
notes, saves. Later phases (#96 LK judges, #97 golden corpus) auto-
populate `auto_labels` alongside human tags.

## Endpoints

  * `GET  /admin/annotate` — index page: last 50 calls with annotation status
  * `GET  /admin/annotate/{call_id}` — annotation form for one call
  * `POST /admin/annotate/{call_id}/save` — upsert annotation
  * `GET  /admin/annotate/{call_id}/json` — annotation payload as JSON
  * `POST /admin/annotate/{call_id}/gold` — toggle is_gold flag

Admin-token gated (same pattern as `/admin/calls/{call_id}/incident`).

## Why not tenant-scoped

Annotation is an operator function — YOU (the receptionist-agent
operator) reviewing calls across tenants to improve the shared agent.
Later, per-tenant reviewer flows can gate on tenant Bearer instead of
admin token.

## Not in v1

- No paging on the index (limit 50 is enough for pilot).
- No CSV export (add when annotations start piling up).
- No undo/history (last-write-wins; add `call_annotation_history`
  table later if needed).
- No filter by tenant / verdict on the index.
"""
from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy.orm import Session

from app.db import CallAnnotation, SessionRow, TranscriptRow, BookingRow
from app.db.session import get_session
from app.routes.admin import _require_admin


router = APIRouter(prefix="/admin/annotate", tags=["admin", "annotate"])


# ─── helpers ────────────────────────────────────────────────────────────────


def _session_id_from_call_id(call_id: str) -> str:
    """Twilio call rows are stored with id = f'twilio_{CallSid}'. Accept
    either raw CA-SID or the prefixed form so URLs are forgiving."""
    if call_id.startswith("twilio_"):
        return call_id
    return f"twilio_{call_id}"


def _raw_call_id(call_id: str) -> str:
    """Strip the twilio_ prefix so annotations key on the bare CA-SID."""
    if call_id.startswith("twilio_"):
        return call_id[len("twilio_"):]
    return call_id


def _to_iso(dt: object) -> Optional[str]:
    if dt is None:
        return None
    if isinstance(dt, str):
        return dt
    try:
        return dt.isoformat()  # type: ignore[attr-defined]
    except Exception:
        return str(dt)


def _js_json(obj: object) -> str:
    """json.dumps that is safe to embed inside a <script> tag.

    Security 2026-08-31: transcript text + annotator input reach the
    annotator page via json.dumps().  If any string contains
    '</script>' the HTML parser terminates the script block early and
    the rest of the payload becomes injected markup.  U+2028 / U+2029
    are valid in JSON but line terminators in JavaScript — they break
    the parser or (worse) enable smuggling.  Escape all four to their
    \\uXXXX form + the ampersand for defense in depth.

    Round-trip safe: json.loads recovers the identical object because
    JSON accepts these as unicode escapes.
    """
    import json as _json
    return (
        _json.dumps(obj)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )


# ─── GET /admin/annotate — index ────────────────────────────────────────────


@router.get("", response_class=HTMLResponse)
def get_index(request: Request, db: Session = Depends(get_session)) -> HTMLResponse:
    """Index: last 50 calls, showing annotation status."""
    _require_admin(request)

    # Recent sessions across all tenants (admin cross-tenant read).
    sessions = (
        db.query(SessionRow)
        .execution_options(allow_cross_tenant=True)
        .order_by(SessionRow.started_at.desc())
        .limit(50)
        .all()
    )

    # Batch-load annotations for those sessions in one query.
    call_ids = [_raw_call_id(s.id) for s in sessions]
    annotations = {
        a.call_id: a
        for a in db.query(CallAnnotation)
        .execution_options(allow_cross_tenant=True)
        .filter(CallAnnotation.call_id.in_(call_ids))
        .all()
    }

    # Build the HTML. Keep it small + inline CSS so no external deps.
    rows_html = []
    for s in sessions:
        cid = _raw_call_id(s.id)
        ann = annotations.get(cid)
        verdict = ann.verdict if ann else "unreviewed"
        gold = "⭐" if (ann and ann.is_gold) else ""
        verdict_class = {
            "win": "verdict-win",
            "fail": "verdict-fail",
            "mixed": "verdict-mixed",
            "unreviewed": "verdict-none",
        }.get(verdict, "verdict-none")
        started = _to_iso(s.started_at) or ""
        rows_html.append(
            f'<tr>'
            f'<td><code>{html.escape(cid)}</code></td>'
            f'<td>{html.escape(s.tenant_id or "?")}</td>'
            f'<td>{html.escape(started[:19])}</td>'
            f'<td><span class="{verdict_class}">{html.escape(verdict)}</span> {gold}</td>'
            f'<td>'
            f'<a href="/admin/annotate/{html.escape(cid)}">annotate</a>'
            f' &middot; '
            f'<a href="/trace/{html.escape(cid)}" target="_blank" '
            f'title="Humanness event timeline">trace ↗</a>'
            f'</td>'
            f'</tr>'
        )

    body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Call annotations</title>
<style>
  body {{ font-family: -apple-system, sans-serif; margin: 2em; max-width: 1000px; }}
  h1 {{ font-size: 1.4em; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ text-align: left; padding: 0.4em 0.6em; border-bottom: 1px solid #eee; font-size: 0.9em; }}
  th {{ background: #fafafa; }}
  code {{ font-size: 0.85em; }}
  .verdict-win {{ color: #0a7c3a; font-weight: 600; }}
  .verdict-fail {{ color: #c22; font-weight: 600; }}
  .verdict-mixed {{ color: #d47800; font-weight: 600; }}
  .verdict-none {{ color: #888; }}
  .hint {{ color: #666; font-size: 0.85em; margin-bottom: 1em; }}
</style></head><body>
<h1>Call annotations</h1>
<p class="hint">Last {len(sessions)} calls across all tenants. Click any CallSid to annotate.</p>
<table>
<thead><tr><th>CallSid</th><th>Tenant</th><th>Started (UTC)</th><th>Verdict</th><th></th></tr></thead>
<tbody>{"".join(rows_html)}</tbody>
</table>
</body></html>"""
    return HTMLResponse(body)


# ─── GET /admin/annotate/{call_id} — form ───────────────────────────────────


# Reasonable starting tag vocabulary. Reviewer can also write a
# `comment` on any turn. Freeform text is fine too — the schema doesn't
# constrain the tag string, so new tags added here just work.
_TAG_VOCAB = [
    ("great_response", "great response"),
    ("wrong_service_asked", "asked wrong slot"),
    ("empty_completion", "empty LLM response"),
    ("hallucination", "hallucination / made up info"),
    ("bad_phrasing", "awkward phrasing"),
    ("wrong_time", "wrong date/time"),
    ("stt_garble", "STT misheard"),
    ("interrupted_early", "interrupted caller too early"),
    ("dead_air", "dead air / silence"),
    ("prompt_leak", "leaked system prompt text"),
]


@router.get("/{call_id}", response_class=HTMLResponse)
def get_annotation_form(
    call_id: str, request: Request, db: Session = Depends(get_session),
) -> HTMLResponse:
    """Form: transcript + tags + verdict + save."""
    _require_admin(request)

    raw = _raw_call_id(call_id)
    session_id = _session_id_from_call_id(raw)

    # Session + transcript
    sess = (
        db.query(SessionRow)
        .execution_options(allow_cross_tenant=True)
        .filter(SessionRow.id == session_id)
        .one_or_none()
    )
    turns = (
        db.query(TranscriptRow)
        .execution_options(allow_cross_tenant=True)
        .filter(TranscriptRow.session_id == session_id)
        .order_by(TranscriptRow.timestamp.asc())
        .all()
    )
    if sess is None and not turns:
        raise HTTPException(
            status_code=404,
            detail=f"no signal for call_id={raw!r}",
        )

    # Existing annotation (if any)
    ann = (
        db.query(CallAnnotation)
        .execution_options(allow_cross_tenant=True)
        .filter(CallAnnotation.call_id == raw)
        .one_or_none()
    )
    existing_verdict = ann.verdict if ann else "unreviewed"
    existing_notes = ann.notes if ann else ""
    existing_is_gold = bool(ann and ann.is_gold)
    existing_tags = ann.turn_tags if (ann and ann.turn_tags) else []
    # Map for lookup: {turn_idx: {tag: comment}}
    tag_lookup: dict[int, dict[str, str]] = {}
    for entry in existing_tags:
        idx = entry.get("turn_idx")
        tag = entry.get("tag")
        if idx is None or not tag:
            continue
        tag_lookup.setdefault(idx, {})[tag] = entry.get("comment", "")

    # ─── Redesigned reviewer console (task #104, 2026-08-31) ─────────
    #
    # UX change: NO always-on tag checkboxes per turn (screenshot showed
    # every row displayed all 10 tags + a "comment for this turn" input,
    # which read like "remarks for the caller"). Instead:
    #
    #   - Transcript rows are minimal — role chip + timestamp + text.
    #   - Click a row → right sidebar becomes the tag panel for THAT
    #     turn. One turn at a time, focus mode.
    #   - Tool payloads collapsed to one-line summary; click to expand.
    #   - Comment field ("Note (only you see this)") only appears when
    #     a tag is checked — no bare textarea invitation to write to
    #     caller.
    #   - Left rail: color-coded timeline of every turn (speaker + tag
    #     severity if annotated). Scan-at-a-glance where problems
    #     clustered.
    #   - Keyboard: j/k step turns, 1-9 toggle tag N on focused turn,
    #     w/f/m set verdict, g gold, ⌘S save.
    #
    # Same POST contract — form field names identical, POST /save
    # handler unchanged. Backwards-compat with existing annotations.

    # Serialize turns to JSON for client-side rendering. Keeps the
    # template small + lets the JS handle the focus/tag interactions
    # without a full page reload per click.
    #
    # Security 2026-08-31: transcript text comes from live STT
    # (untrusted).  Embed via _js_json which escapes < > & and the
    # U+2028/U+2029 line separators — otherwise a caller/annotator
    # value containing '</script>' breaks out of the <script> block.
    turns_json = _js_json([
        {
            "idx": i,
            "role": ("caller" if t.role == "user"
                     else "agent" if t.role == "assistant"
                     else t.role),
            "text": t.text or "",
            "ts": (_to_iso(t.timestamp) or "")[11:19],
            "tool_name": t.tool_name,
            "tool_args": t.tool_args,
            "tool_result": t.tool_result,
        }
        for i, t in enumerate(turns)
    ])
    tag_vocab_json = _js_json(_TAG_VOCAB)
    tag_lookup_json = _js_json(
        {str(k): v for k, v in tag_lookup.items()}
    )

    call_started = _to_iso(sess.started_at) if sess else "?"
    tenant_label = (sess.tenant_id if sess else "?") or "?"
    reviewer_val = html.escape((ann.reviewer_id if ann else "") or "")
    notes_val = html.escape(existing_notes or "")

    body = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Review · {html.escape(raw[:12])}…</title>
<style>
  /* ─── Design tokens — audit-desk theme (task #104) ─── */
  :root {{
    --ink: #1a1a1a;
    --ink-2: #4a4a4a;
    --ink-3: #8a8a8a;
    --paper: #fafaf7;
    --panel: #ffffff;
    --rule: #e6e3dc;
    --rule-2: #d4d0c6;
    --caller-band: #eef1f7;
    --caller-tab: #4a6fa5;
    --agent-band: #fbf5ea;
    --agent-tab: #b8804e;
    --tool-band: #f2f2ec;
    --tool-tab: #7a7a6e;
    --accent: #b8360f;       /* vermilion signature */
    --accent-soft: #f2ddd4;
    --good: #2d6a4f;
    --warn: #b45309;
    --danger: #b8360f;
    --focus: #b8360f;
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      --ink: #eaeae4;
      --ink-2: #b0b0a8;
      --ink-3: #808078;
      --paper: #1a1a17;
      --panel: #22221e;
      --rule: #33332e;
      --rule-2: #464640;
      --caller-band: #1c2434;
      --caller-tab: #7a95c2;
      --agent-band: #2a221a;
      --agent-tab: #d69f6c;
      --tool-band: #26261f;
      --tool-tab: #a09e8e;
      --accent: #e46744;
      --accent-soft: #3a2018;
    }}
  }}
  :root[data-theme="dark"] {{
    --ink: #eaeae4;
    --ink-2: #b0b0a8;
    --ink-3: #808078;
    --paper: #1a1a17;
    --panel: #22221e;
    --rule: #33332e;
    --rule-2: #464640;
    --caller-band: #1c2434;
    --caller-tab: #7a95c2;
    --agent-band: #2a221a;
    --agent-tab: #d69f6c;
    --tool-band: #26261f;
    --tool-tab: #a09e8e;
    --accent: #e46744;
    --accent-soft: #3a2018;
  }}

  * {{ box-sizing: border-box; }}
  html, body {{ margin: 0; padding: 0; background: var(--paper); color: var(--ink); }}
  body {{
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    font-size: 14px;
    line-height: 1.5;
    -webkit-font-smoothing: antialiased;
  }}
  .serif {{ font-family: 'Fraunces', Georgia, serif; }}
  .mono  {{ font-family: 'JetBrains Mono', 'SF Mono', Menlo, Consolas, monospace; }}

  /* ─── Layout: 3-column desk ─── */
  .app {{
    display: grid;
    grid-template-columns: 88px minmax(0, 1fr) 340px;
    grid-template-rows: auto 1fr auto;
    grid-template-areas:
      "hdr hdr hdr"
      "rail stream panel"
      "ftr ftr ftr";
    height: 100vh;
  }}
  @media (max-width: 900px) {{
    .app {{
      grid-template-columns: 1fr;
      grid-template-areas: "hdr" "stream" "panel" "ftr";
      height: auto;
    }}
    .rail {{ display: none; }}
  }}

  /* ─── Header ─── */
  .hdr {{
    grid-area: hdr;
    display: flex; align-items: center; gap: 20px;
    padding: 12px 24px;
    background: var(--panel);
    border-bottom: 1px solid var(--rule);
  }}
  .hdr h1 {{
    font-family: 'Fraunces', Georgia, serif;
    font-weight: 500;
    font-size: 20px;
    margin: 0;
    letter-spacing: -0.01em;
  }}
  .hdr .cid {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 12px;
    color: var(--ink-3);
    padding: 3px 8px;
    background: var(--paper);
    border: 1px solid var(--rule);
    border-radius: 4px;
  }}
  .hdr .meta {{ color: var(--ink-3); font-size: 12px; }}
  .hdr .meta b {{ color: var(--ink-2); font-weight: 500; }}
  .hdr .spacer {{ flex: 1; }}
  .hdr a {{ color: var(--ink-2); text-decoration: none; font-size: 12px; margin-left: 12px; }}
  .hdr a:hover {{ color: var(--accent); }}

  /* ─── Left rail: turn timeline ─── */
  .rail {{
    grid-area: rail;
    background: var(--panel);
    border-right: 1px solid var(--rule);
    overflow-y: auto;
    padding: 12px 8px;
  }}
  .rail-title {{
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--ink-3);
    padding: 0 8px 8px;
  }}
  .rail-turn {{
    display: flex; align-items: center; gap: 8px;
    padding: 4px 8px;
    cursor: pointer;
    border-radius: 4px;
    color: var(--ink-2);
    font-size: 11px;
    font-family: 'JetBrains Mono', monospace;
    transition: background 0.1s;
  }}
  .rail-turn:hover {{ background: var(--paper); }}
  .rail-turn.active {{ background: var(--accent-soft); color: var(--ink); }}
  .rail-turn .stripe {{
    width: 4px; height: 24px; border-radius: 2px;
    flex-shrink: 0;
  }}
  .rail-turn[data-role="caller"] .stripe {{ background: var(--caller-tab); }}
  .rail-turn[data-role="agent"]  .stripe {{ background: var(--agent-tab); }}
  .rail-turn[data-role="tool"]   .stripe {{ background: var(--tool-tab); }}
  .rail-turn.tagged .stripe     {{ box-shadow: inset 0 0 0 2px var(--accent); }}
  .rail-turn .idx {{ min-width: 24px; text-align: right; }}

  /* ─── Center: transcript stream ─── */
  .stream {{
    grid-area: stream;
    overflow-y: auto;
    padding: 24px 32px;
  }}
  .turn {{
    display: grid;
    grid-template-columns: 80px 1fr;
    gap: 16px;
    padding: 12px 16px;
    margin-bottom: 4px;
    border-radius: 6px;
    cursor: pointer;
    transition: background 0.1s;
    scroll-margin-top: 20px;
  }}
  .turn:hover {{ background: rgba(184, 54, 15, 0.03); }}
  .turn.focused {{
    background: var(--accent-soft);
    box-shadow: inset 3px 0 0 var(--accent);
  }}
  .turn[data-role="caller"] {{ background: var(--caller-band); }}
  .turn[data-role="caller"].focused {{ background: var(--accent-soft); }}
  .turn[data-role="agent"]  {{ background: var(--agent-band); }}
  .turn[data-role="agent"].focused {{ background: var(--accent-soft); }}
  .turn[data-role="tool"]   {{ background: var(--tool-band); }}
  .turn[data-role="tool"].focused {{ background: var(--accent-soft); }}

  .turn .who {{
    text-align: right;
    font-size: 10px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--ink-3);
    padding-top: 3px;
  }}
  .turn .who .idx {{
    display: block;
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    color: var(--ink-3);
    margin-top: 2px;
  }}
  .turn .role-caller-txt {{ color: var(--caller-tab); font-weight: 600; }}
  .turn .role-agent-txt  {{ color: var(--agent-tab);  font-weight: 600; }}
  .turn .role-tool-txt   {{ color: var(--tool-tab);   font-weight: 600; }}

  .turn .text {{ color: var(--ink); line-height: 1.55; }}
  .turn .text.tool-line {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 12px;
    color: var(--ink-2);
  }}
  .turn .tool-summary {{
    color: var(--tool-tab);
    font-weight: 500;
  }}
  .turn .tool-details {{
    display: none;
    margin-top: 8px;
    padding: 8px 12px;
    background: var(--panel);
    border: 1px solid var(--rule);
    border-radius: 4px;
    font-size: 11px;
    white-space: pre-wrap;
    word-break: break-all;
  }}
  .turn.expanded .tool-details {{ display: block; }}
  .turn .badges {{ margin-top: 6px; }}
  .turn .badge {{
    display: inline-block;
    font-size: 10px;
    padding: 2px 6px;
    margin-right: 4px;
    background: var(--accent);
    color: white;
    border-radius: 3px;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }}
  .turn .ts {{
    display: block;
    font-family: 'JetBrains Mono', monospace;
    font-size: 10px;
    color: var(--ink-3);
    margin-top: 2px;
  }}

  /* ─── Right panel: tag console ─── */
  .panel {{
    grid-area: panel;
    background: var(--panel);
    border-left: 1px solid var(--rule);
    overflow-y: auto;
    padding: 20px 20px 100px;
  }}
  .panel-hd {{
    font-family: 'Fraunces', Georgia, serif;
    font-size: 16px;
    font-weight: 500;
    margin: 0 0 4px;
  }}
  .panel-sub {{
    font-size: 11px;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--ink-3);
    margin-bottom: 12px;
  }}
  .panel-empty {{
    color: var(--ink-3);
    font-size: 13px;
    padding: 20px 0;
    font-style: italic;
  }}
  .tag {{
    display: flex; align-items: center; gap: 10px;
    padding: 8px 10px;
    border-radius: 4px;
    cursor: pointer;
    transition: background 0.1s;
  }}
  .tag:hover {{ background: var(--paper); }}
  .tag input {{ margin: 0; accent-color: var(--accent); }}
  .tag .kbd {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 10px;
    padding: 1px 5px;
    background: var(--paper);
    border: 1px solid var(--rule);
    border-radius: 3px;
    color: var(--ink-3);
    margin-left: auto;
  }}
  .tag-note {{
    display: none;
    margin: 6px 0 12px 24px;
  }}
  .tag.checked ~ .tag-note {{ display: block; }}
  .tag-note textarea {{
    width: 100%;
    min-height: 40px;
    padding: 6px 8px;
    font: inherit;
    font-size: 12px;
    background: var(--paper);
    color: var(--ink);
    border: 1px solid var(--rule);
    border-radius: 3px;
    resize: vertical;
  }}
  .tag-note label {{
    display: block;
    font-size: 10px;
    color: var(--ink-3);
    margin-bottom: 4px;
    text-transform: uppercase;
    letter-spacing: 0.06em;
  }}

  /* ─── Footer: save bar ─── */
  .ftr {{
    grid-area: ftr;
    background: var(--panel);
    border-top: 1px solid var(--rule);
    padding: 12px 24px;
    display: flex; align-items: center; gap: 16px;
    flex-wrap: wrap;
  }}
  .verdict-group {{
    display: flex; align-items: center; gap: 6px;
    font-size: 12px;
    color: var(--ink-2);
  }}
  .verdict-group .vlabel {{
    text-transform: uppercase;
    letter-spacing: 0.08em;
    font-size: 10px;
    color: var(--ink-3);
    margin-right: 4px;
  }}
  .v-btn {{
    padding: 6px 12px;
    border: 1px solid var(--rule);
    background: var(--paper);
    color: var(--ink-2);
    border-radius: 4px;
    cursor: pointer;
    font: inherit;
    font-size: 12px;
    text-transform: lowercase;
    letter-spacing: 0.02em;
  }}
  .v-btn:hover {{ border-color: var(--rule-2); }}
  .v-btn.active[data-v="win"]   {{ background: var(--good);   color: white; border-color: var(--good); }}
  .v-btn.active[data-v="fail"]  {{ background: var(--danger); color: white; border-color: var(--danger); }}
  .v-btn.active[data-v="mixed"] {{ background: var(--warn);   color: white; border-color: var(--warn); }}
  .v-btn.active[data-v="unreviewed"] {{ background: var(--ink-3); color: white; border-color: var(--ink-3); }}
  .gold-toggle {{
    display: flex; align-items: center; gap: 6px;
    font-size: 12px; color: var(--ink-2);
    padding: 6px 12px;
    border: 1px solid var(--rule);
    background: var(--paper);
    border-radius: 4px;
    cursor: pointer;
  }}
  .gold-toggle.on {{ background: #fef3c7; border-color: #d4a017; color: #78350f; }}
  .ftr-spacer {{ flex: 1; }}
  .debrief-btn, .save-btn {{
    padding: 8px 18px;
    font: inherit;
    font-size: 13px;
    font-weight: 500;
    border-radius: 4px;
    border: 1px solid var(--rule);
    background: var(--paper);
    color: var(--ink);
    cursor: pointer;
  }}
  .save-btn {{
    background: var(--accent);
    color: white;
    border-color: var(--accent);
  }}
  .save-btn:hover {{ background: #a02e0d; }}
  .debrief-btn:hover {{ border-color: var(--accent); color: var(--accent); }}
  .hint {{
    font-family: 'JetBrains Mono', monospace;
    font-size: 10px;
    color: var(--ink-3);
    margin-left: 8px;
  }}

  /* ─── Debrief modal (rare interaction, keep out of the way) ─── */
  .debrief {{
    display: none;
    position: fixed; inset: 0;
    background: rgba(0,0,0,0.4);
    z-index: 100;
    align-items: center; justify-content: center;
  }}
  .debrief.open {{ display: flex; }}
  .debrief-box {{
    background: var(--panel);
    padding: 24px;
    border-radius: 8px;
    width: 90%; max-width: 540px;
    box-shadow: 0 20px 60px rgba(0,0,0,0.2);
  }}
  .debrief-box h3 {{ margin: 0 0 4px; font-family: 'Fraunces', Georgia, serif; font-weight: 500; }}
  .debrief-box p {{ font-size: 12px; color: var(--ink-3); margin: 0 0 12px; }}
  .debrief-box textarea {{
    width: 100%; min-height: 120px;
    padding: 10px;
    font: inherit;
    font-size: 13px;
    line-height: 1.5;
    background: var(--paper);
    color: var(--ink);
    border: 1px solid var(--rule);
    border-radius: 4px;
    resize: vertical;
  }}
  .debrief-box input {{
    margin-top: 12px;
    width: 100%;
    padding: 8px;
    font: inherit;
    background: var(--paper);
    color: var(--ink);
    border: 1px solid var(--rule);
    border-radius: 4px;
  }}
  .debrief-box .row {{ display: flex; gap: 8px; margin-top: 16px; justify-content: flex-end; }}

  /* ─── Toast ─── */
  .toast {{
    position: fixed; bottom: 88px; left: 50%; transform: translateX(-50%);
    background: var(--ink); color: var(--paper);
    padding: 10px 18px;
    border-radius: 4px;
    font-size: 12px;
    opacity: 0; transition: opacity 0.2s;
    pointer-events: none;
  }}
  .toast.show {{ opacity: 1; }}
</style>
</head>
<body>
<form method="POST" action="/admin/annotate/{html.escape(raw)}/save" id="annotate-form">

<div class="app">

  <!-- Header -->
  <header class="hdr">
    <h1 class="serif">Call review</h1>
    <span class="cid">{html.escape(raw[:16])}…</span>
    <span class="meta"><b>{html.escape(tenant_label)}</b> · {html.escape(call_started)} · {len(turns)} turns</span>
    <span class="spacer"></span>
    <a href="/admin/annotate">← all calls</a>
    <a href="/trace/{html.escape(raw)}" target="_blank">humanness ↗</a>
    <a href="#" onclick="document.cookie='voiceops_admin=; Max-Age=0; path=/'; location='/admin/login'; return false;">sign out</a>
  </header>

  <!-- Left rail: turn timeline -->
  <aside class="rail" id="rail">
    <div class="rail-title">Turns</div>
    <div id="rail-turns"></div>
  </aside>

  <!-- Center: transcript stream -->
  <main class="stream" id="stream"></main>

  <!-- Right: tag panel -->
  <aside class="panel" id="panel">
    <div class="panel-empty" id="panel-empty">
      Click any turn on the left to tag it.<br><br>
      Keyboard: <span class="mono">j</span>/<span class="mono">k</span> step turns,
      <span class="mono">1-9</span> toggle tag N,
      <span class="mono">w</span>/<span class="mono">f</span>/<span class="mono">m</span> verdict,
      <span class="mono">g</span> gold, <span class="mono">⌘S</span> save.
    </div>
    <div id="panel-content" style="display:none">
      <div class="panel-hd" id="panel-hd">Turn #0</div>
      <div class="panel-sub" id="panel-sub">CALLER</div>
      <div id="tags"></div>
    </div>
  </aside>

  <!-- Footer: save bar -->
  <footer class="ftr">
    <div class="verdict-group">
      <span class="vlabel">Verdict</span>
      <button type="button" class="v-btn" data-v="win">win</button>
      <button type="button" class="v-btn" data-v="fail">fail</button>
      <button type="button" class="v-btn" data-v="mixed">mixed</button>
      <button type="button" class="v-btn" data-v="unreviewed">unreviewed</button>
    </div>
    <label class="gold-toggle" id="gold-toggle">
      <input type="checkbox" name="is_gold" value="1" id="is-gold-input" style="display:none">
      <span>★</span> <span>gold reference</span>
    </label>
    <span class="ftr-spacer"></span>
    <button type="button" class="debrief-btn" onclick="openDebrief()">Debrief…</button>
    <button type="submit" class="save-btn">Save review <span class="hint">⌘S</span></button>
  </footer>

</div>

<!-- Hidden fields the form posts -->
<input type="hidden" name="verdict" value="{html.escape(existing_verdict)}" id="verdict-input">
<textarea name="notes" style="display:none">{notes_val}</textarea>
<input type="hidden" name="reviewer_id" value="{reviewer_val}" id="reviewer-input">

<!-- Debrief modal -->
<div class="debrief" id="debrief">
  <div class="debrief-box">
    <h3 class="serif">Call debrief</h3>
    <p>Long-form notes — what went wrong, what to fix, ideas for training data. Only you see this.</p>
    <textarea id="notes-editor" placeholder="e.g. Agent looped 5x asking for service. PII redactor eating the date. Should mark as fail.">{notes_val}</textarea>
    <input type="text" id="reviewer-editor" placeholder="Your name or email" value="{reviewer_val}">
    <div class="row">
      <button type="button" class="debrief-btn" onclick="closeDebrief()">Cancel</button>
      <button type="button" class="save-btn" onclick="saveDebrief()">Done</button>
    </div>
  </div>
</div>

<div class="toast" id="toast">Saved</div>

</form>

<script>
  // ─── State ─────────────────────────────────────────────────────
  const TURNS = {turns_json};
  const TAG_VOCAB = {tag_vocab_json};
  const EXISTING_TAGS = {tag_lookup_json};  // {{turn_idx_str: {{tag: comment}}}}

  let focusIdx = null;
  // Per-turn state: {{ tags: {{tag_key: comment}} }}
  const state = {{}};
  TURNS.forEach(t => {{
    const existing = EXISTING_TAGS[String(t.idx)] || {{}};
    state[t.idx] = {{ tags: {{ ...existing }} }};
  }});

  // ─── Render: transcript stream + left rail ─────────────────────
  const streamEl = document.getElementById('stream');
  const railEl = document.getElementById('rail-turns');

  function toolSummary(t) {{
    if (!t.tool_name) return '';
    const args = t.tool_args || {{}};
    const result = t.tool_result || {{}};
    if (t.tool_name === 'check_availability') {{
      const slots = (result.open_slots || []).slice(0, 4);
      const dateArg = args.date || '?';
      if (slots.length) return `${{t.tool_name}}(${{dateArg}}) → ${{slots.length}}+ slots (${{slots.join(', ')}})`;
      return `${{t.tool_name}}(${{dateArg}}) → no slots`;
    }}
    if (t.tool_name === 'book_appointment') {{
      const ev = result.event || {{}};
      if (result.booked) return `${{t.tool_name}} → booked ${{ev.service || '?'}} @ ${{ev.start || '?'}}`;
      if (typeof result === 'string') return `${{t.tool_name}} → ${{result}}`;
      return `${{t.tool_name}} → ${{JSON.stringify(result).slice(0, 80)}}`;
    }}
    return `${{t.tool_name}}(${{Object.keys(args).join(',')}}) → ${{typeof result === 'object' ? JSON.stringify(result).slice(0,60) : String(result).slice(0,60)}}`;
  }}

  function renderTurns() {{
    streamEl.innerHTML = '';
    railEl.innerHTML = '';
    TURNS.forEach(t => {{
      const tagged = Object.keys(state[t.idx].tags).length > 0;

      // Rail entry
      const rail = document.createElement('div');
      rail.className = 'rail-turn' + (tagged ? ' tagged' : '');
      rail.dataset.role = t.role;
      rail.dataset.idx = t.idx;
      rail.innerHTML = `<span class="stripe"></span><span class="idx">#${{t.idx}}</span>`;
      rail.onclick = () => focusTurn(t.idx);
      railEl.appendChild(rail);

      // Stream entry
      const div = document.createElement('div');
      div.className = 'turn';
      div.dataset.role = t.role;
      div.dataset.idx = t.idx;
      div.onclick = () => focusTurn(t.idx);

      const roleLabel = t.role.charAt(0).toUpperCase() + t.role.slice(1);
      let textHTML = '';
      if (t.role === 'tool' || t.tool_name) {{
        textHTML = `
          <div class="text tool-line">
            <span class="tool-summary">${{escape(toolSummary(t))}}</span>
            <div class="tool-details">${{escape(JSON.stringify({{args: t.tool_args, result: t.tool_result}}, null, 2))}}</div>
          </div>`;
      }} else {{
        textHTML = `<div class="text">${{escape(t.text)}}</div>`;
      }}
      const badges = Object.keys(state[t.idx].tags).map(k => {{
        const lbl = (TAG_VOCAB.find(v => v[0] === k) || [k, k])[1];
        return `<span class="badge" title="${{escape(lbl)}}">${{escape(k.replace(/_/g, ' '))}}</span>`;
      }}).join('');
      div.innerHTML = `
        <div class="who">
          <span class="role-${{t.role}}-txt">${{roleLabel}}</span>
          <span class="idx">#${{t.idx}}</span>
          <span class="ts mono">${{escape(t.ts)}}</span>
        </div>
        <div>
          ${{textHTML}}
          <div class="badges">${{badges}}</div>
        </div>`;
      // Toggle tool details on click when tool row
      if (t.role === 'tool' || t.tool_name) {{
        div.addEventListener('dblclick', e => {{
          e.stopPropagation();
          div.classList.toggle('expanded');
        }});
      }}
      streamEl.appendChild(div);
    }});
  }}

  function escape(s) {{
    if (s == null) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }}

  // ─── Focus / tag panel ─────────────────────────────────────────
  const panelEmpty = document.getElementById('panel-empty');
  const panelContent = document.getElementById('panel-content');
  const panelHd = document.getElementById('panel-hd');
  const panelSub = document.getElementById('panel-sub');
  const tagsEl = document.getElementById('tags');

  function focusTurn(idx) {{
    focusIdx = idx;
    document.querySelectorAll('.turn.focused').forEach(el => el.classList.remove('focused'));
    document.querySelectorAll('.rail-turn.active').forEach(el => el.classList.remove('active'));
    const turnEl = document.querySelector(`.turn[data-idx="${{idx}}"]`);
    const railTurnEl = document.querySelector(`.rail-turn[data-idx="${{idx}}"]`);
    if (turnEl) {{ turnEl.classList.add('focused'); turnEl.scrollIntoView({{block:'center', behavior:'smooth'}}); }}
    if (railTurnEl) railTurnEl.classList.add('active');

    const t = TURNS[idx];
    panelEmpty.style.display = 'none';
    panelContent.style.display = 'block';
    panelHd.textContent = `Turn #${{idx}}`;
    panelSub.textContent = t.role.toUpperCase();

    tagsEl.innerHTML = '';
    TAG_VOCAB.forEach((v, i) => {{
      const [key, label] = v;
      const isChecked = key in state[idx].tags;
      const wrap = document.createElement('div');
      wrap.innerHTML = `
        <label class="tag ${{isChecked ? 'checked' : ''}}" data-key="${{key}}">
          <input type="checkbox" ${{isChecked ? 'checked' : ''}}
                 name="tag_${{idx}}_${{key}}" value="1">
          <span>${{escape(label)}}</span>
          ${{i < 9 ? `<span class="kbd">${{i+1}}</span>` : ''}}
        </label>
        <div class="tag-note" data-key="${{key}}" ${{isChecked ? '' : 'style="display:none"'}}>
          <label>Note (only you see this)</label>
          <textarea name="comment_${{idx}}_${{key}}" placeholder="What went wrong here?">${{escape(state[idx].tags[key] || '')}}</textarea>
        </div>`;
      const cb = wrap.querySelector('input');
      const noteWrap = wrap.querySelector('.tag-note');
      const labelEl = wrap.querySelector('label');
      cb.addEventListener('change', () => {{
        if (cb.checked) {{
          state[idx].tags[key] = state[idx].tags[key] || '';
          labelEl.classList.add('checked');
          noteWrap.style.display = 'block';
        }} else {{
          delete state[idx].tags[key];
          labelEl.classList.remove('checked');
          noteWrap.style.display = 'none';
        }}
        renderTurns();       // update badges + rail tint
        focusTurn(idx);      // keep panel open on the same turn
      }});
      wrap.querySelector('textarea').addEventListener('input', e => {{
        state[idx].tags[key] = e.target.value;
      }});
      tagsEl.appendChild(wrap);
    }});
  }}

  // ─── Verdict + gold + debrief + save ───────────────────────────
  const verdictInput = document.getElementById('verdict-input');
  const goldInput = document.getElementById('is-gold-input');
  const goldToggle = document.getElementById('gold-toggle');

  function setVerdict(v) {{
    verdictInput.value = v;
    document.querySelectorAll('.v-btn').forEach(b => b.classList.toggle('active', b.dataset.v === v));
  }}
  document.querySelectorAll('.v-btn').forEach(b => {{
    b.addEventListener('click', () => setVerdict(b.dataset.v));
  }});
  setVerdict(verdictInput.value || 'unreviewed');

  function setGold(on) {{
    goldInput.checked = on;
    goldToggle.classList.toggle('on', on);
  }}
  goldToggle.addEventListener('click', e => {{ e.preventDefault(); setGold(!goldInput.checked); }});
  setGold({str(existing_is_gold).lower()});

  window.openDebrief = () => document.getElementById('debrief').classList.add('open');
  window.closeDebrief = () => document.getElementById('debrief').classList.remove('open');
  window.saveDebrief = () => {{
    const notesText = document.getElementById('notes-editor').value;
    const rev = document.getElementById('reviewer-editor').value;
    document.querySelector('textarea[name="notes"]').value = notesText;
    document.getElementById('reviewer-input').value = rev;
    closeDebrief();
  }};

  // Toast
  const toast = document.getElementById('toast');
  function showToast(msg) {{
    toast.textContent = msg;
    toast.classList.add('show');
    setTimeout(() => toast.classList.remove('show'), 1400);
  }}

  // ─── Keyboard nav ──────────────────────────────────────────────
  document.addEventListener('keydown', e => {{
    // Only when not editing text
    const active = document.activeElement;
    const inEditor = active && ['INPUT','TEXTAREA'].includes(active.tagName);
    if (inEditor && !(e.metaKey || e.ctrlKey)) return;

    // Cmd/Ctrl+S save
    if ((e.metaKey || e.ctrlKey) && e.key === 's') {{
      e.preventDefault();
      document.getElementById('annotate-form').requestSubmit();
      return;
    }}

    // ESC close modal
    if (e.key === 'Escape') {{ closeDebrief(); return; }}

    if (inEditor) return;

    if (e.key === 'j') {{
      const next = (focusIdx == null ? 0 : Math.min(TURNS.length - 1, focusIdx + 1));
      focusTurn(next);
    }} else if (e.key === 'k') {{
      const prev = (focusIdx == null ? 0 : Math.max(0, focusIdx - 1));
      focusTurn(prev);
    }} else if (e.key === 'w') {{ setVerdict('win'); showToast('verdict: win'); }}
    else if (e.key === 'f') {{ setVerdict('fail'); showToast('verdict: fail'); }}
    else if (e.key === 'm') {{ setVerdict('mixed'); showToast('verdict: mixed'); }}
    else if (e.key === 'g') {{ setGold(!goldInput.checked); showToast('gold: ' + (goldInput.checked ? 'on' : 'off')); }}
    else if (/^[1-9]$/.test(e.key) && focusIdx != null) {{
      const i = parseInt(e.key) - 1;
      if (i < TAG_VOCAB.length) {{
        const key = TAG_VOCAB[i][0];
        const cb = document.querySelector(`input[name="tag_${{focusIdx}}_${{key}}"]`);
        if (cb) {{ cb.checked = !cb.checked; cb.dispatchEvent(new Event('change')); }}
      }}
    }}
  }});

  renderTurns();
  // Focus first tagged turn if any, else first turn
  const firstTagged = TURNS.find(t => Object.keys(state[t.idx].tags).length > 0);
  if (firstTagged) focusTurn(firstTagged.idx);
</script>
</body></html>"""
    return HTMLResponse(body)


# ─── POST /admin/annotate/{call_id}/save — upsert ───────────────────────────


@router.post("/{call_id}/save")
async def save_annotation(
    call_id: str,
    request: Request,
    db: Session = Depends(get_session),
):
    """Upsert annotation for the call.

    Form fields (all optional except verdict):
      * verdict = win | fail | mixed | unreviewed
      * is_gold = "1" if checkbox checked, absent otherwise
      * notes = freetext
      * reviewer_id = freetext
      * tag_{i}_{tag_key} = "1" if that turn/tag checkbox is checked
      * comment_{i} = per-turn freetext comment
    """
    _require_admin(request)

    raw = _raw_call_id(call_id)
    form = await request.form()

    verdict = str(form.get("verdict", "unreviewed")) or "unreviewed"
    is_gold = form.get("is_gold") == "1"
    notes = str(form.get("notes", "")) or None
    reviewer_id = str(form.get("reviewer_id", "")) or None

    # Reconstruct turn_tags from form. Two field naming schemes are
    # both supported for backwards compatibility:
    #
    #   * tag_{i}_{tag_key}=1              → turn i has tag tag_key
    #   * comment_{i}_{tag_key}=...        → per-TAG comment (new UI, task #104)
    #   * comment_{i}=...                  → per-TURN comment (old UI form)
    #
    # v1 UI (index page + curl) posted comment_{i}. Redesigned UI (task
    # #104) posts comment_{i}_{tag_key} so each tag can carry its own
    # note. Both round-trip cleanly through the JSONB turn_tags list —
    # per-tag comments preserve identity; the legacy per-turn comment
    # collapses onto the FIRST tag of that turn (or standalone note).
    turn_tags: list[dict] = []
    turn_comments_legacy: dict[int, str] = {}
    turn_comments_by_tag: dict[tuple[int, str], str] = {}
    per_turn_tags: dict[int, list[str]] = {}
    for field_name, val in form.items():
        if not val:
            continue
        if field_name.startswith("tag_"):
            # tag_3_wrong_service_asked
            parts = field_name.split("_", 2)
            if len(parts) != 3:
                continue
            try:
                idx = int(parts[1])
            except ValueError:
                continue
            tag_key = parts[2]
            per_turn_tags.setdefault(idx, []).append(tag_key)
        elif field_name.startswith("comment_"):
            rest = field_name[len("comment_"):]
            # New shape: comment_{i}_{tag_key}
            if "_" in rest:
                idx_str, tag_key = rest.split("_", 1)
                try:
                    idx = int(idx_str)
                except ValueError:
                    continue
                comment = str(val).strip()
                if comment:
                    turn_comments_by_tag[(idx, tag_key)] = comment
            else:
                # Legacy shape: comment_{i}
                try:
                    idx = int(rest)
                except ValueError:
                    continue
                comment = str(val).strip()
                if comment:
                    turn_comments_legacy[idx] = comment

    for idx, tags in per_turn_tags.items():
        legacy_comment = turn_comments_legacy.get(idx, "")
        for i, tag in enumerate(tags):
            # Prefer the per-tag comment (new UI); fall back to legacy
            # per-turn comment attached to the first tag.
            per_tag = turn_comments_by_tag.get((idx, tag), "")
            comment = per_tag if per_tag else (legacy_comment if i == 0 else "")
            turn_tags.append({
                "turn_idx": idx,
                "tag": tag,
                "comment": comment,
            })

    # Also record any turn that has ONLY a legacy comment, no tags —
    # reviewer left free-text without checking any box (old flow only).
    for idx, comment in turn_comments_legacy.items():
        if idx not in per_turn_tags:
            turn_tags.append({
                "turn_idx": idx, "tag": "note", "comment": comment,
            })

    # Resolve tenant_id from the session row (fallback "unknown").
    session_id = _session_id_from_call_id(raw)
    sess = (
        db.query(SessionRow)
        .execution_options(allow_cross_tenant=True)
        .filter(SessionRow.id == session_id)
        .one_or_none()
    )
    tenant_id = (sess.tenant_id if sess else None) or "unknown"

    # Upsert
    ann = (
        db.query(CallAnnotation)
        .execution_options(allow_cross_tenant=True)
        .filter(CallAnnotation.call_id == raw)
        .one_or_none()
    )
    now = datetime.now(timezone.utc)
    if ann is None:
        ann = CallAnnotation(
            call_id=raw,
            tenant_id=tenant_id,
            verdict=verdict,
            turn_tags=turn_tags or None,
            is_gold=is_gold,
            notes=notes,
            reviewer_id=reviewer_id,
            created_at=now,
            updated_at=now,
        )
        db.add(ann)
    else:
        ann.verdict = verdict
        ann.turn_tags = turn_tags or None
        ann.is_gold = is_gold
        ann.notes = notes
        ann.reviewer_id = reviewer_id
        ann.updated_at = now
    db.commit()

    # Redirect back to the form so reviewer sees the saved state.
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url=f"/admin/annotate/{raw}", status_code=303)


# ─── GET /admin/annotate/{call_id}/json — machine-readable ─────────────────


@router.get("/{call_id}/json")
def get_annotation_json(
    call_id: str, request: Request, db: Session = Depends(get_session),
):
    _require_admin(request)
    raw = _raw_call_id(call_id)
    ann = (
        db.query(CallAnnotation)
        .execution_options(allow_cross_tenant=True)
        .filter(CallAnnotation.call_id == raw)
        .one_or_none()
    )
    if ann is None:
        raise HTTPException(status_code=404, detail=f"no annotation for {raw!r}")
    return {
        "call_id": ann.call_id,
        "tenant_id": ann.tenant_id,
        "verdict": ann.verdict,
        "turn_tags": ann.turn_tags,
        "auto_labels": ann.auto_labels,
        "is_gold": ann.is_gold,
        "notes": ann.notes,
        "reviewer_id": ann.reviewer_id,
        "created_at": _to_iso(ann.created_at),
        "updated_at": _to_iso(ann.updated_at),
    }
