# Research Bundle Index — 2026-08-20

**Purpose:** when you're ready to have ChatGPT synthesize everything into one prioritized TODO list, this doc lists what's IN the bundle + gives ChatGPT the synthesis brief.

**Status:** waiting on 2nd humanness deep-research doc before zipping. Once it arrives, run `scripts/bundle_research.sh` to build `~/Desktop/research-bundle-YYYY-MM-DD.zip`.

---

## What's in the bundle (in reading order for ChatGPT)

### Foundation — the code + current state
1. **`WORKING-NOTES.md`** — live session state, what's shipped, current server PID, TODO. Read FIRST. Tells you what already works.
2. **`docs/UNIFIED-IMPLEMENTATION-PLAN.md`** — T-SP1..T-SP12 + T-SP-SPEED-EXTRA-A..H + T-SP-RELIABILITY + T-SP-SCALE. All tasks known so far, with per-task files/DB/deps/test-plan.

### Market + product research (highest-level → most-specific)
3. **`VOICEOPS_MASTER_RESEARCH_FINDINGS_AND_ROADMAP_2026-08-18.md`** — 2600-line master roadmap (market signals + Upwork demand).
4. **`docs/VOICEOPS_CODEBASE_MARKET_DEMAND_GAP_AUDIT_2026-08-19.md`** — verified gap audit (kernel done, business-layer missing). SPOT-CHECKED against real code, 5/5 claims true.
5. **`VOICEOPS_CODEBASE_MARKET_DEMAND_GAP_AUDIT_2026-08-19.md`** (repo-root, 65KB) — audit's original delivery from ChatGPT.
6. **`VOICEOPS_SYSTEMS_ARCHITECTURE_BLUEPRINT_FOR_CLAUDE_CODE_2026-08-19.md`** — the "build these systems" blueprint follow-up.

### Speed research (round 1 → round 2)
7. **`docs/openai-speed-research-2026-08-20.md`** — 8 pure-speed OpenAI TTFT levers (round 1). Fast tier already shipped; predicted-outputs, prompt-cache-key, schema-warmup, max_tokens shrink covered.
8. **`docs/DEEP-RESEARCH-NETWORK-ARCHITECTURE-2026-08-20.md`** — network + session architecture (round 2). Multi-context ElevenLabs WS, us-east geography, zero-transcode, Flux+EagerEndOfTurn, Twilio CLEAR barge-in, OpenAI Responses WS. **Supersedes round 1 in ordering** — network changes now #1 priority.

### Humanness research (multiple rounds)
9. **`docs/HUMANNESS-RESEARCH-BRIEF-2026-08-20.md`** — the research brief we sent (asks 9 specific questions).
10. **`HUMANNESS-RECOMMENDATION-2026-08-20.md`** (repo-root, 63KB) — response #1. 3-problem compound framing.
11. **`deep-research-report-humanness.md`** (repo-root, 72KB) — response #2. Deeper. Provides drop-in PERSONA + HOW YOU ACTUALLY TALK text; voice IDs (all failed API verification); multi-context WS recommendation.
12. **[2nd humanness deep-research doc — PENDING, add when it arrives]**

### Historical audits (context for what was already tried)
13. `docs/AUDIT_INTELLIGENCE_2026-08-04.md`
14. `docs/AUDIT_RESPONSE.md`, `AUDIT_RESPONSE_2.md`, `AUDIT_RESPONSE_3.md`
15. `docs/AUDIT_2026-08-05-runtime-failure-patterns.md`
16. `docs/AUDIT_VERIFICATION_2026-08-05.md`
17. `VOICEOPS_CODEBASE_AUDIT.md` (repo-root, 91KB — original codebase audit)
18. `VOICEOPS_REAUDIT_2026-08-02.md`

### Bench data + call transcripts (empirical ground truth)
19. **`docs/llm-ttft-bench-2026-08-20_012206.md`** — measured openai=1534ms, openai-fast=772ms, groq-oss20b=485ms (rate-limits at scale).
20. **`docs/transcripts/README.md` + all transcripts** — real call transcripts with per-turn latency annotations. Grounds every "what's actually happening" claim.

---

## The synthesis brief for ChatGPT

Copy this text into ChatGPT with the bundle attached:

---

### Task
You're being handed **12+ research/audit/plan docs** covering the same repo. Priorities have shifted three times as new research arrived. **Synthesize into ONE authoritative prioritized TODO list.**

### Constraints
- **Do NOT reinvent** anything. Cite exact task IDs from `UNIFIED-IMPLEMENTATION-PLAN.md` where possible; add new ones only for genuinely new items.
- **Cross-reference conflicts.** If doc A says "one WS per turn" and doc B says "one WS per call with multi-context," note the newer wins and mark the older as superseded.
- **Cite evidence per recommendation.** If it's from a bench, cite the bench. If it's from a real call transcript, cite the CallSid. If it's a claim without evidence, say "unverified" and rank it lower.
- **Kill duplicates.** Merge same-topic items across docs. Don't produce a 200-item list.
- **Rank by leverage × ease.** Not alphabetically, not by author preference.

### Structure of the output

```markdown
# Master Priority TODO — synthesized <date>

## Guiding principles (5 bullets max, from the pattern in the research)

## What's already shipped (from WORKING-NOTES session log — pull the actual current state)

## The prioritized list

### Now (this week, ~10 hours or less)
1. [TASK-ID] Title — one-line why — evidence: <cite> — files touched: <list>
2. ...

### Next (2-4 week arc)
...

### Later (2-3 months out, after client validation)
...

### Won't do / deferred (with reason)
...

## Superseded recommendations
Old rec → why it's superseded → what to do instead.

## Open questions
Things the research disagrees on. Flag for human decision.
```

### Ground rules for what NOT to include

- Don't include generic "voice AI best practices" that don't cite the repo state
- Don't include tasks that were already shipped (verify against WORKING-NOTES session log)
- Don't include speculative product features not in any doc (no HubSpot connectors, no Bland.ai comparisons, no vocal cloning until someone's asked)
- Don't include marketing copy or Upwork gig-writing — engineering only
- Don't hedge every recommendation with "consider X" — pick one, cite why

### What the receipt should be
One markdown file. ~500-800 lines. Ready to paste back into repo as `docs/MASTER-PRIORITY-TODO-<date>.md` and replace `docs/UNIFIED-IMPLEMENTATION-PLAN.md` as the working source of truth.

---

## Bundle script

`scripts/bundle_research.sh` (created alongside this doc) will zip everything above. Run:
```bash
bash scripts/bundle_research.sh
```
Output: `~/Desktop/research-bundle-<date>.zip`

---

## Reminder about the code base

**Send this WITH the code zip.** ChatGPT can't synthesize accurately if it doesn't know what's shipped. Code zip: `~/Desktop/receptionist-agent-code-<date>.zip` (7-8 MB).
