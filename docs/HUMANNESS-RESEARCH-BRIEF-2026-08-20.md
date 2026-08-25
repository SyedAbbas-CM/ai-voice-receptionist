# Humanness Research Brief — send to ChatGPT

**Purpose:** Get a research-backed prompt + voice-config recommendation to make our AI receptionist sound clearly human, warm, and friendly — not corporate-template or robot-neutral.

**Why we're asking you (ChatGPT):** Real callers keep saying the current agent "lacks warmth" and "sounds robotic." We already have a working reference (subtodealz-vapi assistant) in-house that sounds much more human. We want a research-grade upgrade specific to this codebase, not another generic guide.

---

## What we already have — the constraint

**Runtime:** Twilio Media Streams → Deepgram Nova-3 STT → OpenAI gpt-4o-mini (via router) → ElevenLabs Flash v2.5 → Twilio playback.

**Voice model:** ElevenLabs `eleven_flash_v2_5` (~75ms first-byte). Cannot change model without losing sub-300ms TTS latency.

**Current voice ID:** `EXAVITQu4vr4xnSDxMaL` (Sarah — ElevenLabs stock, "warm business female"). Considered too corporate by real callers.

**Current voice_settings sent to ElevenLabs:**
```json
{"stability": 0.5, "similarity_boost": 0.75}
```

**No `style` param set.** No SSML markup. No pause tokens. Text goes straight to TTS as plain sentences.

**Language:** English only for the demo tenant (Smile Dental Clinic, Plano TX).

**Streaming path status:** LLM→TTS streaming IS wired (`streaming_llm_to_tts=true`), so we can chunk text by sentence into TTS.

---

## Real-call evidence — what the agent actually says right now

From `CAa8d6d3d6751eea6856cb18b53c0ed7c2` (2026-08-19 tester Oliver, US):

```
Agent: Thanks for calling Smile Dental Clinic, how can I help?
Caller: Tell me about your services?
Agent: Of course! What service do you need, and do you have a specific date, and time in mind?
Caller: I'm trying to see... Oliver.
Agent: Thanks, Oliver! What service are you looking for, and do you have a date in mind for your appointment?
Caller: Bye.
Agent: Take care, Oliver! If you need anything later, just give us a call.
Caller: Bye.
Agent: Goodbye! Have a great day!
Caller: Bye.
Agent: See you later!
```

**Latency:** p50 1.53s end-of-caller → first agent audio. Breakdown per turn: STT ~350ms, LLM first-token ~1900ms, TTS first-byte ~300ms, network ~200ms. LLM is the bottleneck; TTS is fast.

**Tester's verbatim feedback:** "voice is robotic," "lacks warmth," "not friendly," "need to make it more human like."

Full transcript index: `docs/transcripts/README.md`.

---

## What "human-sounding" looks like in our own repo — the reference

We have an existing outbound-real-estate assistant that reads much more natural: `workflows/n8n/subtodealz-vapi-assistant-prompt.md` (1087 lines).

Key excerpts from its `Voice & Persona` and `Guidelines for Natural, Human-Like Conversation` sections:

**Speech Style:**
- Natural fillers: "umm," "well," "you know," "actually," "kind of," "sort of"
- Active listening: "oh okay," "got it," "that makes sense," "interesting," "I hear you"
- Pause naturally: give them space to respond — don't rush
- Mirror their energy: if they're busy, be quick. If they're chatty, engage more
- Avoid robotic pacing: vary tone and speed like a real person

**DOs:**
- Keep responses short: 20-30 words max per turn
- Let them talk — pause after questions, don't rush
- Acknowledge everything: "Oh okay," "I hear you," "That makes sense"
- Repeat back key info: "So just to confirm, it's still at $1,800 per month, right?"
- Use contractions: "I'm," "you're," "that's," "it's"
- Sound curious, not pushy: "I was just wondering..." "Would you be open to..."
- Mirror their tone — if they're formal, be slightly more professional; if casual, match it

**Example turn (Alex → Sarah):**
> "Hey Sarah, this is Alex with SubtoDealz. I'm just reaching out about the property you've got listed at 456 Oak Avenue — is now a good time?"

That reads warm and human.  Ours reads "receptionist template."

---

## The current clinic prompt (relevant sections)

**Persona field (`sample-data/clinic/business.json`):**
> You are Alex — the office manager and front-desk lead at Smile Dental Clinic. You've worked here for five years. You know Doctor Chen, Doctor Ramanathan, Doctor Whitfield, and Rosa personally — first-name basis. You're warm and Texas-friendly (light 'y'all' or 'gotcha' occasionally, not exaggerated). Efficient on routine bookings — you don't waste patients' time but you're never curt. On emergencies you drop the pep and get calm and reassuring. If a caller sounds frustrated, you skip the 'I understand' scripts and get to fixing the problem. You genuinely like the patients and it shows.

**Prompt sections (from `packages/core_agent/prompt.py`):**
1. CURRENT DATE + TIME (injected per business timezone) — **keep as-is**
2. PERSONA — **rewrite for warmth**
3. IDENTITY LOCK — **keep**
4. HOW YOU ACTUALLY TALK — **rewrite completely** (the section we most want research on)
5. EXAMPLES — **replace with warmer, more subtodealz-style examples**
6. PROACTIVE, NOT REACTIVE — **keep, maybe tighten**
7. MOOD-AWARE — **expand, this is the mirroring guidance we need**
8. TOOLS — **keep**
9. WAIT-PROMISE ↔ TOOL-CALL LAW — **keep (load-bearing)**
10. DATE HANDLING — **keep (load-bearing)**
11. SEMANTIC PLAN — **keep (T-SP1 dependency)**
12. TIME HANDLING — **keep (load-bearing)**
13. REMEMBER MULTI-STEP INTENT — **keep**
14. HALLUCINATION GUARDRAILS — **keep (load-bearing)**
15. BOOKING CONFIRMATION RULE — **keep (load-bearing)**
16. PHONE NUMBER HANDLING — **keep (load-bearing)**
17. COMPLIANCE REFUSALS — **keep (load-bearing)**
18. BUSINESS INFO — **keep**

**"Load-bearing" means:** past regressions were fixed by these rules; changing them would reintroduce shipped bugs. Do NOT change wording of load-bearing sections; only the personality/style sections.

Full file: `packages/core_agent/prompt.py` (426 lines).

---

## What we're asking you to produce

Please produce ONE consolidated recommendation document with these sections.  Cite research where relevant (papers, ElevenLabs docs, HCI voice-AI studies).  Be specific, not generic — this is going into production code.

### 1. Rewritten PERSONA field

New content for the `voice_persona` field in `business.json`. Aim for warmer, more specific, less "brochure."  Think of a real 30-something Texan receptionist who's genuinely good at her job and enjoys the patients.  Include specific speech tics she'd have.

### 2. Rewritten "HOW YOU ACTUALLY TALK" section

Replace lines 40-70 of `packages/core_agent/prompt.py`.  Draw on:
- Published research on human-like conversation (parasocial cues, back-channels, hedges, prosodic tone-matching)
- HCI voice-AI usability studies (what actually makes people say "that felt human")
- The subtodealz reference above
- The 20-30-word response length limit (subtodealz DO)
- Filler placement (where + how often)
- Acknowledgment tokens (which ones, how varied)
- Contraction density
- Mirroring rules (formal vs casual caller)
- When to slow down / speed up
- When to use small disfluencies ("umm", trailing off with "so...") vs when NOT to (compliance/booking confirmations)

### 3. Rewritten EXAMPLES (3-5 example turns)

Replace lines 72-105.  Each example should be a caller → agent exchange the LLM can pattern-match.  At least one should show:
- Handling a frustrated caller
- A quick booking
- A caller who trails off / mumbles
- An emergency
- A compliment or small-talk moment

### 4. ElevenLabs voice recommendation

Recommend 3-5 alternative voice IDs from the ElevenLabs library, ranked, with rationale.  Target: warm American female clinic receptionist, 30-40 age range, Texas or general Southern lilt acceptable, avoids "voice actor" quality.  Include:
- Voice ID (public ID or clone-required flag)
- Sample text you'd have us listen to
- Why this voice matches the persona
- Any known cost / license considerations
- Whether Voice Cloning (paid tier) would beat all stock voices

### 5. ElevenLabs voice_settings recommendation

Current: `{stability: 0.5, similarity_boost: 0.75}`, no `style` param.

Recommend values for the chosen voice.  ElevenLabs knobs:
- **stability** (0-1): lower = more expressive/variable; higher = more consistent/robotic
- **similarity_boost** (0-1): higher = closer to source clone; can amplify noise at extremes
- **style** (0-1, Flash v2.5 supports it): amount of style exaggeration; 0 = neutral, 0.5 = moderate flair
- **use_speaker_boost** (bool): louder + clearer, mild artifact tradeoff

For a warm receptionist, what's the empirically-good starting point?  Cite ElevenLabs' own docs where you can.

### 6. Text-level prosody tricks

What can we do in the TEXT the LLM emits to make TTS sound more natural?  We use `eleven_flash_v2_5` which has limited SSML support — please confirm what actually works:
- Ellipses `...` for pauses
- Em-dashes `—` for shorter pauses
- Multiple exclamation for emphasis
- Comma density (fewer commas = smoother, more commas = choppier)
- "Um," "uh," "hmm" as inline filler tokens — does Flash v2.5 speak them or skip them?
- All-caps for emphasis — does it work?
- SSML `<break>` tags — supported by Flash v2.5?
- Sentence length — does Flash sound better on short sentences vs long?

### 7. Streaming-path prosody tips

We stream LLM tokens → TTS one sentence at a time. Any advice on:
- Chunk boundary placement (sentence vs clause)
- How to avoid TTS "restarting" mid-thought
- Whether to send text with trailing pauses inline or via chunk delay

### 8. Ordered recommendation

If we can only ship 3 things this week for maximum perceived-humanness lift, which 3 and in what order?  Rank by (impact × ease).

### 9. Validation plan

How would you have us empirically test whether your recommendations are working?  We can dial the agent from real US + PK phones.  What specific caller-side observable behavior would prove the humanness upgrade landed vs feels the same?

---

## What we do NOT want

- Generic "prompt engineering best practices" that don't cite research or account for our runtime
- Recommendations that require changing away from Flash v2.5 (breaks our <300ms first-byte)
- Suggestions to add SSML if Flash v2.5 doesn't support it
- Ideas that break load-bearing prompt sections (see list above)
- Voice IDs that don't exist / require enterprise-tier plans without saying so

---

## Attachments (files in this repo you may want to reference)

- `packages/core_agent/prompt.py` — full current prompt (426 lines)
- `workflows/n8n/subtodealz-vapi-assistant-prompt.md` — the reference (1087 lines)
- `sample-data/clinic/business.json` — persona field
- `apps/api/app/providers/tts/elevenlabs_tts.py` — current TTS config (voice_settings live here)
- `docs/transcripts/CAa8d6d3d6751eea6856cb18b53c0ed7c2.md` — the "sounds robotic" call
- `docs/transcripts/README.md` — index of all recent calls

Please deliver as a single markdown document we can paste back into the repo as `docs/HUMANNESS-RECOMMENDATION-<date>.md`.
