# product-lead — reusable product-thinking subagent

Drop-in subagent for any Claude Code project building a conversational
agent product (voice, chat, IVR, whatever). It thinks like the head
of product, not the engineer — reviews features from the end user's
POV, produces customer journey docs, persona ladders, service
taxonomies, golden call scripts, bad-outcome catalogs.

Vertical-agnostic. You tell it "vertical=X" and it consults
`product_playbooks/X.md` for domain knowledge. Ships with populated
playbooks for clinic (dental/medical/vet), restaurant, and
real-estate. Stubs for salon and home-services. Empty template for
any vertical you add.

## What it does

The persistent gap in most engineering-led projects: nobody stops to
ask "what would a real receptionist / concierge / dispatcher do
next?" Engineers build what's easy. Product-lead is the missing
voice that asks the caller-POV questions the code was never designed
to answer.

Five deliverable shapes:

1. **Persona ladder** — 5-8 caller archetypes for the vertical
2. **Service taxonomy** — the full menu of things the business does,
   with per-service required-info / duration / cross-sell hooks
3. **Golden call scripts** — 20-30 fully written transcripts of what
   an ideal agent would say in each scenario. Become regression tests
   AND prompt reference.
4. **Bad-outcome catalog** — every way a call can fail from the
   caller's POV, ordered by severity + frequency
5. **Customer journey audit** — deep dive on one specific caller
   scenario, turn-by-turn what-agent-did vs what-agent-should-do

It NEVER writes application source code. It writes Markdown
deliverables that engineers then implement from.

## How to invoke

From Claude Code, once this plugin is installed in `.claude/plugins/`:

```
/agents product-lead
```

Or via the `Agent` tool with `subagent_type: "product-lead"`.

Always invoke with a **vertical** in scope. Examples:

```
Use the product-lead agent to produce a persona ladder for our
clinic vertical.
```

```
Ask product-lead to audit the caller journey where a Dutch expat
calls a dental clinic asking for "a follow-up."
```

If the vertical isn't in `product_playbooks/`, the agent will
prompt you to create one from `_template.md`.

## Installation in another project

See `README-portability.md` for the full copy-out-of-repo recipe.
Short version:

```bash
cp -r .claude/plugins/product-lead /path/to/other-project/.claude/plugins/
```

Then edit the playbooks for that project's verticals.

## Directory structure

```
.claude/plugins/product-lead/
├── README.md                           # this file
├── README-portability.md               # install-in-other-project recipe
├── agents/
│   └── product-lead.md                 # the agent definition
├── product_playbooks/                  # vertical domain knowledge
│   ├── _template.md                    # blank template for new verticals
│   ├── clinic.md                       # dental/medical/vet
│   ├── restaurant.md                   # restaurant/cafe/bar
│   ├── real-estate.md                  # brokerage/rental/property mgmt
│   ├── salon.md                        # (create when needed)
│   └── home-services.md                # (create when needed)
└── deliverable_templates/              # scaffolds for each deliverable
    ├── persona-ladder.md
    ├── service-taxonomy.md
    ├── golden-scripts.md
    ├── bad-outcome-catalog.md
    └── customer-journey-audit.md
```

## Output location

Deliverables land under `docs/product/` in the invoking project.
Directory is created if missing.

## Playbook maintenance

Every session should leave the playbook richer. If the agent learns
something during a session that belongs in the playbook (a real
service the fixture didn't have, a persona we missed, a failure mode
we hadn't cataloged), it updates the playbook before finishing.

Playbooks are compounding assets. Treat them like the wiki they are.
