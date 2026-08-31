"""HTML template for /admin/annotate/{call_id} — task #104 v2.

Kept in a separate module so annotate.py stays readable + the template
can be tested in isolation. All values interpolated at call time via
`render_form_html(...)`.

## v2 UI notes (2026-08-31)

Iterated on user feedback: "designed better like WhatsApp caller on
left agent on right you can only label agent replies and i want pure
text also available for the bottom."

- Chat bubbles: caller left (blue), agent right (tan). WhatsApp shape.
- ONLY agent turns are clickable / annotatable. Caller turns are
  context — you can't tag what the human said, only how the agent
  responded to it.
- Tool calls render inline as compact "system" chips between bubbles.
- Bottom bar always shows a plain textarea labeled "Notes (plain
  text, only you see this)" — no modal, always visible.
- Save bar stays sticky at the bottom.
- Keyboard: j/k step agent turns, 1-9 toggle tag N, w/f/m verdict,
  g gold, cmd-s save.
"""
from __future__ import annotations

import html
from string import Template


TAG_VOCAB = [
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


# CSS + HTML kept as module constants so f-string interpolation is
# purely template variables; the template body doesn't need f-string
# escaping of literal braces.

_CSS = r"""
/* Color system: "audit console" — neutral warm-paper ground with a
 * deep vermilion accent. WhatsApp layout, NOT WhatsApp colors.
 * User pushback (2026-08-31): "i said the FORMAT of whatsapp not
 * literally whatsapp colors." */
:root {
  --ink: #1a1a17;
  --ink-2: #4a4a44;
  --ink-3: #8a8a80;
  --paper: #f6f4ee;
  --panel: #ffffff;
  --rule: #e6e3dc;
  --caller-bubble: #ffffff;
  --caller-ink: #1a1a17;
  --caller-shadow: 0 1px 2px rgba(26,26,23,0.06);
  --agent-bubble: #eef1f7;
  --agent-ink: #1a1a17;
  --agent-shadow: 0 1px 2px rgba(26,26,23,0.06);
  --agent-bubble-focused: #f2ddd4;
  --tool-band: rgba(26,26,23,0.05);
  --tool-ink: #6a6a60;
  --accent: #b8360f;
  --accent-hover: #a02e0d;
  --good: #2d6a4f;
  --warn: #b45309;
  --danger: #b8360f;
  --gold: #d4a017;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --ink: #eaeae4;
    --ink-2: #b0b0a8;
    --ink-3: #808078;
    --paper: #1a1a17;
    --panel: #22221e;
    --rule: #33332e;
    --caller-bubble: #2a2a26;
    --caller-ink: #eaeae4;
    --caller-shadow: 0 1px 2px rgba(0,0,0,0.3);
    --agent-bubble: #2b3140;
    --agent-ink: #eaeae4;
    --agent-shadow: 0 1px 2px rgba(0,0,0,0.3);
    --agent-bubble-focused: #4a2418;
    --tool-band: rgba(255,255,255,0.04);
    --tool-ink: #a09e94;
    --accent: #e46744;
    --accent-hover: #f07e5c;
    --gold: #d4a017;
  }
}
:root[data-theme="dark"] {
  --ink: #eaeae4;
  --ink-2: #b0b0a8;
  --ink-3: #808078;
  --paper: #1a1a17;
  --panel: #22221e;
  --rule: #33332e;
  --caller-bubble: #2a2a26;
  --caller-ink: #eaeae4;
  --caller-shadow: 0 1px 2px rgba(0,0,0,0.3);
  --agent-bubble: #2b3140;
  --agent-ink: #eaeae4;
  --agent-shadow: 0 1px 2px rgba(0,0,0,0.3);
  --agent-bubble-focused: #4a2418;
  --tool-band: rgba(255,255,255,0.04);
  --tool-ink: #a09e94;
  --accent: #e46744;
  --accent-hover: #f07e5c;
  --gold: #d4a017;
}

* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; background: var(--paper); color: var(--ink); }
body {
  font-family: -apple-system, "Segoe UI Emoji", "Segoe UI", "Helvetica Neue", Helvetica, Arial, sans-serif;
  font-size: 14.2px;
  line-height: 1.4;
  -webkit-font-smoothing: antialiased;
}
.mono { font-family: 'JetBrains Mono', 'SF Mono', Menlo, Consolas, monospace; }

/* ─── Layout: header, chat pane, tag panel, notes bar, save bar ─── */
.app {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 320px;
  grid-template-rows: auto minmax(0, 1fr) auto auto;
  grid-template-areas:
    "hdr    hdr"
    "chat   panel"
    "notes  panel"
    "save   save";
  height: 100vh;
}
@media (max-width: 820px) {
  .app {
    grid-template-columns: 1fr;
    grid-template-areas: "hdr" "chat" "panel" "notes" "save";
    height: auto;
  }
}

/* ─── Header ─── */
.hdr {
  grid-area: hdr;
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 10px 16px;
  background: var(--panel);
  border-bottom: 1px solid var(--rule);
  min-height: 56px;
}
.hdr .title { font-weight: 500; font-size: 16px; }
.hdr .cid { font-family: 'JetBrains Mono', monospace; font-size: 11px; color: var(--ink-3); }
.hdr .meta { color: var(--ink-3); font-size: 12px; }
.hdr .meta b { color: var(--ink-2); font-weight: 500; }
.hdr .spacer { flex: 1; }
.hdr a {
  color: var(--ink-2);
  text-decoration: none;
  font-size: 12px;
  padding: 4px 8px;
  border-radius: 4px;
  transition: background 0.1s;
}
.hdr a:hover { background: var(--tool-band); color: var(--accent); }

/* ─── Audio player (under header, sits above chat) ─── */
.player {
  grid-column: 1 / -1;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 16px;
  background: var(--panel);
  border-bottom: 1px solid var(--rule);
}
.player-label {
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  letter-spacing: 0.16em;
  text-transform: uppercase;
  color: var(--ink-3);
  min-width: 72px;
}
.player audio {
  flex: 1;
  height: 32px;
  max-width: 720px;
}
.player-dur {
  font-family: 'JetBrains Mono', monospace;
  font-size: 12px;
  color: var(--ink-2);
  font-variant-numeric: tabular-nums;
  min-width: 44px;
  text-align: right;
}
.player-dl {
  color: var(--ink-3);
  text-decoration: none;
  font-size: 16px;
  padding: 4px 10px;
  border-radius: 4px;
  transition: background 0.1s;
}
.player-dl:hover { background: var(--tool-band); color: var(--accent); }

/* ─── Chat pane ─── */
.chat {
  grid-area: chat;
  background: var(--paper);
  overflow-y: auto;
  padding: 20px 16px 8px;
}
.bubble-row {
  display: flex;
  margin-bottom: 10px;
  padding: 0 4px;
}
.bubble-row.caller { justify-content: flex-start; }
.bubble-row.agent  { justify-content: flex-end; }
.bubble {
  max-width: 68%;
  padding: 8px 12px 22px;
  border-radius: 8px;
  position: relative;
  box-shadow: var(--caller-shadow);
  word-wrap: break-word;
}
.bubble.caller {
  background: var(--caller-bubble);
  color: var(--caller-ink);
  border-top-left-radius: 0;
}
.bubble.agent  {
  background: var(--agent-bubble);
  color: var(--agent-ink);
  border-top-right-radius: 0;
  cursor: pointer;
  transition: box-shadow 0.1s, background 0.15s;
}
.bubble.agent:hover { box-shadow: 0 2px 6px rgba(0,0,0,0.1); }
.bubble.agent.focused {
  background: var(--agent-bubble-focused);
  box-shadow: 0 0 0 2px var(--accent), 0 2px 6px rgba(0,0,0,0.15);
}
.bubble .text { color: var(--ink); white-space: pre-wrap; }
.bubble .meta {
  position: absolute;
  right: 8px;
  bottom: 4px;
  font-size: 10px;
  color: var(--ink-3);
  font-family: 'JetBrains Mono', monospace;
  user-select: none;
}
.bubble .idx {
  margin-right: 6px;
  opacity: 0.7;
}
.bubble .badges {
  margin-top: 6px;
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.bubble .badge {
  display: inline-block;
  font-size: 10px;
  padding: 2px 6px;
  background: var(--accent);
  color: white;
  border-radius: 10px;
  font-weight: 500;
}
.bubble .badge.err { background: var(--danger); }

/* ─── Tool / system row ─── */
.tool-row {
  display: flex;
  justify-content: center;
  margin: 8px 0;
}
.tool-chip {
  max-width: 80%;
  padding: 5px 12px;
  background: var(--tool-band);
  color: var(--tool-ink);
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
  border-radius: 8px;
  cursor: pointer;
}
.tool-chip:hover { background: rgba(11,20,26,0.09); }
.tool-details {
  display: none;
  max-width: 92%;
  margin: 4px auto 12px;
  padding: 8px 12px;
  background: var(--panel);
  border: 1px solid var(--rule);
  border-radius: 6px;
  font-family: 'JetBrains Mono', monospace;
  font-size: 11px;
  white-space: pre-wrap;
  word-break: break-all;
  color: var(--ink-2);
}
.tool-chip.expanded + .tool-details { display: block; }

/* ─── Right panel: tag console ─── */
.panel {
  grid-area: panel;
  background: var(--panel);
  border-left: 1px solid var(--rule);
  overflow-y: auto;
  padding: 18px 16px 20px;
}
.panel-hd {
  font-size: 14px;
  font-weight: 500;
  margin: 0 0 3px;
  color: var(--ink);
}
.panel-sub {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--ink-3);
  margin-bottom: 12px;
}
.panel-empty {
  color: var(--ink-3);
  font-size: 13px;
  padding: 20px 0;
  font-style: italic;
  line-height: 1.6;
}
.panel-empty .mono {
  background: var(--tool-band);
  padding: 1px 5px;
  border-radius: 3px;
  font-style: normal;
}
.tag {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 7px 10px;
  border-radius: 4px;
  cursor: pointer;
  transition: background 0.1s;
  color: var(--ink);
}
.tag:hover { background: var(--tool-band); }
.tag input { margin: 0; accent-color: var(--accent); cursor: pointer; }
.tag .label-text { flex: 1; font-size: 13px; }
.tag .kbd {
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  padding: 1px 5px;
  background: var(--tool-band);
  border: 1px solid var(--rule);
  border-radius: 3px;
  color: var(--ink-3);
}

/* ─── Notes bar (always visible, plain text) ─── */
.notes-bar {
  grid-area: notes;
  background: var(--panel);
  border-top: 1px solid var(--rule);
  padding: 10px 16px;
}
.notes-bar label {
  display: block;
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: var(--ink-3);
  margin-bottom: 4px;
}
.notes-bar textarea {
  width: 100%;
  min-height: 44px;
  max-height: 120px;
  padding: 8px 10px;
  font: inherit;
  font-size: 13px;
  color: var(--ink);
  background: var(--paper);
  border: 1px solid var(--rule);
  border-radius: 6px;
  resize: vertical;
}
.notes-bar textarea:focus {
  outline: none;
  border-color: var(--accent);
}

/* ─── Save bar (sticky bottom) ─── */
.save {
  grid-area: save;
  background: var(--panel);
  border-top: 1px solid var(--rule);
  padding: 10px 16px;
  display: flex;
  align-items: center;
  gap: 12px;
  flex-wrap: wrap;
}
.v-group {
  display: flex;
  gap: 4px;
  padding: 2px;
  background: var(--tool-band);
  border-radius: 6px;
}
.v-btn {
  padding: 4px 10px;
  font: inherit;
  font-size: 12px;
  color: var(--ink-2);
  background: transparent;
  border: none;
  border-radius: 4px;
  cursor: pointer;
  text-transform: lowercase;
}
.v-btn:hover { color: var(--ink); }
.v-btn.active {
  background: var(--panel);
  color: var(--ink);
  box-shadow: 0 1px 2px rgba(0,0,0,0.08);
}
.v-btn.active[data-v="win"]   { color: var(--good); }
.v-btn.active[data-v="fail"]  { color: var(--danger); }
.v-btn.active[data-v="mixed"] { color: var(--warn); }
.gold-toggle {
  display: flex;
  align-items: center;
  gap: 6px;
  padding: 5px 10px;
  font-size: 12px;
  color: var(--ink-2);
  border: 1px solid var(--rule);
  background: transparent;
  border-radius: 6px;
  cursor: pointer;
}
.gold-toggle.on {
  background: rgba(245, 166, 35, 0.15);
  border-color: var(--gold);
  color: var(--gold);
}
.reviewer-in {
  padding: 5px 10px;
  font: inherit;
  font-size: 12px;
  color: var(--ink);
  background: var(--paper);
  border: 1px solid var(--rule);
  border-radius: 6px;
  width: 160px;
}
.reviewer-in::placeholder { color: var(--ink-3); }
.spacer { flex: 1; }
.save-btn {
  padding: 7px 18px;
  font: inherit;
  font-size: 13px;
  font-weight: 500;
  color: white;
  background: var(--accent);
  border: none;
  border-radius: 6px;
  cursor: pointer;
}
.save-btn:hover { background: var(--accent-hover); }
.hint {
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  color: rgba(255,255,255,0.7);
  margin-left: 6px;
}

/* ─── Toast ─── */
.toast {
  position: fixed;
  bottom: 100px;
  left: 50%;
  transform: translateX(-50%);
  background: var(--ink);
  color: var(--paper);
  padding: 8px 16px;
  border-radius: 20px;
  font-size: 12px;
  opacity: 0;
  transition: opacity 0.2s;
  pointer-events: none;
  z-index: 100;
}
.toast.show { opacity: 1; }
"""


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Review · $call_id_short…</title>
<style>$css</style>
</head>
<body>
<form method="POST" action="/admin/annotate/$call_id_esc/save" id="annotate-form">

<div class="app">

  <!-- Header -->
  <header class="hdr">
    <span class="title">$caller_label</span>
    <span class="cid">$call_id_short…</span>
    <span class="meta"><b>$tenant_label</b> · $call_started_short · $turn_count turns</span>
    <span class="spacer"></span>
    <a href="/admin/annotate">← all calls</a>
    <a href="/trace/$call_id_esc" target="_blank">humanness ↗</a>
    <a href="#" onclick="document.cookie='voiceops_admin=; Max-Age=0; path=/'; location='/admin/login'; return false;">sign out</a>
  </header>

  $recording_html

  <!-- Chat pane -->
  <main class="chat" id="chat"></main>

  <!-- Tag panel (right) -->
  <aside class="panel" id="panel">
    <div class="panel-empty" id="panel-empty">
      Click an <b>agent</b> reply on the left to tag what went wrong (or right).
      <br><br>
      Caller lines are context — you tag how the agent responded, not what the human said.
      <br><br>
      <span class="mono">j</span> / <span class="mono">k</span> next / prev agent reply<br>
      <span class="mono">1</span>–<span class="mono">9</span> toggle tag<br>
      <span class="mono">w</span> / <span class="mono">f</span> / <span class="mono">m</span> verdict<br>
      <span class="mono">g</span> gold · <span class="mono">⌘S</span> save
    </div>
    <div id="panel-content" style="display:none">
      <div class="panel-hd" id="panel-hd">Agent turn #0</div>
      <div class="panel-sub" id="panel-sub">tag issues below</div>
      <div id="tags"></div>
    </div>
  </aside>

  <!-- Notes bar (always visible, plain text) -->
  <div class="notes-bar">
    <label>Notes (plain text — only you see this)</label>
    <textarea name="notes" id="notes-input" placeholder="Long-form notes about this call — what went wrong, what to fix, ideas for training data.">$notes_val</textarea>
  </div>

  <!-- Save bar -->
  <footer class="save">
    <div class="v-group">
      <button type="button" class="v-btn" data-v="win">win</button>
      <button type="button" class="v-btn" data-v="fail">fail</button>
      <button type="button" class="v-btn" data-v="mixed">mixed</button>
      <button type="button" class="v-btn" data-v="unreviewed">unreviewed</button>
    </div>
    <button type="button" class="gold-toggle" id="gold-toggle">
      <input type="checkbox" name="is_gold" value="1" id="is-gold-input" style="display:none">
      <span>★</span> <span>gold reference</span>
    </button>
    <input type="text" name="reviewer_id" class="reviewer-in" id="reviewer-input" placeholder="Your name" value="$reviewer_val">
    <span class="spacer"></span>
    <button type="submit" class="save-btn">Save review<span class="hint">⌘S</span></button>
  </footer>

</div>

<input type="hidden" name="verdict" value="$existing_verdict" id="verdict-input">
<div class="toast" id="toast">Saved</div>

</form>

<script>
  const TURNS = $turns_json;
  const TAG_VOCAB = $tag_vocab_json;
  const EXISTING_TAGS = $tag_lookup_json;

  // ONLY agent turns are annotatable. Rail nav also filters to agent-only.
  const AGENT_INDICES = TURNS.filter(t => t.role === 'agent').map(t => t.idx);
  let focusIdx = null;

  const state = {};
  TURNS.forEach(t => {
    const existing = EXISTING_TAGS[String(t.idx)] || {};
    state[t.idx] = { tags: { ...existing } };
  });

  const chat = document.getElementById('chat');
  const panelEmpty = document.getElementById('panel-empty');
  const panelContent = document.getElementById('panel-content');
  const panelHd = document.getElementById('panel-hd');
  const panelSub = document.getElementById('panel-sub');
  const tagsEl = document.getElementById('tags');
  const toast = document.getElementById('toast');
  const verdictInput = document.getElementById('verdict-input');
  const goldInput = document.getElementById('is-gold-input');
  const goldToggle = document.getElementById('gold-toggle');

  function esc(s) {
    if (s == null) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function toolSummary(t) {
    const name = t.tool_name || '?';
    const args = t.tool_args || {};
    const result = t.tool_result;
    if (name === 'check_availability') {
      const slots = (result && result.open_slots) ? result.open_slots.slice(0, 4) : [];
      const dateArg = args.date || '?';
      if (slots.length) return `📅 check ${dateArg} → ${slots.length}+ slots (${slots.join(', ')})`;
      return `📅 check ${dateArg} → no slots`;
    }
    if (name === 'book_appointment') {
      const ev = (result && result.event) || {};
      if (result && result.booked) return `✔ booked ${ev.service || '?'} @ ${ev.start || '?'}`;
      if (typeof result === 'string') return `✗ book failed: ${result}`;
      return `book_appointment → ${JSON.stringify(result || {}).slice(0, 80)}`;
    }
    return `${name}(${Object.keys(args).join(',')})`;
  }

  function renderChat() {
    chat.innerHTML = '';
    TURNS.forEach(t => {
      // Tool rows render as centered chips
      if (t.role === 'tool' || t.tool_name) {
        const row = document.createElement('div');
        row.className = 'tool-row';
        const chip = document.createElement('div');
        chip.className = 'tool-chip';
        chip.textContent = toolSummary(t);
        chip.title = 'double-click to expand raw payload';
        chip.addEventListener('dblclick', e => {
          e.stopPropagation();
          chip.classList.toggle('expanded');
        });
        row.appendChild(chip);

        const details = document.createElement('div');
        details.className = 'tool-details';
        details.textContent = JSON.stringify({ args: t.tool_args, result: t.tool_result }, null, 2);
        chat.appendChild(row);
        chat.appendChild(details);
        return;
      }

      const isAgent = t.role === 'agent';
      const row = document.createElement('div');
      row.className = 'bubble-row ' + t.role;

      const bubble = document.createElement('div');
      bubble.className = 'bubble ' + t.role;
      bubble.dataset.idx = t.idx;

      const tagged = Object.keys(state[t.idx].tags).length > 0;
      const badges = tagged ? Object.keys(state[t.idx].tags).map(k => {
        const lbl = (TAG_VOCAB.find(v => v[0] === k) || [k, k])[1];
        const cls = k === 'great_response' ? '' : ' err';
        return '<span class="badge' + cls + '">' + esc(lbl) + '</span>';
      }).join('') : '';

      bubble.innerHTML =
        '<div class="text">' + esc(t.text || '') + '</div>' +
        (badges ? '<div class="badges">' + badges + '</div>' : '') +
        '<div class="meta"><span class="idx">#' + t.idx + '</span>' + esc(t.ts) + '</div>';

      if (isAgent) {
        bubble.addEventListener('click', () => focusTurn(t.idx));
      }
      row.appendChild(bubble);
      chat.appendChild(row);
    });
  }

  function focusTurn(idx) {
    focusIdx = idx;
    document.querySelectorAll('.bubble.focused').forEach(el => el.classList.remove('focused'));
    const el = document.querySelector('.bubble[data-idx="' + idx + '"]');
    if (el) {
      el.classList.add('focused');
      el.scrollIntoView({ block: 'center', behavior: 'smooth' });
    }

    panelEmpty.style.display = 'none';
    panelContent.style.display = 'block';
    panelHd.textContent = 'Agent turn #' + idx;
    const t = TURNS[idx];
    const preview = (t.text || '').slice(0, 60);
    panelSub.textContent = preview ? '"' + preview + (t.text.length > 60 ? '…' : '') + '"' : 'tag issues below';

    tagsEl.innerHTML = '';
    TAG_VOCAB.forEach((v, i) => {
      const key = v[0], label = v[1];
      const isChecked = key in state[idx].tags;
      const wrap = document.createElement('label');
      wrap.className = 'tag';
      wrap.innerHTML =
        '<input type="checkbox" name="tag_' + idx + '_' + key + '" value="1"' + (isChecked ? ' checked' : '') + '>' +
        '<span class="label-text">' + esc(label) + '</span>' +
        (i < 9 ? '<span class="kbd">' + (i + 1) + '</span>' : '');
      const cb = wrap.querySelector('input');
      cb.addEventListener('change', e => {
        e.stopPropagation();
        if (cb.checked) state[idx].tags[key] = state[idx].tags[key] || '';
        else delete state[idx].tags[key];
        renderChat();
      });
      tagsEl.appendChild(wrap);
    });
  }

  function setVerdict(v) {
    verdictInput.value = v;
    document.querySelectorAll('.v-btn').forEach(b => b.classList.toggle('active', b.dataset.v === v));
  }
  document.querySelectorAll('.v-btn').forEach(b => {
    b.addEventListener('click', () => setVerdict(b.dataset.v));
  });
  setVerdict(verdictInput.value || 'unreviewed');

  function setGold(on) {
    goldInput.checked = on;
    goldToggle.classList.toggle('on', on);
  }
  goldToggle.addEventListener('click', e => { e.preventDefault(); setGold(!goldInput.checked); });
  setGold($is_gold_bool);

  function showToast(msg) {
    toast.textContent = msg;
    toast.classList.add('show');
    setTimeout(() => toast.classList.remove('show'), 1200);
  }

  document.addEventListener('keydown', e => {
    const active = document.activeElement;
    const inEditor = active && ['INPUT', 'TEXTAREA'].includes(active.tagName);

    if ((e.metaKey || e.ctrlKey) && e.key === 's') {
      e.preventDefault();
      document.getElementById('annotate-form').requestSubmit();
      return;
    }
    if (inEditor) return;

    if (e.key === 'j') {
      const pos = focusIdx == null ? -1 : AGENT_INDICES.indexOf(focusIdx);
      const next = AGENT_INDICES[Math.min(AGENT_INDICES.length - 1, pos + 1)];
      if (next != null) focusTurn(next);
    } else if (e.key === 'k') {
      const pos = focusIdx == null ? AGENT_INDICES.length : AGENT_INDICES.indexOf(focusIdx);
      const prev = AGENT_INDICES[Math.max(0, pos - 1)];
      if (prev != null) focusTurn(prev);
    } else if (e.key === 'w') { setVerdict('win'); showToast('verdict: win'); }
    else if (e.key === 'f') { setVerdict('fail'); showToast('verdict: fail'); }
    else if (e.key === 'm') { setVerdict('mixed'); showToast('verdict: mixed'); }
    else if (e.key === 'g') { setGold(!goldInput.checked); showToast('gold: ' + (goldInput.checked ? 'on' : 'off')); }
    else if (/^[1-9]$/.test(e.key) && focusIdx != null) {
      const i = parseInt(e.key) - 1;
      if (i < TAG_VOCAB.length) {
        const key = TAG_VOCAB[i][0];
        const cb = document.querySelector('input[name="tag_' + focusIdx + '_' + key + '"]');
        if (cb) { cb.checked = !cb.checked; cb.dispatchEvent(new Event('change')); }
      }
    }
  });

  renderChat();

  // Focus first tagged agent turn, else first agent turn
  const firstTagged = TURNS.find(t => t.role === 'agent' && Object.keys(state[t.idx].tags).length > 0);
  if (firstTagged) focusTurn(firstTagged.idx);
</script>
</body></html>
"""


def render_form_html(
    *,
    call_id_raw: str,
    tenant_label: str,
    call_started: str,
    turn_count: int,
    turns_json: str,
    tag_vocab_json: str,
    tag_lookup_json: str,
    existing_verdict: str,
    existing_is_gold: bool,
    notes_val: str,
    reviewer_val: str,
    caller_name: str = "",
    recording_path: str = "",
    recording_duration_ms: int = 0,
) -> str:
    """Interpolate the template with escaped values. All string values
    should already be html-escaped by the caller when needed; JSON
    payloads are pre-serialized via _js_json in the caller so </script>
    breakout is not possible."""
    # Use string.Template + safe_substitute so the massive CSS/JS body
    # (full of literal `{` `}`) doesn't have to be brace-escaped. Only
    # `$name` markers substitute; a bare `$` in the source (rare — we
    # audited) would need `$$` — but none exist in the template.
    # Header title shows the caller's name when we know it, else falls back
    # to a generic label. Keeps the h1 informative when scanning many calls.
    caller_label = (
        html.escape(caller_name.strip())
        if caller_name and caller_name.strip()
        else "Call review"
    )

    # Audio player: rendered only when a recording exists on disk.
    # Duration shown as m:ss for scan-at-a-glance. Muted plays inline
    # so a reviewer can scrub through the call without downloading.
    if recording_path:
        secs = max(0, int(recording_duration_ms) // 1000)
        dur_label = f"{secs // 60}:{secs % 60:02d}"
        recording_html = (
            '<section class="player">'
            '<span class="player-label">Recording</span>'
            f'<audio controls preload="metadata" '
            f'src="/admin/recordings/{html.escape(call_id_raw)}.mp3"></audio>'
            f'<span class="player-dur">{dur_label}</span>'
            f'<a class="player-dl" href="/admin/recordings/{html.escape(call_id_raw)}.mp3" '
            'download title="Download MP3">↓</a>'
            '</section>'
        )
    else:
        recording_html = ""
    return Template(_HTML_TEMPLATE).safe_substitute(
        css=_CSS,
        call_id_short=html.escape(call_id_raw[:16]),
        call_id_esc=html.escape(call_id_raw),
        tenant_label=html.escape(tenant_label),
        call_started_short=html.escape((call_started or "?")[:19]),
        turn_count=turn_count,
        turns_json=turns_json,
        tag_vocab_json=tag_vocab_json,
        tag_lookup_json=tag_lookup_json,
        existing_verdict=html.escape(existing_verdict),
        is_gold_bool="true" if existing_is_gold else "false",
        notes_val=notes_val,       # caller already html.escape'd
        reviewer_val=reviewer_val, # caller already html.escape'd
        caller_label=caller_label,
        recording_html=recording_html,
    )
