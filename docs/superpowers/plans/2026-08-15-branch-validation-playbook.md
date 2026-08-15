# Per-Branch Validation Playbook

**Purpose:** the repeatable loop for taking a feature branch, proving it doesn't regress on real calls, and merging it to base. Applies to every branch from R6 onward.

**Scope:** validation only. Building the branch itself is covered by its own plan doc (e.g. `2026-08-14-phase2-2b-knowledge.md`). This doc is about what happens AFTER the branch is done and before it lands on `feat/architectural-networking`.

---

## The 5-step loop

```
1. Baseline soak on plain base    (N calls, save scorecards)
2. Merge branch to test branch    (never to base directly)
3. Same N calls on test branch    (same testers/day if possible)
4. Diff scorecards                (regression = any scenario greener on baseline)
5. Fast-forward merge to base OR fix on source branch and re-loop
```

### Step 1 — Baseline soak

```bash
cd "/Users/az/Desktop/Receptionist Agent"
git checkout feat/architectural-networking
./apps/api/scripts/run_server.sh &

# Dial N calls per docs/soak/scenarios.md
# For each call, after hangup:
apps/api/scripts/verify-call.sh CA<sid> \
  > apps/api/data/soak_reports/baseline-<date>-<callN>.json
```

Save the whole reports directory as `baseline-<date>/`.

### Step 2 — Merge to test branch

```bash
git checkout feat/architectural-networking
git checkout -b soak/<branch-name>-test
git merge <feature-branch>
# resolve conflicts if any, do NOT touch feature branch's code
```

### Step 3 — Same N calls on test branch

```bash
git checkout soak/<branch-name>-test
./apps/api/scripts/run_server.sh &

# Same N calls, same scenarios.
# Save reports to soak-<branch-name>-<date>/
```

### Step 4 — Diff scorecards

```bash
apps/api/scripts/soak/diff-scorecards.py \
  --baseline baseline-<date>/ \
  --candidate soak-<branch-name>-<date>/
```

Output:
- **Cleaner-or-equal** in every scenario → PASS
- **Anywhere regressed** → FAIL, output tells you which scenarios + which markers

Regressions get investigated before proceeding.

### Step 5 — Merge or fix

**On PASS:**
```bash
git checkout feat/architectural-networking
git merge --no-ff <feature-branch>  # keeps history clean
git branch -d soak/<branch-name>-test  # cleanup
# feature branch itself: keep for a week in case rollback needed, then delete
```

**On FAIL:**
- Fix on the source branch (not the test branch)
- Restart at step 2

---

## Calls-per-branch guidance

Not every branch needs 30 calls. Right-size to what it changes.

| Branch category | Min calls | Which scenarios must run | Extra checks |
|---|---|---|---|
| **Log-only** (e.g. PHASE2-shadow) | 5 | Any 5 | Grep for the new log lines actually appearing |
| **Observability** (e.g. R7) | 5 | Any 5 | Grep for chain events + linking lines |
| **Audio-path** (e.g. R6) | 15 | All 8 × 2 | Comparative listen — audio quality is subjective |
| **Slot-capture** (e.g. R3 sub-phases) | 20 | Scenarios 3, 4, 5, 6 × 5 each | Verify canonical E.164 in output |
| **Brain path** (e.g. PHASE2-2b Knowledge) | 20 | Scenario 8 × 5 + 15 fresh Q&A | Answer correctness matters more than count |
| **Booking path** (e.g. PHASE2-2c Workflow) | 30 | Scenarios 3, 4, 5, 6 × 5 + explicit book/reschedule/cancel × 5 each | Verify calendar backend state after each |
| **Provider swap** (e.g. PHASE1 winner-wiring) | 40 | All 8 × 5 | p50/p95 latency comparison; cost per call |
| **Third-party integration** (e.g. PHASE4b CRM adapter) | 20 | All 8 × 2 + 5 forced sync failures | Verify DLQ correctly rescues failures |

**Never fewer than 5 calls.** One bad call proves nothing.
**Never fewer than 2 successes per scenario.** Otherwise "we forgot to test it."
**Never merge on Yellow scorecards** unless the yellow marker is documented as expected-for-this-branch (e.g. scenarios 6-7 will show yellow until PHASE2-2c ships full workflow-controller phone capture).

---

## When to skip the loop

Only three cases:

1. **Docs-only changes** — no runtime effect, no soak needed. Same as this doc.
2. **Test-only changes** — new test file, no source touched. Same as this doc.
3. **Config-only changes** — new setting default, provided default is off. Merge, restart, verify boot smoke, done.

Everything else runs the loop. Yes, even "trivial" changes. Doc #56 line 46 has "every production failure becomes a regression test" as the eventual state — we're building the muscle for it now.

---

## Tooling this playbook assumes exists

- `apps/api/scripts/verify-call.sh` — SHIPPED (commit `edacdec`)
- `apps/api/scripts/replay-audio.py` — SHIPPED (commit `edacdec`)
- `apps/api/scripts/soak/diff-scorecards.py` — **BLOCKED** on task #390
- `apps/api/scripts/soak/aggregate-soak.py` — **BLOCKED** on task #390
- `apps/api/scripts/soak/synth-fixtures.py` — **BLOCKED** on task #390 (optional — enables replay-only validation without dialing)

Once task #390 ships, this playbook is fully executable.

---

## Where the pattern goes long-term

PHASE5 sub-track 5.2 (failure→regression compiler) is the CI-automated version of this playbook. Every scenario becomes a YAML file with explicit asserts against the event log. `pytest` runs the whole golden library on every PR and blocks merge on any red. This playbook is the manual v1; that phase is the automated v2.

Do NOT skip the manual v1. It's how we discover which markers to actually assert on.
