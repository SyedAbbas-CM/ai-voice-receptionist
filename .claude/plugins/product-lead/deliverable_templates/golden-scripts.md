# Golden call scripts — {vertical}

Author: product-lead (subagent)
Vertical: {vertical}
Date: {yyyy-mm-dd}

## Purpose

Fully-written call transcripts of what an IDEAL receptionist would
say in each scenario. Two uses:
1. **Regression tests** — engineers script the caller side and assert
   the agent's output matches this shape.
2. **Prompt reference** — the wider system prompt cites these as
   examples of persona / brevity / turn structure done right.

## Format

Each script:
- Header: scenario name, persona (from persona-ladder.md), difficulty
- Turn-by-turn: `CALLER:` and `AGENT:` alternating
- Annotations in `[[double brackets]]` explaining WHY the ideal
  agent said what it said

## Happy-path scripts (~10)

### 1. {Scenario name}
**Persona:** {archetype from persona-ladder.md}
**Difficulty:** easy / medium / hard
**Coverage:** which services + slots + tools this exercises

```
AGENT: [opening greeting matching persona]
CALLER: [utterance]
AGENT: [response]
  [[why this response: the ideal receptionist would X because Y]]
CALLER: [next utterance]
...
AGENT: [confirmation + call close]
```

## Failure-recovery scripts (~8-10)

### 1. {Failure scenario}
**Trigger:** what goes wrong (e.g. caller says ambiguous phrase, network hiccup mid-turn, caller changes mind mid-booking)
**Expected recovery:** what the ideal agent does to save the interaction

```
[turn-by-turn with the failure and the recovery]
```

## Edge-case scripts (~5-7)

Things a receptionist rarely handles but MUST handle correctly:
- AI disclosure when directly asked
- Emergency triage (must escalate, not book)
- Non-English caller
- Caller with severe stutter / speech impediment
- Caller who's on speakerphone with noisy background
- Caller who dictates instead of converses (elderly, giving info in one long stream)
- Caller who tries to jailbreak / prompt-inject

## What each script validates

| Script | Validates |
|---|---|
| Happy-1 | resolve_service exact match + phone slot capture + book_appointment success |
| Happy-2 | recall exam booking + returning-caller resolver |
| Fail-1 | ambiguous service → clarification loop → resolved → booking |
| Fail-2 | wrong service booked → mid-turn correction → cancel + rebook |
| Edge-1 | AI disclosure trigger → correct response |
| ... | ... |

## Recommendations for engineering

- Convert each script to a pytest fixture in `apps/api/tests/test_golden_calls_{vertical}.py`.
- Scripted-LLM harness: feed caller lines, assert agent's response matches expected shape (fuzzy match, not exact — persona wobble is OK).
- Track which scripts pass/fail per week — regression signal.
