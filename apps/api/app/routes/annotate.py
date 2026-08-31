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

    # ─── Redesigned index (task #104-followup, 2026-08-31) ────────────
    # Editorial layout matching the annotator's vermilion/warm-paper
    # palette. Stats strip up top (total / reviewed / gold / unreviewed),
    # then a proper table with row hover + verdict pills. Theme-aware.

    # Compute stats for the top strip.
    total = len(sessions)
    reviewed = sum(1 for s in sessions if annotations.get(_raw_call_id(s.id)))
    gold_count = sum(
        1 for s in sessions
        if (a := annotations.get(_raw_call_id(s.id))) and a.is_gold
    )
    unreviewed = total - reviewed
    win_count = sum(
        1 for s in sessions
        if (a := annotations.get(_raw_call_id(s.id))) and a.verdict == "win"
    )
    fail_count = sum(
        1 for s in sessions
        if (a := annotations.get(_raw_call_id(s.id))) and a.verdict == "fail"
    )

    rows_html = []
    for s in sessions:
        cid = _raw_call_id(s.id)
        ann = annotations.get(cid)
        verdict = ann.verdict if ann else "unreviewed"
        gold = '<span class="gold" title="Gold reference">★</span>' if (ann and ann.is_gold) else ""
        verdict_class = {
            "win": "pill pill-win",
            "fail": "pill pill-fail",
            "mixed": "pill pill-mixed",
            "unreviewed": "pill pill-none",
        }.get(verdict, "pill pill-none")
        started = _to_iso(s.started_at) or ""
        # Short CallSid for readability — full one in title tooltip.
        short_cid = cid[:12] + "…" if len(cid) > 12 else cid
        # Approximate call length from tenant_id column not available here;
        # show tenant + started date/time only.
        started_date = started[:10] if started else ""
        started_time = started[11:19] if started else ""
        rows_html.append(
            f'<tr onclick="location.href=\'/admin/annotate/{html.escape(cid)}\'" style="cursor:pointer">'
            f'<td class="cid" title="{html.escape(cid)}"><code>{html.escape(short_cid)}</code></td>'
            f'<td class="tenant">{html.escape(s.tenant_id or "?")}</td>'
            f'<td class="ts"><span class="date">{html.escape(started_date)}</span> '
            f'<span class="time">{html.escape(started_time)}</span></td>'
            f'<td class="verdict"><span class="{verdict_class}">{html.escape(verdict)}</span>{gold}</td>'
            f'<td class="actions">'
            f'<a href="/admin/annotate/{html.escape(cid)}" onclick="event.stopPropagation()">review →</a>'
            f' <a class="secondary" href="/trace/{html.escape(cid)}" target="_blank" '
            f'onclick="event.stopPropagation()" title="Humanness event timeline">trace ↗</a>'
            f'</td>'
            f'</tr>'
        )

    body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Call review</title>
<style>
  :root {{
    --paper: #f7f5ef;
    --ink: #1a1a1a;
    --ink-2: #4a4a4a;
    --ink-3: #7a7a7a;
    --rule: #d9d3c4;
    --card: #ffffff;
    --card-2: #fbf9f2;
    --accent: #b8360f;
    --accent-soft: #f2ddd4;
    --good: #2f6b2a;
    --warn: #a67300;
    --bad: #a11b1b;
    --hover: #f2ecd9;
  }}
  @media (prefers-color-scheme: dark) {{
    :root:not([data-theme="light"]) {{
      --paper: #14140f;
      --ink: #f0ece0;
      --ink-2: #b8b3a3;
      --ink-3: #7a7568;
      --rule: #2f2c22;
      --card: #1c1a15;
      --card-2: #17150f;
      --accent: #e46540;
      --accent-soft: #3a1f14;
      --good: #6ec665;
      --warn: #d9a63a;
      --bad: #e26a6a;
      --hover: #22201a;
    }}
  }}
  html {{ color-scheme: light dark; }}
  * {{ box-sizing: border-box; }}
  body {{
    background: var(--paper);
    color: var(--ink);
    font-family: "Charter", "Iowan Old Style", "Georgia", "Cambria", serif;
    font-size: 15px;
    line-height: 1.5;
    margin: 0;
    padding: 40px 32px;
    -webkit-font-smoothing: antialiased;
  }}
  .wrap {{ max-width: 1080px; margin: 0 auto; }}

  header.mast {{
    border-bottom: 3px double var(--ink);
    padding-bottom: 16px;
    margin-bottom: 24px;
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 16px;
    flex-wrap: wrap;
  }}
  header.mast h1 {{
    font-family: "Playfair Display", "Didot", "Bodoni 72", serif;
    font-size: 34px;
    font-weight: 900;
    letter-spacing: -0.01em;
    margin: 0;
    line-height: 1;
  }}
  header.mast .kicker {{
    font-family: "SF Mono", "IBM Plex Mono", "Menlo", monospace;
    font-size: 10px;
    letter-spacing: 0.22em;
    text-transform: uppercase;
    color: var(--ink-3);
    display: block;
    margin-bottom: 6px;
  }}
  header.mast .meta {{
    font-family: "SF Mono", "IBM Plex Mono", monospace;
    font-size: 10px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--ink-2);
    text-align: right;
    line-height: 1.7;
  }}
  header.mast .meta strong {{ color: var(--accent); }}

  /* Stats strip */
  .stats {{
    display: grid;
    grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
    gap: 0;
    border: 1px solid var(--rule);
    background: var(--card);
    margin-bottom: 28px;
    border-radius: 2px;
    overflow: hidden;
  }}
  .stat {{
    padding: 16px 20px;
    border-right: 1px solid var(--rule);
  }}
  .stat:last-child {{ border-right: none; }}
  .stat .n {{
    font-family: "Playfair Display", "Didot", serif;
    font-size: 28px;
    font-weight: 900;
    line-height: 1;
    display: block;
    color: var(--ink);
  }}
  .stat.accent .n {{ color: var(--accent); }}
  .stat.win .n {{ color: var(--good); }}
  .stat.fail .n {{ color: var(--bad); }}
  .stat .label {{
    font-family: "SF Mono", monospace;
    font-size: 10px;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    color: var(--ink-3);
    margin-top: 6px;
    display: block;
  }}

  /* Section title */
  .section-title {{
    font-family: "SF Mono", monospace;
    font-size: 10px;
    letter-spacing: 0.22em;
    text-transform: uppercase;
    color: var(--ink-3);
    margin: 0 0 10px;
    padding-bottom: 8px;
    border-bottom: 1px solid var(--rule);
  }}
  .section-title .count {{
    color: var(--accent);
    font-weight: 700;
    margin-left: 8px;
  }}

  /* Table */
  table {{
    border-collapse: collapse;
    width: 100%;
    background: var(--card);
    border: 1px solid var(--rule);
    border-radius: 2px;
    overflow: hidden;
  }}
  thead th {{
    text-align: left;
    padding: 12px 16px;
    background: var(--card-2);
    border-bottom: 1px solid var(--rule);
    font-family: "SF Mono", monospace;
    font-size: 10px;
    letter-spacing: 0.16em;
    text-transform: uppercase;
    color: var(--ink-3);
    font-weight: 600;
  }}
  tbody td {{
    padding: 12px 16px;
    border-bottom: 1px solid var(--rule);
    font-size: 14px;
  }}
  tbody tr:last-child td {{ border-bottom: none; }}
  tbody tr:hover {{ background: var(--hover); }}
  tbody tr:hover .actions a:first-child {{ color: var(--accent); text-decoration: underline; }}

  td.cid code {{
    font-family: "SF Mono", "IBM Plex Mono", monospace;
    font-size: 12px;
    color: var(--ink-2);
  }}
  td.tenant {{
    font-family: "SF Mono", monospace;
    font-size: 12px;
    color: var(--ink-2);
    letter-spacing: 0.05em;
  }}
  td.ts .date {{
    font-family: "SF Mono", monospace;
    font-size: 12px;
    color: var(--ink);
    font-variant-numeric: tabular-nums;
  }}
  td.ts .time {{
    font-family: "SF Mono", monospace;
    font-size: 11px;
    color: var(--ink-3);
    font-variant-numeric: tabular-nums;
    margin-left: 4px;
  }}

  /* Verdict pills */
  .pill {{
    display: inline-block;
    padding: 2px 10px;
    border-radius: 12px;
    font-family: "SF Mono", monospace;
    font-size: 10px;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    font-weight: 700;
  }}
  .pill-win {{ background: rgba(47, 107, 42, 0.12); color: var(--good); }}
  .pill-fail {{ background: rgba(161, 27, 27, 0.12); color: var(--bad); }}
  .pill-mixed {{ background: rgba(166, 115, 0, 0.12); color: var(--warn); }}
  .pill-none {{
    background: transparent;
    color: var(--ink-3);
    border: 1px dashed var(--rule);
    font-weight: 500;
  }}
  .gold {{
    color: var(--accent);
    font-size: 14px;
    margin-left: 6px;
    vertical-align: -1px;
  }}

  /* Actions */
  td.actions {{ text-align: right; white-space: nowrap; }}
  td.actions a {{
    color: var(--ink);
    text-decoration: none;
    font-family: "SF Mono", monospace;
    font-size: 11px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    font-weight: 600;
  }}
  td.actions a:hover {{ color: var(--accent); }}
  td.actions a.secondary {{
    color: var(--ink-3);
    font-weight: 500;
    margin-left: 12px;
  }}

  /* Empty state */
  .empty {{
    padding: 60px 20px;
    text-align: center;
    color: var(--ink-3);
    font-style: italic;
  }}

  /* Colophon */
  .colophon {{
    margin-top: 48px;
    padding-top: 16px;
    border-top: 3px double var(--ink);
    font-family: "SF Mono", monospace;
    font-size: 10px;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--ink-3);
    display: flex;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 12px;
  }}
  .colophon strong {{ color: var(--accent); }}

  @media (max-width: 640px) {{
    body {{ padding: 24px 16px; }}
    header.mast {{ flex-direction: column; align-items: flex-start; }}
    header.mast .meta {{ text-align: left; }}
    header.mast h1 {{ font-size: 26px; }}
    td.cid {{ max-width: 100px; overflow: hidden; text-overflow: ellipsis; }}
    td.actions a.secondary {{ display: none; }}
  }}
</style></head><body>
<div class="wrap">

<header class="mast">
  <div>
    <span class="kicker">Reviewer console · Aug 2026</span>
    <h1>Call review</h1>
  </div>
  <div class="meta">
    <strong>agent.eternalconquests.com</strong><br>
    Multi-tenant · Cross-tenant read
  </div>
</header>

<section class="stats">
  <div class="stat"><span class="n">{total}</span><span class="label">Total calls</span></div>
  <div class="stat accent"><span class="n">{reviewed}</span><span class="label">Reviewed</span></div>
  <div class="stat"><span class="n">{unreviewed}</span><span class="label">Unreviewed</span></div>
  <div class="stat win"><span class="n">{win_count}</span><span class="label">Wins</span></div>
  <div class="stat fail"><span class="n">{fail_count}</span><span class="label">Fails</span></div>
  <div class="stat accent"><span class="n">{gold_count}</span><span class="label">★ Gold refs</span></div>
</section>

<p class="section-title">Recent calls <span class="count">{total}</span> · click any row to review</p>

<table>
<thead>
<tr>
  <th>CallSid</th>
  <th>Tenant</th>
  <th>Started (UTC)</th>
  <th>Verdict</th>
  <th></th>
</tr>
</thead>
<tbody>
{"".join(rows_html) if rows_html else '<tr><td colspan="5" class="empty">No calls yet. Place a test call and it will appear here.</td></tr>'}
</tbody>
</table>

<div class="colophon">
  <span>VoiceOps · <strong>reviewer</strong></span>
  <span>Every call becomes training data.</span>
  <span>Set in Playfair Display &amp; SF Mono.</span>
</div>

</div>
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

    # v2 UI (task #104 iteration, 2026-08-31): WhatsApp-style chat
    # bubbles + agent-only annotation + persistent plain-text notes bar.
    # Full template lives in annotate_form_template.py for testability.
    from app.routes.annotate_form_template import render_form_html
    body = render_form_html(
        call_id_raw=raw,
        tenant_label=(sess.tenant_id if sess else "?") or "?",
        call_started=_to_iso(sess.started_at) if sess else "?",
        turn_count=len(turns),
        turns_json=turns_json,
        tag_vocab_json=tag_vocab_json,
        tag_lookup_json=tag_lookup_json,
        existing_verdict=existing_verdict,
        existing_is_gold=existing_is_gold,
        notes_val=notes_val,
        reviewer_val=reviewer_val,
    )
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
