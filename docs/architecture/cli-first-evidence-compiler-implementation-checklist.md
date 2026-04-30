# CLI-First Evidence Compiler Plan and Implementation Checklist

## Goal

Pivot the project toward a CLI-first Evidence Compiler that external coding
agents can invoke reliably, while making the compiler codebase easier to review
by breaking `services/evidence-compiler/src/evidence_compiler/compiler/pipeline.py`
into a small set of cohesive modules.

This document is both the architecture plan and the implementation checklist for
that transition.

## Current State Summary

- `services/evidence-compiler` already contains the real product logic:
  workspace initialization, document ingest, credentials, compile pipeline,
  linting, and job persistence.
- `services/evidence-compiler` already ships a basic CLI entrypoint, but it is
  still developer-oriented rather than agent-oriented.
- `services/brain-service` is mainly a local HTTP facade plus watch/job
  orchestration for the desktop and app shell.
- `apps/desktop` is a thin Tauri shell.
- `packages/app` is a thin client that polls `brain-service` and renders state.
- `compiler/pipeline.py` currently mixes schemas, LiteLLM wrappers, markdown
  normalization, planning logic, evidence verification, page rendering, and
  top-level orchestration in one file.

## Locked Decisions

- `services/evidence-compiler` is the product core.
- The primary public product surface becomes the CLI.
- The compiler remains library-first internally. The CLI is the primary UX, not
  the only integration path.
- `services/brain-service` remains a temporary compatibility shim, not the place
  for new business logic.
- `apps/desktop`, `packages/app`, and `packages/ui` enter feature freeze.
- Skill v1 for external agents does not depend on watch mode.
- The first refactor pass prioritizes readability and reviewability, not compile
  behavior changes.
- The public compiler surface remains stable during refactor:
  `CompileArtifacts`, `compile_documents(...)`, and `rebuild_index(...)`.
- The existing workspace layout and `.brain/` persistence contract stay intact.
- Prefer a few cohesive modules over many tiny files.
- Keep current integration tests green throughout the refactor.

## Scope

In scope:

- Split `compiler/pipeline.py` into cohesive modules.
- Keep `pipeline.py` as the orchestration facade.
- Preserve existing compile behavior during the modularization pass.
- Harden the CLI for agent usage with structured output and stable failure
  semantics.
- Freeze the legacy service and UI layers while CLI parity is built.
- Document the deprecation path for desktop and web layers.

Out of scope for the first pass:

- Large compile behavior redesign mixed into the refactor.
- Replacing the compiler with a separate HTTP worker service.
- Rebuilding watch mode as part of the initial agent skill surface.
- Removing compatibility layers before the CLI is production-ready.
- Reworking the workspace artifact model.

## Target Architecture

The long-term shape should be:

```text
services/
  evidence-compiler/
    src/evidence_compiler/
      api.py
      cli/
      compiler/
        __init__.py
        pipeline.py
        models.py
        llm.py
        summaries.py
        planning.py
        evidence.py
        pages.py
      state/
      credentials.py
      config.py
  brain-service/
    src/brain_service/
      main.py
      watch_manager.py
```

Responsibilities:

- `api.py`: stable library API for workspaces, documents, credentials, compile,
  and status.
- `cli/`: agent-facing and human-facing command surface.
- `compiler/pipeline.py`: stage orchestration only.
- `compiler/models.py`: compile-local schemas and dataclasses.
- `compiler/llm.py`: LiteLLM structured-output helpers and retry behavior.
- `compiler/summaries.py`: source materialization, summary prompts, summary page
  writing, and markdown normalization.
- `compiler/planning.py`: evidence planning, taxonomy planning, plan
  reconciliation, and plan summary building.
- `compiler/evidence.py`: evidence drafting, verification, manifest I/O, and
  evidence page rendering.
- `compiler/pages.py`: typed page drafting, markdown page I/O, page upsert
  helpers, backlinks, and conflict confirmation helpers.
- `brain-service`: compatibility-only HTTP shim during migration.

## Reviewability Rules

- Move code by functional cohesion, not by arbitrary line count.
- Prefer pure helpers and local imports over creating new cross-module
  abstractions without a real need.
- Preserve current behavior before improving CLI ergonomics.
- Avoid mixing refactor-only changes with compile algorithm changes in the same
  patch series.
- Keep comments and docstrings short and review-oriented.

## Compiler Module Split Proposal

### `compiler/models.py`

Move these compile-local dataclasses, protocols, and Pydantic models:

- `DraftCreateUpdateFn`
- `CompileArtifacts`
- `_MaterializedDocument`
- `SummaryStageResult`
- `PagePlanItem`
- `PagePlanActions`
- `EvidencePlanItem`
- `EvidencePlanActions`
- `EvidenceDraftQuote`
- `EvidenceDraftOutput`
- `EvidenceValidationIssue`
- `VerifiedEvidenceInstance`
- `EvidenceDocumentManifest`
- `TaxonomyPlanResult`
- `TopicPageOutput`
- `RegulationPageOutput`
- `ProcedurePageOutput`
- `ConflictPageOutput`
- `ConflictCheckResult`
- `_EvidencePageState`

### `compiler/llm.py`

Move LiteLLM response handling and structured-output helpers:

- `_safe_json`
- `_looks_like_truncated_json`
- `_preview_text`
- `_StructuredResponseTruncatedError`
- `_add_completion_error_note`
- `_response_field`
- `_extract_first_choice`
- `_extract_completion_content`
- `_extract_finish_reason`
- `_extract_usage`
- `_should_retry_without_structured_output`
- `_is_json_invalid_validation`
- `_is_truncated_structured_validation`
- `_validate_structured_response`
- `_validate_unstructured_response`
- `_structured_completion`
- `_structured_acompletion`

### `compiler/summaries.py`

Move source materialization, summary prompts, and markdown normalization:

- `_to_int`
- `_relative_ref`
- `_slugify`
- `_derive_brief`
- `_is_structured_markdown_line`
- `_fence_marker`
- `_normalize_inline_markdown_structure`
- `_reflow_markdown_paragraphs`
- `_normalize_summary_markdown`
- `_materialize_short_document`
- `_materialize_long_document`
- `_summary_messages`
- `_summarize_document`
- `_write_summary_page`

### `compiler/planning.py`

Move planning prompts, heuristics, and plan post-processing:

- `_json_blob`
- `_downstream_messages`
- `_evidence_planner_messages`
- `_taxonomy_planner_messages`
- `_normalize_claim_key`
- `_normalize_plan_item`
- `_normalize_evidence_plan_item`
- `_sanitize_taxonomy_plan`
- `_sanitize_evidence_plan`
- `_contains_any`
- `_planning_context_text`
- `_is_informational_reference_document`
- `_has_explicit_role_workflow`
- `_has_explicit_conflict_signal`
- `_has_normative_reference_signal`
- `_item_implies_no_conflict`
- `_reconcile_page_actions`
- `_reconcile_evidence_actions`
- `_filter_candidate_evidence_ids`
- `_finalize_evidence_plan`
- `_finalize_taxonomy_plan`
- `_plan_evidence`
- `_plan_taxonomy`
- `_build_plan_bucket`
- `_merge_plan_buckets`
- `_build_compile_plan_summary`

### `compiler/evidence.py`

Move evidence-specific persistence, verification, drafting, and rendering:

- `_summary_link`
- `_evidence_manifest_path`
- `_write_evidence_manifest`
- `_load_evidence_manifests`
- `_extract_section`
- `_bootstrap_evidence_pages_from_wiki`
- `_group_evidence_pages`
- `_existing_evidence_pages`
- `_document_evidence_briefs`
- `_collapse_whitespace`
- `_quote_search_pattern`
- `_line_range_for_span`
- `_anchor_exists`
- `_infer_anchor_from_match`
- `_stable_evidence_id`
- `_verify_evidence_output`
- `_draft_evidence_messages`
- `_draft_evidence`
- `_render_evidence_page`
- `_write_evidence_validation_report`
- `_remove_stale_evidence_pages`

### `compiler/pages.py`

Move markdown page I/O, typed page drafting, and backlink-related helpers:

- `_split_frontmatter`
- `_render_frontmatter`
- `_read_page`
- `_write_page`
- `_ensure_links_in_section`
- `_list_from_meta`
- `_append_unique`
- `_existing_page_briefs`
- `_strip_managed_sections`
- `_page_draft_messages`
- `_render_topic_page`
- `_render_regulation_page`
- `_render_procedure_page`
- `_render_conflict_page`
- `_draft_topic`
- `_draft_regulation`
- `_draft_procedure`
- `_draft_conflict`
- `_draft_topic_page`
- `_draft_regulation_page`
- `_draft_procedure_page`
- `_draft_conflict_page`
- `_upsert_typed_page`
- `_add_related_summary`
- `_tokenize_subject`
- `_extract_title`
- `_confirm_conflict`
- `_brief_for_index`
- `_apply_actions`

### `compiler/pipeline.py`

Keep only:

- callback type aliases
- `_emit_stage(...)`
- `_emit_counter(...)`
- `_emit_plan(...)`
- `_stage_usage_reporter(...)`
- `compile_documents(...)`
- `rebuild_index(...)`
- temporary compatibility wrappers and re-exports used by tests

## Test and Compatibility Constraints

The refactor should explicitly preserve current test patch paths where practical.

Important constraints:

- `tests/integration/test_milestone_a.py` imports
  `evidence_compiler.compiler.pipeline as compiler_pipeline`.
- The tests directly access several private helpers and private models.
- The tests patch `compiler_pipeline._structured_completion`,
  `compiler_pipeline._structured_acompletion`, `compiler_pipeline._summarize_document`,
  and several draft/planning helpers.

Compatibility strategy during refactor:

- Keep thin wrappers in `pipeline.py` for patch-sensitive helpers.
- Re-export moved models and helpers from `pipeline.py` until tests are updated.
- Avoid changing `compiler/__init__.py` in the modularization pass.
- Update tests only when a wrapper-based compatibility path is no longer useful.

## Execution Order

The work should happen in this order:

1. Refactor the compiler into cohesive modules without changing behavior.
2. Slim `pipeline.py` into an orchestration facade.
3. Harden the CLI for agent usage.
4. Freeze `brain-service` as a compatibility-only layer.
5. Retire desktop and web packages only after CLI parity is proven.

## Phase 1: Compiler Models Extraction

Files:

- `services/evidence-compiler/src/evidence_compiler/compiler/models.py`
- `services/evidence-compiler/src/evidence_compiler/compiler/pipeline.py`

Checklist:

- [ ] Create `compiler/models.py`.
- [ ] Move compile-local dataclasses, protocol types, and Pydantic models.
- [ ] Keep docstrings and `Field(description=...)` content intact.
- [ ] Re-export moved symbols through `pipeline.py` to avoid breaking tests.
- [ ] Keep `compiler/__init__.py` unchanged.

Definition of done:

- [ ] No compile behavior change.
- [ ] Existing imports from `evidence_compiler.compiler.pipeline` still work.
- [ ] Integration tests still pass.

## Phase 2: LLM and Summary Utility Extraction

Files:

- `services/evidence-compiler/src/evidence_compiler/compiler/llm.py`
- `services/evidence-compiler/src/evidence_compiler/compiler/summaries.py`
- `services/evidence-compiler/src/evidence_compiler/compiler/pipeline.py`

Checklist:

- [ ] Create `compiler/llm.py` and move LiteLLM structured-output helpers.
- [ ] Create `compiler/summaries.py` and move summary/materialization helpers.
- [ ] Keep `_structured_completion(...)` and `_structured_acompletion(...)`
      patch-compatible through thin wrappers or re-exports in `pipeline.py`.
- [ ] Keep `_summarize_document(...)` patch-compatible through a thin wrapper in
      `pipeline.py`.
- [ ] Keep summary markdown normalization behavior unchanged.
- [ ] Keep long-document PageIndex materialization behavior unchanged.

Definition of done:

- [ ] Summary generation still produces identical artifacts for unchanged tests.
- [ ] Long-document downstream prompts still use downstream source text, not only
      summary seed text.
- [ ] Structured-output retry and usage behavior remain unchanged.

## Phase 3: Planning, Evidence, and Page Slice Extraction

Files:

- `services/evidence-compiler/src/evidence_compiler/compiler/planning.py`
- `services/evidence-compiler/src/evidence_compiler/compiler/evidence.py`
- `services/evidence-compiler/src/evidence_compiler/compiler/pages.py`
- `services/evidence-compiler/src/evidence_compiler/compiler/pipeline.py`

Checklist:

- [ ] Create `compiler/planning.py` and move planning prompts, heuristics, and
      reconciliation logic.
- [ ] Create `compiler/evidence.py` and move evidence manifest, draft, verify,
      and render logic.
- [ ] Create `compiler/pages.py` and move page frontmatter I/O, typed page
      drafting, rendering, and backlink helpers.
- [ ] Keep `_plan_evidence(...)` patch-compatible through a wrapper in
      `pipeline.py`.
- [ ] Keep `_plan_taxonomy(...)` patch-compatible through a wrapper in
      `pipeline.py`.
- [ ] Keep `_draft_topic_page(...)`, `_draft_regulation_page(...)`,
      `_draft_procedure_page(...)`, and `_draft_conflict_page(...)`
      patch-compatible through wrappers in `pipeline.py`.
- [ ] Keep evidence verification behavior unchanged.
- [ ] Keep planner post-processing heuristics unchanged.
- [ ] Keep typed page rendering shape unchanged.

Definition of done:

- [ ] Evidence manifests and rendered evidence pages remain compatible.
- [ ] Planner prompts and plan filtering behavior remain compatible.
- [ ] Backlink sections remain compiler-managed and idempotent.

## Phase 4: Pipeline Facade Cleanup

Files:

- `services/evidence-compiler/src/evidence_compiler/compiler/pipeline.py`
- `services/evidence-compiler/src/evidence_compiler/compiler/__init__.py`
- `tests/integration/test_milestone_a.py`

Checklist:

- [ ] Reduce `pipeline.py` to stage orchestration plus compatibility wrappers.
- [ ] Keep `compile_documents(...)` as the top-level compile entrypoint.
- [ ] Keep `rebuild_index(...)` in the facade unless there is a clear gain in
      moving it.
- [ ] Remove dead local constants and imports from `pipeline.py` after each move.
- [ ] Review whether any temporary wrapper can be turned into a direct re-export.
- [ ] Only update tests when the compatibility wrappers make the file structure
      too misleading.

Definition of done:

- [ ] `pipeline.py` is substantially smaller and review-focused.
- [ ] The file primarily reads as orchestration rather than utility storage.
- [ ] Public compiler imports remain stable.

## Phase 5: CLI Hardening for Agent Usage

Files:

- `services/evidence-compiler/src/evidence_compiler/cli/__main__.py`
- `services/evidence-compiler/src/evidence_compiler/api.py`
- `services/evidence-compiler/src/evidence_compiler/config.py`
- `services/evidence-compiler/src/evidence_compiler/credentials.py`
- `services/evidence-compiler/src/evidence_compiler/state/registry.py`
- `services/shared/knowledge-models/src/knowledge_models/compiler_api.py`

Checklist:

- [ ] Add a global `--json` output mode for all commands.
- [ ] Return stable non-zero exit codes for command failures.
- [ ] Keep existing human-readable output for non-JSON usage.
- [ ] Add `jobs list`.
- [ ] Add `jobs get`.
- [ ] Add `jobs wait`.
- [ ] Add `config get`.
- [ ] Add `config set`.
- [ ] Add a read-only compile preview command such as `plan` or
      `rebuild --dry-run`.
- [ ] Add `credentials status`.
- [ ] Add `credentials delete`.
- [ ] Add a safer credential input path than `--api-key` on the command line,
      such as prompt, stdin, or environment-based input.
- [ ] Keep current commands available as compatibility aliases where practical.
- [ ] Ensure JSON output is shaped from existing typed models when possible.

Definition of done:

- [ ] An external agent can initialize a workspace, ingest files, inspect
      status, configure credentials, run compile, and inspect jobs without using
      `brain-service`.
- [ ] CLI failures are machine-detectable.
- [ ] CLI output is machine-readable without text scraping.

## Phase 6: Compatibility Shim and Retirement Path

Files:

- `services/brain-service/src/brain_service/main.py`
- `services/brain-service/src/brain_service/watch_manager.py`
- `apps/desktop/**`
- `packages/app/**`
- `packages/ui/**`
- `package.json`
- `pnpm-workspace.yaml`
- `turbo.json`

Checklist:

- [ ] Freeze `brain-service` as a compatibility-only HTTP shim.
- [ ] Do not add new business logic to `brain-service` unless needed only for
      temporary compatibility.
- [ ] Freeze desktop and web feature work.
- [ ] Keep existing HTTP routes stable during the transition.
- [ ] Move any reusable orchestration needed by both CLI and service into
      `evidence-compiler`, not into the service layer.
- [ ] Remove `apps/desktop`, `packages/app`, and `packages/ui` only after CLI
      parity and adoption are proven.
- [ ] Simplify or remove the JavaScript workspace tooling after UI retirement.

Definition of done:

- [ ] The project can be used end-to-end through the CLI without depending on
      the desktop or app shell.
- [ ] The remaining service layer is clearly transitional.

## Risks and Follow-Up Items

- [ ] Avoid mixing algorithmic compile changes into the refactor series.
- [ ] Avoid over-splitting the compiler into too many micro-modules.
- [ ] Watch for private test patch-path breakage during module extraction.
- [ ] Review workspace credential keying. The current keychain account naming is
      workspace-name-based, which may collide across different roots that share
      the same folder name.
- [ ] Decide whether watch mode stays out of scope for the first external agent
      skill release or needs a later CLI parity phase.

## Verification Checklist

- [ ] Run the compiler integration tests relevant to the refactor.
- [ ] Run `pytest tests/integration/test_milestone_a.py`.
- [ ] Run `npx basedpyright` after Python compiler or service changes.
- [ ] If any shared API JSON contract changes before UI retirement, run
      `pnpm --filter @evidence-brain/app typecheck` and
      `pnpm --filter @evidence-brain/desktop typecheck`.
- [ ] Manually validate the existing CLI commands still work before expanding the
      command surface.

## Final Completion Criteria

- [ ] `services/evidence-compiler` is the clear product center of gravity.
- [ ] `compiler/pipeline.py` is reduced to a reviewable orchestration file.
- [ ] The CLI is a reliable surface for external agent skills.
- [ ] `brain-service` is clearly transitional rather than strategic.
- [ ] Desktop and web packages are no longer required for the core workflow.
