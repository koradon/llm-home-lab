# Templates and Naming

All starter templates live in `docs/llm/templates/`. Copy the relevant file when creating a new document.

## Available templates

| Template | Path | Use for |
| --- | --- | --- |
| Idea | `docs/llm/templates/idea.md` | Early concepts that may be promoted or rejected |
| Spec | `docs/llm/templates/spec.md` | Feature behavior, requirements, contracts |
| Acceptance (BDD) | `docs/llm/templates/acceptance.feature` | Gherkin acceptance scenarios for a spec |
| Plan | `docs/llm/templates/plan.md` | Implementation approach for a spec |
| ADR (light) | `docs/llm/templates/adr-light.md` | Small or obvious decisions |
| ADR (standard) | `docs/llm/templates/adr-standard.md` | Decisions with real alternatives |
| ADR (full) | `docs/llm/templates/adr-full.md` | Major architectural decisions |
| Roadmap | `docs/llm/templates/roadmap.md` | Now / Next / Later horizon pages |
| Runbook | `docs/llm/templates/runbook.md` | Operations when `docs/runbooks/` exists |
| Reference | `docs/llm/templates/reference.md` | CLI/API/config reference when added |

## File naming

| Type | Pattern | Example |
| --- | --- | --- |
| ADR | `NNNN-short-title.md` | `0001-use-sqlite.md` |
| Idea | `short-idea-name.md` | `cache-invalidation.md` |
| Spec | `short-feature-name.md` | `cli-init.md` |
| Acceptance (BDD) | `short-feature-name.feature` | `cli-init.feature` |
| Plan (active) | `short-feature-name.md` | `cli-init.md` |
| Plan (completed) | `short-feature-name.completed.md` | `cli-init.completed.md` |
| Plan (revision) | `short-feature-name-v2.md` | `cli-init-v2.md` |
| Roadmap (horizon page) | `now.md` / `next.md` / `later.md` | `now.md` |
| Runbook | `short-operation-name.md` | `local-development.md` |
| Reference | `short-topic-name.md` | `cli-commands.md` |

Use lowercase kebab-case for new files unless an existing convention differs.

ADR numbers are sequential and monotonic — never reuse a number.

## Document structure

Use markdown sections for metadata. Do not use YAML frontmatter.

Common sections:

- `## Status` — document lifecycle state
- `## Related` — links to related Ideas, specs, plans, or ADRs

### Status values

| Document | Status values |
| --- | --- |
| Idea | `draft`, `accepted`, `rejected` |
| Spec | `draft`, `accepted`, `deprecated` |
| Plan | `draft`, `active`, `completed`, `superseded` |
| ADR | `proposed`, `accepted`, `deprecated`, `superseded` |
| Roadmap | `draft`, `active` |

## Update semantics

- Prefer editing the smallest relevant section.
- Keep prior decisions visible; do not rewrite history unless correcting errors.
- Link related Ideas, specs, plans, and ADRs in `## Related`.
