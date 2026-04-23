# Milestone 2 Compilation Plan

This document records the Milestone 2 decisions that are now locked for
implementation. It refines `docs/FINAL_PLAN.md` and keeps the compile pipeline
aligned with the OpenKB donor flow while preserving Evidence Brain's product
taxonomy.

## Goal

Milestone 2 rebuilds the compiler around a deterministic, taxonomy-native wiki
pipeline that turns workspace `raw/` documents into auditable pages under:

- `wiki/summaries/`
- `wiki/topics/`
- `wiki/regulations/`
- `wiki/procedures/`
- `wiki/conflicts/`
- `wiki/evidence/`

The implementation should borrow compile orchestration ideas from OpenKB
summary/planning/backlink/index steps, but it must not restore
`wiki/concepts/` or any hidden concept abstraction.

## Locked Decisions

### 1. Public Wiki Taxonomy

- The public wiki stays taxonomy-native from the start.
- `wiki/concepts/` is not part of Milestone 2 output.
- A hybrid design is out of scope: no internal `concepts` planning layer that
  later maps into `topics/regulations/procedures/conflicts/evidence`.

### 2. Compile Engine

- Milestone 2 compile uses code-orchestrated pipeline steps plus LiteLLM calls.
- DeepAgent is not the primary compile artifact writer.
- DeepAgent may be used later for semantic lint, review, or repair workflows,
  but not for the main wiki write path.

### 3. Structured Outputs

- Compiler LLM calls should prefer LiteLLM structured outputs.
- Use `response_format`, `json_schema`, or Pydantic response models whenever the
  selected provider/model supports them.
- Avoid defaulting to free-form JSON parsing and repair logic for new compile
  stages.

### 4. Shared Subject Across Page Types

- The same domain subject may appear across multiple page types.
- This is intentional, not duplication, because each page type has a different
  contract:
  - `topics/` explain what the subject is and why it matters.
  - `regulations/` capture binding requirements, scope, and authority.
  - `procedures/` capture operational steps and execution flow.
  - `conflicts/` record explicit mismatches between pages or sources.
- Cross-links between these pages are required.

### 5. Evidence Page Model

- `evidence/` uses one page per canonical claim.
- Each evidence page may contain multiple supporting quotes, anchors, and
  source references.
- Milestone 2 should merge evidence primarily by normalized claim key, not by
  aggressive semantic clustering.

### 6. Cross-Document Conflict Detection

- Conflict pages may be created across multiple compiled documents.
- Milestone 2 should use incremental conflict detection, not full wiki pairwise
  comparison.
- Candidate selection should be narrowed by code first, then confirmed by LLM
  only on a smaller shortlist.

## Page Contracts

### `summaries/`

- One page per source document.
- Includes stable frontmatter such as document type and source location.
- Links to all generated topic, regulation, procedure, conflict, and evidence
  pages derived from the document.

### `topics/`

- Explanatory synthesis pages.
- Focus on meaning, relevance, and context.
- Must not become procedural checklists or policy matrices.

### `regulations/`

- Normative requirement pages.
- Focus on obligations, applicability, authority, exceptions, and provenance.

### `procedures/`

- Operational execution pages.
- Focus on ordered steps, roles, and workflow guidance.

### `conflicts/`

- Explicit conflict records between sources or compiled pages.
- Should link to impacted regulations, procedures, topics, summaries, and
  evidence when relevant.

### `evidence/`

- Claim-centric evidence pages.
- Contain one canonical claim plus many supporting quote blocks.
- Every quote block should preserve source and anchor information.

## Compiler Flow

### 1. Source Materialization

- Short documents compile from normalized markdown in `wiki/sources/`.
- Long PDF documents first run through `pageindex-adapter`.
- Long-document indexing must materialize both source artifacts and a summary
  seed page before downstream compile steps.

### 2. Summary Stage

- Short documents generate a typed summary result that includes at least:
  - document brief
  - summary markdown
- Long documents generate a typed overview from the PageIndex summary seed.

### 3. Taxonomy Planning Stage

- The planner outputs taxonomy-native actions directly.
- No `concept` or `concept-like` intermediate model is allowed.
- The planner decides `create`, `update`, and `related` actions separately for:
  - `topics`
  - `regulations`
  - `procedures`
  - `conflicts`
- Evidence planning should produce claim-centric evidence actions.

### 4. Typed Page Drafting and Writing

- After taxonomy planning, the compiler should call LiteLLM again for each
  `create` and `update` action in:
  - `topics`
  - `regulations`
  - `procedures`
  - `conflicts`
- Each page type uses its own typed output schema as the structured-output
  contract for draft generation.
- `create` drafts a new page body from the summary plus the planner item, then
  writes the page.
- `update` reads the existing page body, strips compiler-managed backlink and
  provenance sections from the prompt context, then rewrites the full page while
  preserving frontmatter invariants and source tracking.
- If structured draft generation fails, Milestone 2 may fall back to a minimal
  deterministic code-generated page shape so the compile job can still complete.
- `related` remains code-only and does not trigger a full page rewrite.
- `related` should be treated as provenance attachment to an existing page,
  primarily to preserve source-summary tracking for pages that were relevant but
  did not need rewritten content.

### 5. Evidence Merge

- Evidence pages are keyed by normalized claim.
- New supporting quotes append into an existing evidence page when the claim key
  already exists.
- Milestone 2 should avoid expensive semantic merge passes unless a later stage
  explicitly adds them.

### 6. Backlinking

- Summary pages must link to all derived pages.
- Derived pages must link back to their source summary pages.
- Related pages should also link to evidence and conflict pages when applicable.
- Backlinks should be added by code, not left to prompt compliance alone.
- The OpenKB donor flow has overlap between its `related` concept-page mutation
  pass and its concept-side backlink pass; Milestone 2 should not copy that
  overlap directly.
- In Evidence Brain, the centralized backlink step is the authoritative place
  for generated summary, derived-page, evidence, and conflict link sections.
- `related` is still required, but only as a code-driven provenance update on an
  existing page rather than as a separate "see also" backlink writer.

### 7. Index Update

- `wiki/index.md` must be updated by section:
  - `Summaries`
  - `Topics`
  - `Regulations`
  - `Procedures`
  - `Conflicts`
  - `Evidence`
- Entries should include brief text when available.

### 8. Lint and Job Progress

- Compile writes structural lint reports into `wiki/reports/`.
- The first implementation pass may keep the current `writing-*` stage names
  even when those stages now include both LLM drafting and file writes.
- Job stages should report real compile progress, for example:
  - `preparing`
  - `indexing-long-docs`
  - `summarizing`
  - `planning-taxonomy`
  - `writing-topics`
  - `writing-regulations`
  - `writing-procedures`
  - `writing-conflicts`
  - `writing-evidence`
  - `backlinking`
  - `updating-index`
  - `linting`
  - `completed`

## Cost and Complexity Notes

### Evidence Aggregation

- One page per claim with multiple quotes does not inherently require more LLM
  calls.
- In Milestone 2, evidence merge should be handled mostly by normalized claim
  keys and code-level append logic.
- Extra LLM cost only appears if a later phase adds semantic claim merging.

### Cross-Document Conflicts

- Allowing cross-document conflict detection does add some cost.
- Milestone 2 should control this by narrowing candidates in code before asking
  the LLM to confirm a conflict.
- Preferred comparison focus for Milestone 2:
  - `regulations` vs `regulations`
  - `regulations` vs `procedures`
  - `procedures` vs `procedures`

## Implementation Order

1. Add stable wiki document identity fields needed for summaries, backlinks, and
   index updates.
2. Implement long-document source and summary materialization through the
   PageIndex adapter.
3. Replace the one-shot extraction pipeline with typed summary and taxonomy
   planning stages.
4. Implement LLM-backed page drafting for `topics`, `regulations`,
   `procedures`, and planner-created `conflicts`, with separate `create` and
   `update` prompt paths plus deterministic fallback shapes.
5. Keep `related` as code-only provenance attachment, then add centralized
   code-driven backlink and section-aware index update logic.
6. Add incremental cross-document conflict checks.
7. Expand lint and integration tests to match the new compile contract,
   including typed page draft schemas.

## Out of Scope for Milestone 2

- Reintroducing `wiki/concepts/`.
- Using DeepAgent as the primary compiler executor.
- Global full-pairwise conflict scans across the entire wiki.
- Aggressive semantic evidence clustering beyond normalized claim keys.
