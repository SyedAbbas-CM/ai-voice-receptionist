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
            f'<td><a href="/admin/annotate/{html.escape(cid)}">annotate</a></td>'
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

    # Build turn rows
    turn_rows_html = []
    for i, t in enumerate(turns):
        role_tag = "caller" if t.role == "user" else "agent" if t.role == "assistant" else t.role
        role_class = f"role-{role_tag}"
        text = html.escape(t.text or "")
        ts_short = _to_iso(t.timestamp) or ""
        ts_short = ts_short[11:19] if len(ts_short) > 19 else ts_short

        # Tag checkboxes for this turn
        tag_html_bits = []
        this_turn_tags = tag_lookup.get(i, {})
        for tag_key, tag_label in _TAG_VOCAB:
            checked = "checked" if tag_key in this_turn_tags else ""
            tag_html_bits.append(
                f'<label class="tag-chip"><input type="checkbox" '
                f'name="tag_{i}_{tag_key}" value="1" {checked}> '
                f'{html.escape(tag_label)}</label>'
            )
        # Per-turn comment
        existing_comment = ""
        if this_turn_tags:
            # If multiple tags for this turn have comments, join
            comments = [c for c in this_turn_tags.values() if c]
            existing_comment = " | ".join(comments)
        comment_html = (
            f'<input type="text" name="comment_{i}" '
            f'placeholder="optional comment for this turn" '
            f'value="{html.escape(existing_comment)}" class="comment-in">'
        )

        turn_rows_html.append(f"""
<div class="turn {role_class}">
  <div class="turn-head">
    <span class="turn-idx">#{i}</span>
    <span class="turn-role">{role_tag.upper()}</span>
    <span class="turn-ts">{html.escape(ts_short)}</span>
  </div>
  <div class="turn-text">{text}</div>
  <div class="turn-tags">{"".join(tag_html_bits)}</div>
  <div class="turn-comment">{comment_html}</div>
</div>""")

    # Verdict radio
    verdict_html_bits = []
    for v in ("win", "fail", "mixed", "unreviewed"):
        checked = "checked" if existing_verdict == v else ""
        verdict_html_bits.append(
            f'<label><input type="radio" name="verdict" value="{v}" {checked}> {v}</label>'
        )
    gold_checked = "checked" if existing_is_gold else ""

    body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Annotate {html.escape(raw)}</title>
<style>
  body {{ font-family: -apple-system, sans-serif; margin: 2em; max-width: 900px; }}
  h1 {{ font-size: 1.2em; margin-bottom: 0.2em; }}
  .header-meta {{ color: #666; font-size: 0.9em; margin-bottom: 1.5em; }}
  .turn {{ padding: 0.6em 0.8em; margin-bottom: 0.6em; border-radius: 6px; border: 1px solid #eee; }}
  .role-caller {{ background: #f4f7ff; }}
  .role-agent  {{ background: #fdfbf4; }}
  .role-tool   {{ background: #f5f5f5; }}
  .turn-head {{ font-size: 0.85em; color: #666; margin-bottom: 0.3em; }}
  .turn-idx {{ font-weight: 600; margin-right: 0.5em; }}
  .turn-role {{ display: inline-block; padding: 0 0.4em; background: rgba(0,0,0,0.06); border-radius: 3px; margin-right: 0.5em; }}
  .turn-ts {{ font-family: monospace; }}
  .turn-text {{ margin: 0.3em 0; }}
  .turn-tags {{ margin: 0.4em 0 0.3em; }}
  .tag-chip {{ display: inline-block; margin-right: 0.5em; font-size: 0.85em; padding: 0.1em 0.3em; cursor: pointer; }}
  .tag-chip input {{ vertical-align: middle; }}
  .comment-in {{ width: 100%; padding: 0.3em; font-size: 0.85em; border: 1px solid #ddd; border-radius: 4px; }}
  .footer {{ position: sticky; bottom: 0; background: white; padding: 1em; margin-top: 2em; border-top: 2px solid #333; }}
  .verdict-row label {{ margin-right: 1.5em; }}
  textarea {{ width: 100%; min-height: 4em; padding: 0.5em; font-family: inherit; }}
  button {{ padding: 0.5em 1.5em; font-size: 1em; margin-right: 0.5em; cursor: pointer; }}
  .btn-primary {{ background: #0a7c3a; color: white; border: none; border-radius: 4px; }}
  .btn-gold {{ background: #f5d000; border: 1px solid #d4b000; border-radius: 4px; }}
</style></head><body>
<h1>Annotate: <code>{html.escape(raw)}</code></h1>
<div class="header-meta">
  Tenant: <b>{html.escape((sess.tenant_id if sess else "?") or "?")}</b> ·
  Started: {html.escape(_to_iso(sess.started_at) if sess else "?")} ·
  {len(turns)} turns
  · <a href="/admin/annotate">← index</a>
</div>

<form method="POST" action="/admin/annotate/{html.escape(raw)}/save">
  {"".join(turn_rows_html)}

  <div class="footer">
    <div class="verdict-row">
      <b>Verdict:</b> {"".join(verdict_html_bits)}
      <label style="margin-left: 2em;"><input type="checkbox" name="is_gold" value="1" {gold_checked}> ⭐ mark as gold (regression corpus)</label>
    </div>
    <div style="margin-top: 0.5em;">
      <b>Notes:</b><br>
      <textarea name="notes" placeholder="Long-form notes about this call — what went wrong, what to fix, ideas for training data">{html.escape(existing_notes or "")}</textarea>
    </div>
    <div style="margin-top: 0.5em;">
      <b>Reviewer:</b>
      <input type="text" name="reviewer_id" placeholder="your name or email"
             value="{html.escape((ann.reviewer_id if ann else "") or "")}" style="padding: 0.3em;">
    </div>
    <div style="margin-top: 1em;">
      <button type="submit" class="btn-primary">Save annotation</button>
    </div>
  </div>
</form>
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

    # Reconstruct turn_tags from form: any `tag_{i}_{tag_key}=1` field
    # means that turn was tagged with that key. The associated
    # `comment_{i}` (if non-empty) becomes the comment on the FIRST
    # tag of that turn — good enough for v1; per-tag comments can come
    # later if needed.
    turn_tags: list[dict] = []
    turn_comments: dict[int, str] = {}
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
            try:
                idx = int(field_name[len("comment_"):])
            except ValueError:
                continue
            comment = str(val).strip()
            if comment:
                turn_comments[idx] = comment

    for idx, tags in per_turn_tags.items():
        comment = turn_comments.get(idx, "")
        # Emit one entry per tag. Comment only attaches to the first
        # (matches the v1 form contract — reviewer can split later).
        for i, tag in enumerate(tags):
            turn_tags.append({
                "turn_idx": idx,
                "tag": tag,
                "comment": comment if i == 0 else "",
            })

    # Also record any turn that has ONLY a comment, no tags — reviewer
    # left free-text without checking any box.
    for idx, comment in turn_comments.items():
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
