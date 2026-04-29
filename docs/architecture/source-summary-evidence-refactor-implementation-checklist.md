# Source + Summary Evidence Refactor Implementation Checklist

## Goal

Refactor the Milestone 2 compiler so that every downstream LLM step after
summary generation uses both `source text` and `summary text`, aligned with the
OpenKB donor pattern of `A -> summary` and `A + summary -> downstream work`.

This refactor also promotes evidence into a first-class sub-pipeline:

- internal evidence is stored at quote-instance level
- public `wiki/evidence/*.md` remains claim-centric
- derived pages must record `used_evidence_ids`
- prompts must use stable `messages` lists to maximize LiteLLM prompt-cache reuse

## Locked Decisions

- Use direction 2: donor-aligned `source + summary` refactor plus evidence
  refactor.
- Do not restore `wiki/concepts/` or any hidden concept layer.
- Keep the public wiki taxonomy under `topics/`, `regulations/`,
  `procedures/`, `conflicts/`, and `evidence/`.
- Remove evidence generation from the taxonomy planner contract.
- Store internal evidence as verified quote instances.
- Keep `wiki/evidence/*.md` as claim-centric aggregate pages.
- Record `used_evidence_ids` on derived pages so backlinking is precise.
- Prefer LiteLLM structured outputs and Pydantic response models.
- Build prompts as ordered `messages` lists and keep document context in
  `role="assistant"` messages before the final task instruction.
- Put `document name`, `source text`, `summary text`, and brief/context blocks
  in `assistant` messages to maximize cache-token reuse across related calls.
- Use a distinct evidence `title` for display, separate from `claim_key` and
  `canonical_claim`.

## Non-Goals

- Do not bring back summary-only planning or summary-only drafting.
- Do not let LLM free-write final evidence markdown pages.
- Do not add semantic claim clustering in this phase.
- Do not force a frontend/API schema redesign unless a concrete shape change is
  required.
- Do not use `anchor: unknown` as a normal happy-path output.

## Target Compile Flow

1. Materialize source artifacts.
2. Generate summary from source.
3. Plan evidence pages from `source + summary + existing evidence briefs`.
4. Draft evidence quote instances from `source + summary + evidence plan item`.
5. Verify quotes and anchors against materialized source text.
6. Persist per-document evidence manifests under `.brain/evidence/`.
7. Plan taxonomy pages from `source + summary + verified evidence briefs`.
8. Draft taxonomy pages from `source + summary + evidence pack`.
9. Render claim-centric `wiki/evidence/*.md` from verified manifests.
10. Run cross-document conflict detection.
11. Backlink summaries, derived pages, evidence pages, and conflicts.
12. Rebuild `wiki/index.md` and validation reports.

## Prompt And Cache Contract

- Every compile-stage LLM call should use a `messages: list[dict[str, str]]`
  layout similar to the OpenKB donor flow.
- The stable prefix for downstream calls should be:

```text
1. system: compiler/task contract
2. assistant: document metadata and refs
3. assistant: source text block
4. assistant: summary text block
5. assistant: existing briefs / evidence pack / existing page body / plan item
6. user: short task-specific instruction
```

- `system` should explicitly say that assistant-provided content is data, not
  instructions.
- `user` should stay short and task-specific.
- `source text`, `summary text`, `document name`, and brief/context blocks
  should not be moved back into a single monolithic `user` message.
- Reuse the same ordered assistant-prefix helpers across:
  `summarize`, `plan evidence`, `draft evidence`, `plan taxonomy`,
  `draft topic`, `draft regulation`, `draft procedure`, `draft conflict`, and
  any conflict-confirmation LLM calls.

## Data Contract Changes

The refactor should move from a single mixed taxonomy plan to separate evidence
and taxonomy contracts.

Suggested internal shapes:

```python
@dataclass
class _MaterializedDocument:
    document: DocumentRecord
    summary_slug: str
    source_ref: str
    text_for_summary: str
    text_for_downstream: str
    downstream_source_ref: str
    summary_seed_ref: str | None = None


class EvidencePlanItem(BaseModel):
    page_slug: str = ""
    claim: str
    title: str
    brief: str = ""


class EvidencePlanActions(BaseModel):
    create: list[EvidencePlanItem] = Field(default_factory=list)
    update: list[EvidencePlanItem] = Field(default_factory=list)


class EvidenceDraftQuote(BaseModel):
    quote: str
    anchor: str
    page_ref: str = ""


class EvidenceDraftOutput(BaseModel):
    claim: str
    title: str
    brief: str
    quotes: list[EvidenceDraftQuote] = Field(default_factory=list)


class VerifiedEvidenceInstance(BaseModel):
    evidence_id: str
    page_slug: str
    claim_key: str
    canonical_claim: str
    title: str
    brief: str
    quote: str
    anchor: str
    page_ref: str = ""
    source_ref: str
    summary_link: str
    document_hash: str


class PagePlanItem(BaseModel):
    slug: str
    title: str
    brief: str = ""
    candidate_evidence_ids: list[str] = Field(default_factory=list)
```

Suggested drafting outputs:

```python
class TopicPageOutput(BaseModel):
    title: str
    brief: str
    context_markdown: str
    used_evidence_ids: list[str] = Field(default_factory=list)


class RegulationPageOutput(BaseModel):
    title: str
    brief: str
    requirement_markdown: str
    applicability_markdown: str
    authority_markdown: str
    used_evidence_ids: list[str] = Field(default_factory=list)


class ProcedurePageOutput(BaseModel):
    title: str
    brief: str
    steps: list[str] = Field(default_factory=list)
    used_evidence_ids: list[str] = Field(default_factory=list)


class ConflictPageOutput(BaseModel):
    title: str
    brief: str
    description_markdown: str
    impacted_pages: list[str] = Field(default_factory=list)
    used_evidence_ids: list[str] = Field(default_factory=list)
```

External API guidance:

- Keep `CompilePlanSummary.evidence_count` stable in the first implementation if
  possible.
- If evidence planning needs a richer frontend shape later, make that a separate
  API contract change and update `packages/app/src/pages/app-shell.tsx` in the
  same change.

## Evidence Identity, Title, And Rendering Rules

- `page_slug` is the file path identity for `wiki/evidence/<page_slug>.md`.
- `claim_key` is the normalized technical key for the canonical claim.
- `canonical_claim` is the full semantic statement stored in the page body.
- `title` is the human-readable display label used in frontmatter, H1, and
  index entries.
- For new evidence pages, default `page_slug = claim_key`.
- For evidence updates, the planner must return the exact existing `page_slug`.
- The display `title` should not be used as the file path identity.
- Prefer title format `<Subject>: <normalized assertion>` when possible.
- Keep material qualifiers that change the meaning of the claim.
- Avoid putting provenance metadata into the title unless the authority itself
  is part of the claim.

Target evidence page shape:

```yaml
page_id: evidence:<page_slug>
page_type: evidence
title: Amoxicillin: First-line for acute otitis media unless beta-lactamase coverage is needed
claim_key: amoxicillin-first-line-for-acute-otitis-media-unless-beta-lactamase-coverage-is-needed
brief: First-line AOM treatment recommendation with resistance qualifier.
source_summaries:
- '[[summaries/amoxicillin-2024-04-10-1-md-72538aa8]]'
```

```md
# Evidence: Amoxicillin: First-line for acute otitis media unless beta-lactamase coverage is needed

## Canonical Claim
...

## Supporting Quotes
> ...
- source: [[summaries/...]]
- anchor: `...`

## Source Summaries
- [[summaries/...]]
```

## Phase 1: Workspace And Internal Compiler Contract

Files:
`services/evidence-compiler/src/evidence_compiler/compiler/pipeline.py`
`services/evidence-compiler/src/evidence_compiler/schema/workspace.py`

Checklist:
- [ ] Extend `_MaterializedDocument` with downstream compile context fields such
  as `text_for_downstream` and `downstream_source_ref`.
- [ ] Treat per-document evidence manifests under `.brain/evidence/` as the
  authoritative internal evidence store.
- [ ] Add `.brain/evidence/by-document` to workspace initialization layout.
- [ ] Keep `wiki/evidence/*.md` as rendered compiler-managed output, not the
  source of truth.
- [ ] Decide and document whether stale compiler-managed evidence pages may be
  removed during authoritative re-render.

## Phase 2: Prompt Helper Refactor For Cacheable Message Prefixes

Files:
`services/evidence-compiler/src/evidence_compiler/compiler/pipeline.py`

Checklist:
- [ ] Introduce shared prompt helper(s) that build the stable OpenKB-style
  `system + assistant-prefix + user-instruction` message list.
- [ ] Put `document name`, `source ref`, `source text`, `summary brief`, and
  `summary markdown` into ordered `assistant` messages.
- [ ] Keep downstream `user` messages short and task-specific.
- [ ] Preserve LiteLLM structured output calls with Pydantic response models.
- [ ] Apply the shared message builder to every downstream compile LLM call.

## Phase 3: Downstream Source Context Materialization

Files:
`services/evidence-compiler/src/evidence_compiler/compiler/pipeline.py`
`services/evidence-compiler/src/evidence_compiler/converter/pipeline.py`

Checklist:
- [ ] Keep `text_for_summary` optimized for summary generation.
- [ ] Add `text_for_downstream` optimized for evidence and taxonomy work.
- [ ] For short docs, allow summary and downstream text to reuse the same
  materialized source markdown.
- [ ] For long docs, keep `text_for_summary` compact if needed but provide
  `text_for_downstream` from the pageindex-derived source artifact rather than
  summary-only seed text.
- [ ] Ensure downstream source refs can point to pageindex/page-based source
  artifacts when relevant.

## Phase 4: Evidence Planning Contract

Files:
`services/evidence-compiler/src/evidence_compiler/compiler/pipeline.py`
`tests/integration/test_milestone_a.py`

Checklist:
- [ ] Remove `evidence` from `TaxonomyPlanResult`.
- [ ] Add `EvidencePlanItem` and `EvidencePlanActions`.
- [ ] Add an evidence-planning stage before taxonomy planning.
- [ ] Feed evidence planning with `source + summary + existing evidence briefs`.
- [ ] Let evidence planning choose `create` versus `update` claim-centric pages.
- [ ] Require exact existing `page_slug` for evidence updates.
- [ ] Keep evidence planning structured and claim-centric, but do not ask it to
  generate final markdown pages.
- [ ] Update fake LLM payloads and compile-plan test fixtures for the new
  evidence-planning contract.

## Phase 5: Evidence Drafting, Verification, And Manifest Persistence

Files:
`services/evidence-compiler/src/evidence_compiler/compiler/pipeline.py`
`tests/integration/test_milestone_a.py`

Checklist:
- [ ] Add structured evidence-drafting output that returns quote instances, not
  final markdown.
- [ ] Feed evidence drafting with `source + summary + evidence plan item`.
- [ ] Verify drafted quotes against materialized source text with whitespace-safe
  normalization.
- [ ] Resolve anchors against actual headings, source sections, or page refs.
- [ ] Reject unverifiable quotes from the manifest instead of rendering them into
  wiki output.
- [ ] Persist verified per-document evidence instances under
  `.brain/evidence/by-document/<file_hash>.json`.
- [ ] Generate stable `evidence_id` values so later drafting and backlinking can
  reference exact quote instances.
- [ ] Write an evidence validation report under `wiki/reports/` for dropped or
  unverifiable items.

## Phase 6: Taxonomy Planning From Source, Summary, And Verified Evidence

Files:
`services/evidence-compiler/src/evidence_compiler/compiler/pipeline.py`
`tests/integration/test_milestone_a.py`

Checklist:
- [ ] Run taxonomy planning only after verified evidence manifests exist.
- [ ] Keep taxonomy planning focused on `topics`, `regulations`,
  `procedures`, and `conflicts`.
- [ ] Feed taxonomy planning with `source + summary + existing taxonomy briefs +
  document evidence briefs`.
- [ ] Extend `PagePlanItem` with `candidate_evidence_ids` or equivalent internal
  references to the relevant evidence pool.
- [ ] Keep code-side heuristics such as `_planning_context_text`,
  `_is_informational_reference_document`, `_has_explicit_role_workflow`,
  `_has_explicit_conflict_signal`, and `_has_normative_reference_signal`
  grounded in `source + summary`, not summary alone.
- [ ] Continue to keep `CompilePlanSummary.evidence_count` stable unless a
  deliberate API change is approved in the same refactor.

## Phase 7: Taxonomy Drafting With Verified Evidence Packs

Files:
`services/evidence-compiler/src/evidence_compiler/compiler/pipeline.py`
`tests/integration/test_milestone_a.py`

Checklist:
- [ ] Add `used_evidence_ids` to every derived-page structured output model.
- [ ] Build compact evidence packs from `candidate_evidence_ids` before drafting
  each page.
- [ ] Feed each page drafter with `source + summary + plan item + evidence pack +
  existing body`.
- [ ] Validate returned `used_evidence_ids` as a subset of the offered evidence
  pack.
- [ ] Keep compiler-managed backlink sections excluded from rewrite context.
- [ ] Use `used_evidence_ids` as the authoritative record of which evidence a
  page actually relied on.

## Phase 8: Claim-Centric Evidence Page Rendering

Files:
`services/evidence-compiler/src/evidence_compiler/compiler/pipeline.py`

Checklist:
- [ ] Remove the current in-memory `plan.evidence` merge-and-write block.
- [ ] Replace it with manifest-driven grouping and deterministic rendering.
- [ ] Group verified evidence instances by rendered evidence-page identity.
- [ ] Use planner-selected `page_slug` for updates and `claim_key`-derived slug
  for new creates.
- [ ] Store `title` in evidence frontmatter and use it in the H1.
- [ ] Keep `Canonical Claim`, `Supporting Quotes`, and `Source Summaries`
  sections code-rendered.
- [ ] Rebuild `source_summaries` from verified manifest data.
- [ ] Remove stale compiler-managed evidence pages only when they are absent from
  the authoritative render set.

## Phase 9: Backlinking, Conflicts, Index, And Progress Signals

Files:
`services/evidence-compiler/src/evidence_compiler/compiler/pipeline.py`
`services/shared/knowledge-models/src/knowledge_models/compiler_api.py`
`services/shared/knowledge-models/src/knowledge_models/__init__.py`
`services/evidence-compiler/src/evidence_compiler/models.py`
`packages/app/src/pages/app-shell.tsx`

Checklist:
- [ ] Backlink summary pages to all derived pages as before.
- [ ] Backlink derived pages to evidence pages based on `used_evidence_ids`, not
  every evidence entry from the same summary.
- [ ] Derive `Related Evidence` from actual evidence usage, not summary-wide
  attachment.
- [ ] Keep cross-document conflict detection after evidence and derived pages are
  available.
- [ ] Rebuild `wiki/index.md` with display-friendly evidence titles.
- [ ] Add or update stage counters/messages for `planning-evidence`,
  `drafting-evidence`, `verifying-evidence`, and `writing-evidence`.
- [ ] Only touch shared schema and frontend types if a concrete API response
  shape changes; otherwise keep the external shape stable.

## Phase 10: Tests And Quality Gate

Files:
`tests/integration/test_milestone_a.py`
`services/evidence-compiler/src/evidence_compiler/compiler/pipeline.py`

Checklist:
- [ ] Update the fake LiteLLM payload helper to cover evidence planning,
  evidence drafting, and `used_evidence_ids`.
- [ ] Add a short-doc integration scenario that proves downstream planning and
  drafting prompts use both source and summary context.
- [ ] Add an evidence-verification assertion showing unverifiable quotes are
  dropped from manifest and wiki output.
- [ ] Add a rerun assertion showing the same compile does not create new stale or
  near-duplicate evidence pages on each run.
- [ ] Add a long-doc/pageindex assertion showing downstream prompts use the
  pageindex-derived source artifact rather than summary-only seed text.
- [ ] Run `pytest tests/integration/test_milestone_a.py`.
- [ ] Run `npx basedpyright`.
- [ ] If shared API response shapes change, run `pnpm typecheck` and update
  `packages/app/src/pages/app-shell.tsx` in the same change.

## Definition Of Done

- [ ] No downstream planner or drafter relies on summary-only prompts.
- [ ] Every post-summary LLM call uses stable message lists with assistant-held
  document context for prompt-cache reuse.
- [ ] Evidence manifests are the authoritative internal evidence store.
- [ ] `wiki/evidence/*.md` pages are rendered from verified manifests, not
  directly from raw planner output.
- [ ] No rendered evidence quote exists only in the summary while missing from
  the source artifact.
- [ ] No happy-path rendered evidence uses `anchor: unknown`.
- [ ] Derived pages record `used_evidence_ids` and backlinking uses those ids.
- [ ] Long-document downstream prompts use source-derived pageindex artifacts in
  addition to summary context.
- [ ] Re-running compile is idempotent for compiler-managed evidence pages.
