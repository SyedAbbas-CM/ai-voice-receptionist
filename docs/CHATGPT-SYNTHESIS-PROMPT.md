# ChatGPT Synthesis Prompt

Copy the text below into ChatGPT (or equivalent) along with the two attachments:
- `research-bundle-2026-08-20.zip` (~15+ research/audit/plan docs)
- `receptionist-agent-code-2026-08-20.zip` (the actual codebase)

---

## The prompt

You're being handed **two zip files**: (a) roughly 15 accumulated research/audit/plan docs about a real voice-agent product in this repo, (b) the current codebase itself.

The docs were written over three weeks by different rounds of deep research. Priorities have shifted at least four times as new research arrived. Some earlier recommendations are now superseded but not marked as such. Some recommended tasks are already shipped but still appear as TODO. Some doc pairs disagree on the right answer.

**Your job: synthesize ONE authoritative prioritized TODO list.** Not another opinion doc. Not another audit. The actual plan we execute from here.

### Read in this order

1. `01_FOUNDATION_WORKING-NOTES.md` — current session state. Tells you what's shipped, what's live on the server, PID, current metric baseline. Read this first so you know what NOT to recommend (already done).
2. `02_FOUNDATION_UNIFIED-IMPLEMENTATION-PLAN.md` — the existing task list (T-SP1..T-SP12 + T-SP-SPEED-EXTRA-A..H + T-SP-RELIABILITY + T-SP-SCALE). This is our source of truth for task IDs. Cite these IDs. Add new ones only for genuinely new work.
3. `20_SPEED_openai-speed-research-2026-08-20.md` — round 1 OpenAI-specific TTFT levers.
4. `21_SPEED_DEEP-RESEARCH-NETWORK-ARCHITECTURE-2026-08-20.md` — round 2 network/session architecture (multi-context WS, us-east geography, zero-transcode, Flux+EagerEOT, Twilio CLEAR barge-in). **This is where the biggest remaining latency wins live.**
5. `31_HUMANNESS_RESPONSE1_HUMANNESS-RECOMMENDATION-2026-08-20.md` — humanness round 1 (3-problem compound framing).
6. `32_HUMANNESS_RESPONSE2_deep-research-report-humanness.md` — humanness round 2 (drop-in PERSONA text + 4 voice IDs — **all 4 IDs failed live API verification, note this**).
7. `33_HUMANNESS_RESPONSE3_deep-research-report.md` — humanness round 3 (**most recent**, converges strongly with round 2 network doc). Adds: NextActionPolicy architecture, per-speech-act token caps, full recommended prompt scaffold (# ROLE / # CONVERSATION CONTRACT / etc), voice-cloning-over-stock recommendation.
8. Market/audit docs (`10_MARKET`, `11_MARKET`, `12_MARKET`, `13_MARKET`) — Upwork-demand + gap audit + systems-blueprint.
9. Historical audits (`40_HISTORICAL`..`47_HISTORICAL`) — context only. Do NOT resurface anything already fixed.
10. Bench data (`50_BENCH_llm-ttft-bench...`) and call transcripts (`51_TRANSCRIPTS/*.md`) — empirical ground truth. Use these to validate any claim about "the LLM is slow" or "the agent sounds robotic."

### Cross-reference the CODE zip

Before recommending a task:

- Check `WORKING-NOTES.md` session log — was it already shipped? If yes, mark as done.
- Check the actual file paths named in tasks (`packages/core_agent/prompt.py`, `apps/api/app/providers/llm/*`, etc.) — did the recommendation misread the code? Note if so.
- Cite exact file paths + line numbers in your output. `apps/api/app/providers/llm/groq_llm.py:42-45` beats "the Groq provider."

### Constraints

- **Kill duplicates.** Many docs recommend the same thing. Merge; don't list twice.
- **Note supersession explicitly.** "Doc A said one WS per turn — superseded by doc B (multi-context, one WS per call)." List old recommendation → why superseded → what to do instead.
- **Cite evidence per recommendation.** If it's from a bench, cite the bench filename + numbers. If it's from a call transcript, cite the CallSid. If claim has no evidence, mark "unverified" and rank lower.
- **Rank by leverage × ease.** Not alphabetically. Not by author. Not by which doc mentioned it first.
- **Preserve load-bearing prompt sections.** Multiple docs explicitly warn: TIME HANDLING, PHONE, BOOKING CONFIRMATION, HALLUCINATION GUARDRAILS, COMPLIANCE REFUSALS, SEMANTIC PLAN, DATE HANDLING in `packages/core_agent/prompt.py` are load-bearing — past regressions were fixed by these rules. Do NOT recommend rewriting them.

### The output structure

```markdown
# Master Priority TODO — synthesized <date>

## Verified current state
[From WORKING-NOTES session log — bullet what's actually live NOW on the server, not what's planned.]

## Guiding principles (5 bullets max)
[The patterns that recur across the docs. Should be verifiable claims, not slogans.]

## Superseded recommendations
[Table: old rec → new rec → source doc that superseded it]

## The prioritized list

### 🔴 CRITICAL — this week (≤10 hours total)
1. [TASK-ID or NEW-<slug>] Title
   - Why: one-line
   - Evidence: <cite doc/bench/CallSid>
   - Files touched: <exact paths>
   - Estimate: <hours>
   - Definition of done: <how we know it worked>

### 🟡 NEXT — 2-4 weeks (business layer + measurable speed)
[Same format]

### 🟢 LATER — after first paying client (2-3 months out)
[Same format]

### ⚫ DEFERRED — reasoned no
[Old recs you're explicitly killing, with why]

## Open questions for human decision
[Where docs disagree and the choice needs human judgment, not more research]

## Bench + verification suggestions
[Small measurement tasks that would reduce open questions]
```

### Ground rules for what NOT to include

- Don't reinvent tasks that are already in `UNIFIED-IMPLEMENTATION-PLAN.md`. Cite existing IDs.
- Don't include tasks the session log says are shipped.
- Don't include marketing copy or Upwork positioning — engineering + product only.
- Don't hedge every recommendation with "consider X." Pick one.
- Don't propose speculative product features nobody's asked for (no HubSpot, no vocal cloning until someone requests, no comparison to Bland.ai/Retell).
- Don't recommend swapping ElevenLabs Flash v2.5 — it's a hard constraint.
- Don't recommend swapping the chained STT→LLM→TTS pipeline for OpenAI Realtime unless it's marked as an explicit tier ("premium fast lane" — 1-2 week rewrite).

### Format of the deliverable

**One markdown file.** ~500-1000 lines. Ready to paste into repo as `docs/MASTER-PRIORITY-TODO-2026-08-20.md` and REPLACE `docs/UNIFIED-IMPLEMENTATION-PLAN.md` as the working source of truth.

### Meta

- If a document is missing evidence for a strong claim, mark it `[unverified]`.
- If two documents contradict, name both by filename and give your reasoning for which wins.
- If your recommendation depends on us doing something first (e.g. "add telemetry first, then decide"), sequence that as a prerequisite task.
- The receipt from you is a plan we can execute against, not another audit.

---

## What was NOT included in the bundle (and why)

- `.env`, secrets, models, TTS caches, uvicorn logs — private / not needed.
- Adversarial test reports — mostly noise, not decision-driving.
- Historical planning docs older than 2026-08-01 — obsoleted by newer research.
- Full git history — read the working notes' session log instead.

## Attachments checklist

- [ ] `research-bundle-2026-08-20.zip` — 15+ research docs (see index inside)
- [ ] `receptionist-agent-code-2026-08-20.zip` — actual current codebase (~7 MB, no venv/secrets)
