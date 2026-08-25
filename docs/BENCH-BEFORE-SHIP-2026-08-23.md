# BENCH-BEFORE-SHIP — pattern learned 2026-08-23

## The rule

**Before shipping any code change that tweaks a single tunable constant claimed to save latency, measure the change with a standalone bench script. Only ship if the measured p50 improvement is ≥50 ms.**

Ship without measurement is only appropriate for architectural changes (structure/behavior, not a knob).

## Why this exists

On 2026-08-23 we received a well-reasoned ChatGPT audit that ranked "cheapest remaining speed levers" in an ordered queue. The top two shippable levers were tunable-constant swaps:

- **S1** — `elevenlabs_tts.py` `aiter_bytes(chunk_size=1600 → 640)` — audit's #1
- **S4** — Flux STT audio path `linear16@48k → mulaw@8k` — audit's second architectural knob

Both looked mechanically correct on paper. Both had convincing "50-150ms savings" reasoning. Networking chat wrote standalone bench scripts before touching runtime code.

**Result: both benched NEUTRAL.**

- S1 across `1600 / 640 / 320 / 160` chunk sizes: all p50s within ±16 ms (noise floor). ElevenLabs streams the first chunk fast enough that the read-boundary constant doesn't cost measurable time.
- S4 across `mulaw@8k` vs `linear16@48k`: 56 ms median delta, inside per-trial variance (271 ms flip on trial 3). Not a ship-worthy signal.

Had we shipped both, we would have spent:
- One coordinated bounce
- One verify cycle (user test call + log analysis)
- Post-hoc bisection when nothing measurably improved
- Bench-script writing at the end anyway to figure out why

Instead we spent ~30 min on two bench scripts, killed both changes, moved to real work.

## The pattern

### 1. Classify the change first

| Class | Ship without bench? | Why |
|---|---|---|
| **Tunable constant** (chunk size, buffer threshold, delay ms, model swap) | No — bench first | Small changes with claimed big impact almost always disappoint in reality. The mechanism is real; the magnitude is drowned in variance. |
| **Bug fix** (wrong param, missing conversion, race condition) | Yes — ship + verify | Correctness, not perf. Bench doesn't apply. |
| **Architectural** (persistent connection vs new-per-request, policy layer vs LLM improvisation, new observability primitive) | Yes — ship + verify | Structural changes bench-measure poorly in isolation because they interact with other subsystems. Ship into the real system and measure end-to-end. |
| **Configuration expansion** (add new field, add new lane, add new fallback) | Yes | No knob to tune; measuring one-value-vs-another doesn't apply. |

### 2. Bench criteria — what makes a bench honest

- **Same machine, same network, same time-of-day** as production
- **Same provider/model/voice/format** — don't bench with easier params
- **Same total input shape** — real prompt size, real tool schemas, real audio content
- **≥5 trials per config** — one trial is noise
- **Report p50 AND p90** — averaging across trials hides tail spikes
- **Standalone, doesn't touch runtime code** — a bench that requires the actor path leaks behavior between trials
- **Committed to `scripts/`** — reproducible for the next audit round

### 3. Ship decision from bench

- **p50 improves ≥50 ms with no p90 regression** — ship
- **p50 improves ≥50 ms but p90 gets worse** — investigate (variance sensitivity may make it worse under load)
- **p50 within ±30 ms** — kill, don't ship, don't churn code
- **Data suggests a related-but-different change** — write the different bench

## Filed bench scripts

Reusable for future audit rounds:

- `scripts/bench_el_chunk_size.py` — ElevenLabs `/stream` first-byte across `aiter_bytes` chunk sizes. Verdict: NEUTRAL (all p50s within ±16ms). Kill.
- `scripts/bench_flux_encoding.py` — Deepgram Flux mulaw@8k vs linear16@48k first-TurnInfo latency. Verdict: NEUTRAL for latency (56ms noise-floor delta), keep mulaw for bandwidth (12x cheaper upstream).
- `scripts/bench_flux_first_update.py` — 6-case Flux first-Update fingerprint (leading silence / keyterm params / eot threshold / cold-connect / short-utterance shape). Defensive — written to be run when a customer trace shows first Update > 1s. Includes interpretation matrix in script output.
- `scripts/voice_llm_bench.py` — 6-model × 7-scenario × 3-trial tournament (leaks / TTFT / p50 / p95 / tool-call quality). Verdict 2026-08-23: gpt-4.1-nano wins (p50 606ms, zero leaks). gpt-5.4-nano / gpt-5.6-luna / gpt-5.6-terra viable but slower. gpt-5.6-sol disqualified for latency (p95 2452ms). **Zero leaks across 126 requests — the CAd26f39 tool-JSON leak was environmental, not model-repeatable.** Absolute TTFTs underestimate prod by 100-300ms because bench uses 161-char fallback prompt vs 21k prod prompt (app.* import chain broke real-prompt loading); relative ranking is trustworthy since all models got identical inputs.
- (add here as more get written)

## What this pattern doesn't cover

- **Bug fixes surfaced by audits** — audit says "your parser reads the wrong field" or "you're double-injecting an event" — those are correctness, ship without benching.
- **Correctness regressions** — always ship the fix, verify end-to-end.
- **Prompt changes** — no bench for humanness; measure by real caller testing + explicit user feedback.
- **Infrastructure changes** (region swap, multi-worker, new provider) — bench the infra ceiling separately, but the shipping decision is architectural.

## Rule of thumb: "audit says X is the top lever"

Audit rankings are hypotheses. They're informed guesses about mechanism, not measurements from your specific environment. Treat every top-ranked *tunable* recommendation as "worth benching," not "worth shipping."

Architectural top-rankings (P1 EL multi-context WS per call, ConversationNextActionPolicy, US-East deploy) don't need benching before commit because they change structure, not a knob. But they DO need explicit before/after measurement after shipping so you can verify the claimed win.

## Cost of not following this

Historical examples from this project:

- Filler delay `1500 → 700 → 1200 → 60000` (four tunes) — every one shipped, every one wrong in a different way. First-real-caller feedback each time. Would have been solvable with one measured bench of "when do LLM turns typically complete." Cost: 3 wasted verify cycles.
- gpt-4o-mini → gpt-4.1-nano attempted, benched at 2141 ms first-token vs mini's 1348 ms one call, reverted. That WAS an unshipped-then-shipped-then-reverted cycle. Should have benched cross-model in isolation first with real prompt.
- Model swap chain gpt-4o-mini → gpt-4.1-nano → gpt-5.4-nano → gpt-5.6-luna (2026-08-22 to 2026-08-23) — three trial-and-error swaps based on one-shot bench data or config-comment claims. Would have been solved in ~15 min by an early 6-model × 7-scenario tournament (`scripts/voice_llm_bench.py`), which is what we eventually did. **Lesson: for the biggest single tunable in the stack (LLM model), skip the ad-hoc benches — always run the full tournament.**

## Environmental vs model-repeatable failures

The 2026-08-23 CAd26f39 tool-JSON leak looked like a model failure — gpt-5.4-nano was blamed for "emitting the tool call as content." Bench tournament proved **zero leaks across 126 requests including gpt-5.4-nano**. The leak was environmental (concurrent request, stream corruption, gen-race — unclear which). Two defensive drops shipped anyway (brain-side + pump-side) because environmental failures reproduce; model failures either always happen or never do.

Rule: **before blaming a model, reproduce in isolation.** If it doesn't repeat under controlled bench, the failure is in the surrounding system — usually racing/state/concurrency — and the fix belongs there, not in a model swap.

## Correctness bugs hide in tournament output

Same tournament caught a separate bug: gpt-4o-mini emitted `book_appointment` with `start_iso="2023-10-04"` — a **2023 date in an August-2026 test**. Would have booked wrong-year appointments in production without anyone noticing until a customer complained. Tournament's "tool_call quality" column caught it; a p50-latency-only bench would not have.

Rule: **bench for correctness AND speed, not just speed.** Any bench that only reports latency ratios misses the failure mode that actually breaks customers.

## The counter-rule (when NOT to bench first)

- **The change is <5 min to ship AND <5 min to revert if wrong**: the bench isn't cheaper than just trying it. Ship + verify.
- **Bench requires infrastructure we don't have**: e.g., Karachi-to-us-east latency, second-device acoustic capture. In those cases, bench in production with explicit before/after measurement instead.
- **The audit itself contains the measurement** — if the audit says "I benched X vs Y from the same environment and X wins by 200 ms," you can trust it more. Verify the environment matched, then ship.

## Known warts — hardcoded values with sunset dates

Values baked into code/prompt that need manual update before a future date. Grep here first when tempted to "just hardcode the year to keep it simple."

| Where | What | Sunset | Fix |
|---|---|---|---|
| `packages/core_agent/prompt.py` § DATE HANDLING § YEAR SANITY CHECK | Hardcoded rejection list `2023-`, `2024-`, `2025-` | 2027-01-01 (or when 2026 becomes past) | Replace with `{today_iso[:4]}` template variable AFTER verifying substitution doesn't break OpenAI prompt-cache prefix alignment. Every year change breaks cache one time on the day it flips; annual repeatable. Cheaper today to eat the 16-month manual update. |

Add rows here as we ship hardcoded-value warts. Grep `sunset` in code to find un-cataloged ones.

## Historical: correctness bugs caught by bench that would have shipped

| Date | Bug | Caught by | Damage prevented |
|---|---|---|---|
| 2026-08-23 | gpt-4o-mini emits `start_iso="2023-10-04"` for Aug 2026 bookings | `voice_llm_bench.py` tool-call quality column | 47% of past bookings (7/15) had wrong-year `scheduled_for` on the mini era (2026-08-04 through 2026-08-18). All test rows, no customer data affected. Would have silently corrupted a customer calendar on the first real booking after prod launch. |

Extend when we find more. This table's growth rate is the ROI of bench-before-ship.

---

**Last updated:** 2026-08-23 — after S1 + S4 both benched neutral (saving two bounces), voice_llm_bench.py tournament (saving a wrong-year silent-corruption in prod).
