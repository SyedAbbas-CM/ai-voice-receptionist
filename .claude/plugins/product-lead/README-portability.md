# Portability — installing product-lead in another project

The plugin is designed to be dropped into any Claude Code project
without modification. No hard-coded paths to this specific repo, no
dependencies on our humanness_events / trace endpoint / etc.

## Install recipe

### Step 1. Copy the plugin directory

```bash
# From the source project's root
cd /path/to/other-project
cp -r /path/to/source-project/.claude/plugins/product-lead .claude/plugins/
```

Or as a git subtree if you want to pull updates:

```bash
cd /path/to/other-project
git subtree add --prefix=.claude/plugins/product-lead \
  <this-repo-url> main --squash
```

### Step 2. Verify Claude Code picks it up

Restart your Claude Code session (or run `/agents` to re-scan). You
should see `product-lead` listed.

### Step 3. Pick your vertical

Look under `product_playbooks/`. If one matches your project, you're
done — invoke the agent.

If your vertical is new, copy the template:

```bash
cp .claude/plugins/product-lead/product_playbooks/_template.md \
   .claude/plugins/product-lead/product_playbooks/YOUR-VERTICAL.md
```

Then fill in sections 1-8. The agent will prompt for missing content
on first invocation.

### Step 4. Invoke

```
Use the product-lead agent to produce a persona ladder for the
YOUR-VERTICAL vertical.
```

## What the plugin assumes about the host project

Minimum:
- **Markdown-friendly.** Deliverables are `.md` files under `docs/product/`.
- **Optional:** if the host project has `docs/transcripts/`, the
  agent will bisect real caller behavior from there.
- **Optional:** if the host project has `sample-data/{vertical}/business.json`
  or similar, the agent will use it as one input to the service
  taxonomy.

No dependencies on:
- Any specific programming language / framework
- Any specific LLM provider
- Any specific database / infra
- Any specific observability schema
- Any specific test framework

## What the plugin does NOT assume

- It does NOT assume the host is a voice-agent project. Chat / SMS /
  email-agent projects work just as well — the personas and
  taxonomies apply.
- It does NOT assume the host has our humanness_events schema. That's
  a source-project-specific thing.
- It does NOT assume any particular Claude Code plugin manager. Just
  standard `.claude/plugins/` directory convention.

## Adapting deliverable templates

The templates under `deliverable_templates/` are opinionated but
adjustable. Fork them per-project if your reporting style differs.
Just edit in place — the agent reads them at invocation time.

## Updates from source

When you fork/copy, updates in the source project don't automatically
propagate. Two ways to keep in sync:

1. **Manual re-copy** — `cp -r .../product-lead .claude/plugins/`
   overwrites everything. Safe unless you customized templates.
2. **Git subtree pull** — if you installed via subtree, `git subtree
   pull ...` handles merges.

Playbooks are project-specific by design — never overwrite
`product_playbooks/*` from a source-project update without merging
your local additions.

## Reporting a bug in the plugin

Bug lives with the source project (whichever one you copied from).
Playbooks that were wrong for your vertical are YOUR playbook to fix
locally.

## Version compatibility

Requires Claude Code with `.claude/plugins/` subagent support. No
version pin — the plugin uses only Markdown + the standard subagent
YAML frontmatter, both of which are stable surfaces.
