# Customer journey audit — {scenario}

Author: product-lead (subagent)
Scenario: {scenario name — e.g. "Dutch expat books follow-up at dental clinic"}
Vertical: {vertical}
Date: {yyyy-mm-dd}

## Scope

Deep dive on ONE specific caller scenario. Turn-by-turn analysis of:
what the agent DID (real transcript or scripted) vs what it SHOULD
have done, plus the product gap each divergence exposes.

## Caller context

**Persona:** {from persona-ladder.md}
**Trigger call reference:** {CallSid or scripted-test name if applicable}
**Business context:** which tenant, which service catalog, which providers on staff, hours, etc.

## Transcript (real or ideal-scripted)

Full call, turn-by-turn.

```
[TS 00:00.000] AGENT: [utterance]
[TS 00:03.400] CALLER: [utterance]
[TS 00:05.100] AGENT: [utterance]
...
```

## Turn-by-turn audit

### Turn 1
- **What the agent SAID:** ...
- **What the ideal agent WOULD say:** ...
- **Divergence severity:** OK / minor / significant / broken
- **Root cause of divergence:** ...
- **Product gap exposed:** e.g. "the wider prompt doesn't tell the model how to handle ambiguous service names, so it defaults to guessing"

### Turn N
[repeat...]

## Product gaps summary

Aggregate the "product gap exposed" fields across all turns.

- **Gap 1:** {name}
  - Turns affected: {list}
  - Fix scope: {prompt change / new code / new fixture data / new tool}
  - Priority: P0 / P1 / P2 / P3
- **Gap 2:** ...

## Cross-references

- Persona-ladder entry: `docs/product/persona-ladder-{vertical}-{date}.md#{persona-name}`
- Service-taxonomy entries: which services touched
- Bad-outcome catalog: which failures fired (or almost fired)

## Recommendations for engineering

Ordered by priority. Each cites the gap it addresses.

1. **P0 — {fix name}** (addresses Gap 1)
   - Where: file / function
   - Approach: ...
   - Test: ...
   - Estimated effort: ...
2. **P1 — ...**
   [repeat...]

## Recommendations for product

- Playbook updates: which sections of `.claude/plugins/product-lead/product_playbooks/{vertical}.md` should be enriched from what we learned this audit.
- New personas / archetypes discovered: add to persona-ladder next revision.
- New failure modes discovered: add to bad-outcome catalog next revision.
