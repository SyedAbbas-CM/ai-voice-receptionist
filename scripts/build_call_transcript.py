"""Build a human-readable transcript + latency breakdown for a call.

Combines two data sources:
  - data/call_events.db (sqlite) — the authoritative full-text agent
    utterances + STT finals, with wall_ts + turn_generation
  - apps/api/data/logs/calls/<CallSid>.log — per-call log with
    TTS_FIRST_BYTE / filler firing / TTS_STREAM_START/DONE / state
    transitions / EVENT_LOOP_LAG / ZOMBIE_SPEAKING / GATE events

Usage:
  python scripts/build_call_transcript.py <CallSid>...
  python scripts/build_call_transcript.py --all-recent
  python scripts/build_call_transcript.py --index

Writes to docs/transcripts/<CallSid>.md and updates docs/transcripts/README.md.
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DB_PATH = REPO_ROOT / "data" / "call_events.db"
LOG_DIR = REPO_ROOT / "apps" / "api" / "data" / "logs" / "calls"
OUT_DIR = REPO_ROOT / "docs" / "transcripts"


def _fetch_events(call_id_with_prefix: str) -> list[tuple]:
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.execute(
        """SELECT wall_ts, source, kind, turn_generation, payload
           FROM call_events
           WHERE call_id = ?
             AND ((source='stt' AND kind='final')
                  OR (source='tts' AND kind='utterance')
                  OR (source='llm' AND kind='reply'))
           ORDER BY wall_ts""",
        (call_id_with_prefix,),
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def _dedupe(rows: list[tuple]) -> list[tuple]:
    """STT sometimes fires 'final' twice for the same text within ~1s.
    Keep the later occurrence."""
    out = []
    for ts, source, kind, turn_gen, payload in rows:
        try:
            p = json.loads(payload)
        except Exception:
            continue
        text = (p.get("text") or "").strip()
        if not text:
            continue
        if out and source == out[-1][1] and text == out[-1][3] and ts - out[-1][0] < 3.0:
            out[-1] = (ts, source, turn_gen, text)
            continue
        out.append((ts, source, turn_gen, text))
    return out


_LOG_LINE_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3})\s+"
    r"(?P<level>\w+)\s+(?P<logger>[\w.]+):\s*(?P<msg>.*)$"
)


def _parse_log_ts(s: str) -> float:
    # Log lines use the machine's LOCAL time (per Python logging default).
    # datetime.timestamp() on a naive datetime treats it as local — exactly
    # what we want to match against wall_ts (which is epoch seconds).
    return datetime.strptime(s, "%Y-%m-%d %H:%M:%S,%f").timestamp()


def _index_per_call_log(short_id: str) -> dict:
    """Extract latency + interesting-event markers from the per-call
    log so we can annotate each transcript turn with real timings."""
    log_file = LOG_DIR / f"{short_id}.log"
    events = {
        "tts_stream_start": [],   # (ts, gen, text)
        "tts_first_byte": [],     # (ts, gen, first_byte_ms)
        "tts_stream_done": [],    # (ts, gen, total_ms, chunks)
        "filler_firing": [],      # (ts, gen, phrase)
        "zombie_speaking": [],    # ts
        "event_loop_lag": [],     # (ts, lag_ms)
        "gate_drop": [],          # (ts, gen, text)
        "stream_reply_replaced": [],  # (ts, gen, spoken, planned)
        "farewell_detected": [],  # ts
        "commit_lock_skip_speculative": [],   # ts, gen
    }
    if not log_file.exists():
        return events
    with log_file.open() as f:
        for line in f:
            m = _LOG_LINE_RE.match(line)
            if not m:
                continue
            try:
                ts = _parse_log_ts(m.group("ts"))
            except Exception:
                continue
            msg = m.group("msg")
            if "TTS_STREAM_START" in msg:
                mm = re.search(r"gen=(\d+).*?text=(['\"])(.*?)\2", msg)
                if mm:
                    events["tts_stream_start"].append((ts, int(mm.group(1)), mm.group(3)))
            elif "TTS_FIRST_BYTE" in msg:
                mm = re.search(r"gen=(\d+).*?first_byte_ms=(\d+)", msg)
                if mm:
                    events["tts_first_byte"].append((ts, int(mm.group(1)), int(mm.group(2))))
            elif "TTS_STREAM_DONE" in msg:
                mm = re.search(r"gen=(\d+).*?total_ms=(\d+)", msg)
                if mm:
                    events["tts_stream_done"].append((ts, int(mm.group(1)), int(mm.group(2))))
            elif "filler firing" in msg:
                mm = re.search(r"turn=(\d+).*?phrase=(['\"])(.*?)\2", msg)
                if mm:
                    events["filler_firing"].append((ts, int(mm.group(1)), mm.group(3)))
            elif "ZOMBIE_SPEAKING" in msg:
                events["zombie_speaking"].append(ts)
            elif "EVENT_LOOP_LAG" in msg:
                mm = re.search(r"lag_ms=([\d.]+)", msg)
                if mm:
                    events["event_loop_lag"].append((ts, float(mm.group(1))))
            elif "GATE_DROP" in msg:
                mm = re.search(r"gen=(\d+).*?text=(['\"])(.*?)\2", msg)
                if mm:
                    events["gate_drop"].append((ts, int(mm.group(1)), mm.group(3)))
            elif "STREAM_REPLY_REPLACED" in msg:
                mm = re.search(r"gen=(\d+)\s+spoken=(['\"])(.*?)\2\s+planned=(['\"])(.*?)\4", msg)
                if mm:
                    events["stream_reply_replaced"].append(
                        (ts, int(mm.group(1)), mm.group(3), mm.group(5))
                    )
            elif "FAREWELL_DETECTED" in msg:
                events["farewell_detected"].append(ts)
            elif "COMMIT_LOCK_SKIP" in msg and "reason=speculative" in msg:
                mm = re.search(r"gen=(\d+)", msg)
                if mm:
                    events["commit_lock_skip_speculative"].append((ts, int(mm.group(1))))
    return events


def _latency_for_agent_turn(
    utt_ts: float, log_events: dict, gen_hint: int | None = None,
) -> str:
    """For a `tts.utterance` event, find the closest TTS_FIRST_BYTE
    within a plausible 5s window (gen may not match because the DB's
    `turn_generation` is the caller-turn gen, not the speech gen)."""
    parts = []
    best = None
    for fb_ts, fb_gen, fb_ms in log_events["tts_first_byte"]:
        delta = fb_ts - utt_ts
        # Match on wall-time only.  first-byte lands within a few
        # hundred ms of _speak() being called; anything > 5s is a
        # different reply.
        if -1.0 < delta < 5.0:
            if best is None or abs(delta) < abs(best[0] - utt_ts):
                best = (fb_ts, fb_gen, fb_ms)
    if best is not None:
        parts.append(f"first-byte {best[2]}ms")
    # Look for filler in the 3s BEFORE this utterance.
    for f_ts, f_gen, f_phrase in log_events["filler_firing"]:
        if 0 < utt_ts - f_ts < 3.0:
            parts.append(f"filler {utt_ts - f_ts:.1f}s prior: {f_phrase!r}")
    return " · ".join(parts) if parts else ""


def _issues_for_call(log_events: dict) -> list[str]:
    """Summarize interesting call-quality issues from the log."""
    lines = []
    zc = len(log_events["zombie_speaking"])
    if zc:
        lines.append(f"- **{zc}× ZOMBIE_SPEAKING** watchdog fires (false-kills mid-reply)")
    lags = [ms for _, ms in log_events["event_loop_lag"] if ms >= 100]
    if lags:
        lines.append(f"- **{len(lags)}× EVENT_LOOP_LAG ≥ 100ms** (max {max(lags):.0f}ms)")
    gd = len(log_events["gate_drop"])
    if gd:
        lines.append(f"- **{gd}× GATE_DROP** (wait-promise spoken without matching tool call)")
    srr = len(log_events["stream_reply_replaced"])
    if srr:
        lines.append(f"- **{srr}× STREAM_REPLY_REPLACED** (spoken text got replaced mid-stream)")
    cls = len(log_events["commit_lock_skip_speculative"])
    if cls:
        lines.append(f"- **{cls}× COMMIT_LOCK_SKIP reason=speculative** (P0 #4 lock veto on LLM turns)")
    fw = len(log_events["farewell_detected"])
    if fw:
        lines.append(f"- **{fw}× FAREWELL_DETECTED** (hangup scheduler fired)")
    return lines


def _turn_latencies(deduped: list[tuple], log_events: dict) -> list[float]:
    """For every agent utterance that came AFTER a caller final,
    compute end-of-STT-final → TTS_FIRST_BYTE delta."""
    lat = []
    last_stt_ts = None
    for ts, source, gen, text in deduped:
        if source == "stt":
            last_stt_ts = ts
        elif source == "tts" and last_stt_ts is not None:
            for fb_ts, fb_gen, fb_ms in log_events["tts_first_byte"]:
                if -0.5 < fb_ts - ts < 5.0:
                    lat.append(fb_ts - last_stt_ts)
                    break
            last_stt_ts = None
    return lat


def render(short_id: str) -> Path:
    call_id = f"twilio_{short_id}"
    rows = _fetch_events(call_id)
    if not rows:
        raise SystemExit(f"no events in DB for {call_id}")
    deduped = _dedupe(rows)
    log_events = _index_per_call_log(short_id)
    t0 = deduped[0][0]
    duration = int(deduped[-1][0] - t0)

    lat = _turn_latencies(deduped, log_events)
    lat_line = ""
    if lat:
        lat_sorted = sorted(lat)
        p50 = lat_sorted[len(lat) // 2]
        p90 = lat_sorted[int(len(lat) * 0.9)]
        mn = min(lat_sorted)
        mx = max(lat_sorted)
        lat_line = (
            f"**End-of-caller → first agent audio byte:** "
            f"p50 **{p50:.2f}s** · p90 {p90:.2f}s · min {mn:.2f}s · max {mx:.2f}s "
            f"(n={len(lat)})"
        )

    issues = _issues_for_call(log_events)

    lines = [
        f"# Call transcript — `{short_id}`",
        "",
        f"**Duration:** ~{duration}s ({len(deduped)} turns)",
        f"**Started:** {datetime.fromtimestamp(t0, tz=timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
        "",
    ]
    if lat_line:
        lines += [lat_line, ""]
    if issues:
        lines += ["## Call-quality issues detected", ""] + issues + [""]

    lines += [
        "## Transcript",
        "",
        "`t=Ns` = seconds from call start.  Annotations: **first-byte** = ElevenLabs TTS latency (start of speech → audio bytes leave server).  **filler** = a filler phrase spoken while the brain was still thinking.",
        "",
        "---",
        "",
    ]

    prev_ts = None
    prev_source = None
    for ts, source, gen, text in deduped:
        rel = ts - t0
        tag = "**Agent**" if source == "tts" else "**Caller**"
        gap = ""
        if prev_ts is not None:
            gap_s = ts - prev_ts
            if gap_s > 0.8:
                arrow = "→" if source != prev_source else "…"
                gap = f" _({arrow} {gap_s:.1f}s)_"
        annot = ""
        if source == "tts":
            a = _latency_for_agent_turn(ts, log_events, gen_hint=gen)
            if a:
                annot = f"  `[{a}]`"
        lines.append(f"`t={rel:6.2f}s`  {tag}{gap}: {text}{annot}")
        prev_ts = ts
        prev_source = source

    lines += [
        "",
        "---",
        "",
        "## Raw logs",
        "",
        f"- Per-call log (event-level, ~200-1500 lines): [`apps/api/data/logs/calls/{short_id}.log`](../../apps/api/data/logs/calls/{short_id}.log)",
        f"- Full uvicorn log covering this call is in `apps/api/data/logs/uvicorn-<date>_<time>.log` — find the one whose date matches the 'Started' timestamp above.",
    ]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUT_DIR / f"{short_id}.md"
    out_path.write_text("\n".join(lines))
    return out_path


def build_index() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    md_files = list(OUT_DIR.glob("CA*.md"))
    # Parse "Started" so we can sort newest-first.
    def meta(md: Path) -> tuple[str, str]:
        started = duration = ""
        for line in md.read_text().splitlines()[:12]:
            if line.startswith("**Duration:**"):
                duration = line.replace("**Duration:**", "").strip()
            elif line.startswith("**Started:**"):
                started = line.replace("**Started:**", "").strip()
        return started, duration
    entries = [(md, *meta(md)) for md in md_files]
    entries.sort(key=lambda e: e[1], reverse=True)
    lines = [
        "# Call transcripts",
        "",
        "Generated by `scripts/build_call_transcript.py`.  Each transcript pairs the full agent + caller text (from `data/call_events.db`) with per-turn latency + call-quality annotations (from `apps/api/data/logs/calls/<CallSid>.log`).",
        "",
        "To regenerate: `python scripts/build_call_transcript.py --all-recent`",
        "",
        "## Recent calls (newest first)",
        "",
    ]
    for md, started, duration in entries:
        lines.append(f"- **[{md.stem}]({md.name})** — {started} · {duration}")
    (OUT_DIR / "README.md").write_text("\n".join(lines) + "\n")


def _all_recent_ids() -> list[str]:
    conn = sqlite3.connect(str(DB_PATH))
    cur = conn.execute(
        """SELECT DISTINCT call_id
           FROM call_events
           WHERE call_id LIKE 'twilio_CA%'
             AND length(call_id) > 20  -- skip 'twilio_CA-t1' test noise
             AND wall_ts > 1787000000
           ORDER BY wall_ts DESC
           LIMIT 25"""
    )
    ids = [row[0] for row in cur.fetchall()]
    conn.close()
    return [i.replace("twilio_", "") for i in ids]


def main(argv: list[str]) -> None:
    p = argparse.ArgumentParser()
    p.add_argument("call_sids", nargs="*")
    p.add_argument("--all-recent", action="store_true")
    p.add_argument("--index", action="store_true")
    args = p.parse_args(argv)

    ids = list(args.call_sids)
    if args.all_recent:
        ids.extend(_all_recent_ids())
    ids = list(dict.fromkeys(ids))  # dedupe, preserve order

    if not ids and not args.index:
        p.error("give at least one CallSid or --all-recent or --index")

    for sid in ids:
        try:
            out = render(sid)
            print(f"wrote {out.relative_to(REPO_ROOT)}")
        except SystemExit as e:
            print(f"skip {sid}: {e}", file=sys.stderr)

    build_index()
    print(f"wrote {(OUT_DIR / 'README.md').relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main(sys.argv[1:])
