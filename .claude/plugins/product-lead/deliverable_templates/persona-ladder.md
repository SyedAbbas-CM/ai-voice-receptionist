# Persona ladder — {vertical}

Author: product-lead (subagent)
Vertical: {vertical}
Date: {yyyy-mm-dd}

## Method

Identified {N} caller archetypes by:
- Reading the vertical playbook: `.claude/plugins/product-lead/product_playbooks/{vertical}.md`
- Reviewing real call transcripts: `docs/transcripts/`
- Sanity-checking against fixture: `sample-data/{vertical}/business.json`

## Archetypes

### 1. {Name}
- **One-liner:** ...
- **What they want:** ...
- **What they know coming in:** ...
- **What they need before they trust the agent:** ...
- **What makes them hang up:** ...
- **Distinguishing utterance markers:** phrases you'd hear from them that you wouldn't from other archetypes.

### 2. {Name}
[repeat...]

## Coverage gaps

- Personas we know exist but couldn't fully characterize (need real interviews or transcript data).
- Archetypes the current agent handles poorly, ranked.

## Recommendations for engineering

- Prompt should recognize archetype from first 1-2 caller utterances and adjust tone.
- Prompt should NOT force one-size-fits-all opening.
- Bad-outcome catalog cross-links: which archetype hits which failure mode most frequently.
