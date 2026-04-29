# PydanticAI xAI Phase 1 Implementation Checklist

## Goal

Chuyển lớp structured-output của `services/evidence-compiler` từ LiteLLM sang
`PydanticAI Agent` cho các compile job dùng model `xai/*`, nhằm giảm lỗi kiểu:

- JSON bị cắt giữa chừng nhưng `finish_reason=stop`
- `Pydantic validation` fail vì output text không hoàn chỉnh
- schema drift ở các bước summary, taxonomy planning, và page drafting

Phase 1 phải giữ business logic compile pipeline gần như nguyên vẹn, chỉ thay lớp
gọi model và schema guidance.

## Locked Decisions

- Phase 1 chỉ hỗ trợ direct `xai/*` models.
- Không xử lý `vercel_ai_gateway/*`, `openai`, `anthropic`, hay `gemini` trong phase này.
- Dùng `PydanticAI Agent`, không dùng direct API cho structured output path.
- Dùng default `Tool Output`, không dùng `NativeOutput` trong phase 1.
- Không tách `reasoning` và `non-reasoning` model ở phase 1.
- `summary` và taxonomy page drafting không đặt hard `max_tokens` cap.
- Không refactor compile pipeline thành agent workflow lớn.
- Giữ deterministic fallbacks hiện có ở outer pipeline.

## Why Agent

`PydanticAI Agent` phù hợp hơn direct API trong phase này vì:

- repo đã có nhiều `BaseModel` output types rõ ràng
- `Agent(..., output_type=MyModel)` sẽ dùng tool-output mặc định, ổn định hơn JSON
  text/native structured response trên Grok
- `Field(description=...)` trên từng field giúp model hiểu semantic contract tốt hơn
- `PydanticAI` có built-in validation retry thay vì phải tự viết fallback
  `response_format -> plain JSON -> repair`

## Scope

Trong scope:

- backend structured-output mới cho `xai/*`
- mapping credential `provider/model/api_key` sang `PydanticAI` xAI model
- migrate sync structured calls cho summary, evidence planning, taxonomy planning,
  conflict confirmation
- migrate async structured calls cho evidence draft và taxonomy page drafting
- bổ sung `Field(description=...)` cho các output models dùng làm `output_type`
- update tests để patch adapter/backend mới thay vì patch trực tiếp LiteLLM

Ngoài scope:

- migrate các provider khác
- `FallbackModel` nhiều provider
- đổi kiến trúc prompt builders sang framework mới hoàn toàn
- redesign compile telemetry hoặc frontend UX
- phân tách model theo stage

## Files Involved

Primary files:

- `services/evidence-compiler/pyproject.toml`
- `services/evidence-compiler/src/evidence_compiler/compiler/pipeline.py`
- `services/evidence-compiler/src/evidence_compiler/compiler/structured_backend.py`
- `services/evidence-compiler/src/evidence_compiler/credentials.py`
- `tests/integration/test_milestone_a.py`

Possible helper-only additions:

- `services/evidence-compiler/src/evidence_compiler/compiler/pydanticai_prompts.py`
  nếu cần tách conversion helper khỏi `pipeline.py`

## Target Call Sites

Structured sync calls currently routed through `_structured_completion(...)`:

- `_summarize_document(...)`
- `_plan_evidence(...)`
- `_plan_taxonomy(...)`
- `_confirm_conflict(...)`

Structured async calls currently routed through `_structured_acompletion(...)`:

- `_draft_evidence(...)`
- `_draft_topic_page(...)`
- `_draft_regulation_page(...)`
- `_draft_procedure_page(...)`
- `_draft_conflict_page(...)`

## Architecture

### 1. New Backend Adapter

Add a small backend module dedicated to `PydanticAI` xAI calls.

Suggested API:

- `complete(...) -> ModelT`
- `acomplete(...) -> ModelT`
- `supports_pydanticai_xai(model: str) -> bool`
- `build_xai_agent(...) -> Agent`
- `usage_to_token_summary(...) -> TokenUsageSummary`

Intent:

- keep `pipeline.py` orchestration stable
- keep current wrapper signatures as close as possible
- isolate all `PydanticAI`-specific code to one place

### 2. Prompt Conversion Strategy

Current pipeline prompt builders already produce role-tagged messages:

- `system`
- `assistant`
- `user`

Phase 1 should keep those builders and add a small conversion helper that maps them
to `PydanticAI Agent` input.

Suggested behavior:

- join `system` messages into `instructions`
- turn `assistant` content blocks into labeled context sections inside the final
  user prompt
- preserve the final `user` instruction as the last section of the user prompt

Suggested helper shape:

- `messages_to_agent_inputs(messages) -> tuple[str, str]`

Where:

- return value 1: `instructions`
- return value 2: final user prompt string

This keeps prompt semantics close to the existing pipeline without rewriting all
prompt builders.

### 3. Output Mode

Use default `Tool Output` via:

```python
agent = Agent(model, output_type=MyPydanticModel)
```

Do not use:

- `NativeOutput(...)`
- manual JSON parsing
- `json_repair`

### 4. Backend Selection

Phase 1 backend routing rule:

- if `model.startswith("xai/")`: use `PydanticAI`
- otherwise: keep current LiteLLM behavior unchanged

This keeps the migration safe and incremental.

## Schema Guidance Requirement

`PydanticAI Agent` can use `Field(description=...)` to produce better tool-output
arguments. Phase 1 should therefore add meaningful descriptions to every field on
every `BaseModel` used as an `output_type` or nested structured output type.

### Description Rules

- Description should explain semantic meaning, not Python type.
- Prefer short imperative guidance that tells the model what belongs in the field.
- Avoid vague descriptions like `"The title"` or `"List of strings"`.
- Mention formatting constraints when they matter.
- Mention when a field should be empty, omitted, concise, or claim-centric.

Good examples:

- `document_brief`: concise one-sentence summary of the source document, suitable
  for summary frontmatter or planner context
- `summary_markdown`: multiline markdown body for the public summary page; start
  sections at `##`, no YAML frontmatter, no top-level `#`
- `candidate_evidence_ids`: evidence ids that directly support this page and should
  be cited in drafting
- `used_evidence_ids`: evidence ids actually used in the drafted page output

### Models That Need Field Descriptions

At minimum, add `Field(description=...)` to these models in `pipeline.py`:

- `SummaryStageResult`
- `PagePlanItem`
- `PagePlanActions`
- `EvidencePlanItem`
- `EvidencePlanActions`
- `EvidenceDraftQuote`
- `EvidenceDraftOutput`
- `TaxonomyPlanResult`
- `TopicPageOutput`
- `RegulationPageOutput`
- `ProcedurePageOutput`
- `ConflictPageOutput`
- `ConflictCheckResult`

Strong recommendation:

- also add concise class docstrings to these models so tool/output descriptions are
  readable when exposed to the model

## Token Policy For Phase 1

Remove hard `max_tokens` from:

- summary generation
- taxonomy page drafting

Keep hard caps for now on:

- evidence planning
- taxonomy planning
- evidence quote drafting

Rationale:

- user explicitly wants no hard limit on summary and page drafting
- planning outputs are bounded structured lists, so keeping caps there is still a
  reasonable cost-control choice for phase 1
- if taxonomy planning still shows truncation after tool-output migration, phase 2
  can remove `_TAXONOMY_PLAN_MAX_TOKENS`

## Deterministic Fallback Policy

Preserve current outer fallbacks exactly where they already exist:

- `_draft_evidence(...)` fallback to empty quote set
- `_draft_topic_page(...)` fallback to local deterministic draft
- `_draft_regulation_page(...)` fallback to local deterministic draft
- `_draft_procedure_page(...)` fallback to local deterministic draft
- `_draft_conflict_page(...)` fallback to local deterministic draft

Do not carry over these LiteLLM-era behaviors into the new xAI path:

- plain JSON fallback after structured failure
- `json_repair`
- custom truncation heuristics as the main repair mechanism

For `xai/*`, let `PydanticAI` own the structured validation and retry behavior.

## Phase 1: Dependency And Backend Scaffold

Files:

- `services/evidence-compiler/pyproject.toml`
- `services/evidence-compiler/src/evidence_compiler/compiler/structured_backend.py`

Checklist:

- [ ] Add `pydantic-ai-slim[xai]` dependency
- [ ] Add backend module for `PydanticAI` xAI structured calls
- [ ] Define typed sync API `complete(...)`
- [ ] Define typed async API `acomplete(...)`
- [ ] Add helper to detect supported `xai/*` models
- [ ] Add helper to normalize `xai/<model>` into `XaiModel(<model>)`
- [ ] Map `PydanticAI` usage into `TokenUsageSummary`
- [ ] Keep backend interface narrow and reusable from current wrappers

Definition of done:

- [ ] xAI backend can execute one typed sync request
- [ ] xAI backend can execute one typed async request
- [ ] usage callback can still receive a `TokenUsageSummary`

## Phase 2: Output Schema Hardening

Files:

- `services/evidence-compiler/src/evidence_compiler/compiler/pipeline.py`

Checklist:

- [ ] Add class docstring for each output model used by `PydanticAI`
- [ ] Add `Field(description=...)` for `SummaryStageResult.document_brief`
- [ ] Add `Field(description=...)` for `SummaryStageResult.summary_markdown`
- [ ] Add `Field(description=...)` for all `PagePlanItem` fields
- [ ] Add `Field(description=...)` for all `PagePlanActions` fields
- [ ] Add `Field(description=...)` for all `EvidencePlanItem` fields
- [ ] Add `Field(description=...)` for all `EvidencePlanActions` fields
- [ ] Add `Field(description=...)` for all `EvidenceDraftQuote` fields
- [ ] Add `Field(description=...)` for all `EvidenceDraftOutput` fields
- [ ] Add `Field(description=...)` for `TaxonomyPlanResult` page-type buckets
- [ ] Add `Field(description=...)` for all taxonomy page output models
- [ ] Add `Field(description=...)` for `ConflictCheckResult` fields

Definition of done:

- [ ] every `output_type` model field has a meaningful description
- [ ] nested structured models also have descriptions where the model sees them

## Phase 3: Wrapper Integration

Files:

- `services/evidence-compiler/src/evidence_compiler/compiler/pipeline.py`

Checklist:

- [ ] Update `_structured_completion(...)` to route `xai/*` through new backend
- [ ] Update `_structured_acompletion(...)` to route `xai/*` through new backend
- [ ] Keep non-`xai/*` path on existing LiteLLM implementation in phase 1
- [ ] Remove xAI-specific dependency on LiteLLM `response_format` behavior
- [ ] Stop using `json_repair` for the `xai/*` path
- [ ] Keep current usage callback contract unchanged

Definition of done:

- [ ] existing call sites do not need major signature changes
- [ ] xAI structured calls no longer parse response text manually

## Phase 4: Summary Migration

Files:

- `services/evidence-compiler/src/evidence_compiler/compiler/pipeline.py`

Checklist:

- [ ] Remove hard `max_tokens` from `_summarize_document(...)`
- [ ] Remove `_SUMMARY_MAX_TOKENS` if no longer used
- [ ] Ensure summary path uses `PydanticAI` for `xai/*`
- [ ] Keep `_normalize_summary_markdown(...)` post-processing unchanged
- [ ] Keep summary prompt structure stable, only adjust wording if needed for tool output

Definition of done:

- [ ] summary compile path no longer depends on JSON text completion length
- [ ] summary still returns `SummaryStageResult`

## Phase 5: Taxonomy Planning Migration

Files:

- `services/evidence-compiler/src/evidence_compiler/compiler/pipeline.py`

Checklist:

- [ ] Migrate `_plan_taxonomy(...)` xAI path onto `PydanticAI`
- [ ] Keep `_TAXONOMY_PLAN_MAX_TOKENS` for initial rollout
- [ ] Preserve `_finalize_taxonomy_plan(...)` behavior unchanged
- [ ] Preserve anti-overcreation tests and post-processing invariants

Definition of done:

- [ ] `TaxonomyPlanResult` no longer depends on free-form JSON text parsing
- [ ] current planner post-processing tests still pass

## Phase 6: Drafting Migration

Files:

- `services/evidence-compiler/src/evidence_compiler/compiler/pipeline.py`

Checklist:

- [ ] Route `_draft_topic_page(...)` xAI path to `PydanticAI`
- [ ] Route `_draft_regulation_page(...)` xAI path to `PydanticAI`
- [ ] Route `_draft_procedure_page(...)` xAI path to `PydanticAI`
- [ ] Route `_draft_conflict_page(...)` xAI path to `PydanticAI`
- [ ] Keep these calls uncapped for `max_tokens`
- [ ] Preserve deterministic fallback behavior on exception

Definition of done:

- [ ] taxonomy page drafting stays uncapped
- [ ] existing deterministic fallback behavior still works

## Phase 7: Remaining Structured xAI Calls

Files:

- `services/evidence-compiler/src/evidence_compiler/compiler/pipeline.py`
- `services/evidence-compiler/src/evidence_compiler/credentials.py`

Checklist:

- [ ] Migrate `_plan_evidence(...)` xAI path
- [ ] Migrate `_draft_evidence(...)` xAI path
- [ ] Migrate `_confirm_conflict(...)` xAI path
- [ ] Update `validate_credentials(...)` to validate xAI credentials through the same backend family
- [ ] Preserve current evidence and conflict fallbacks

Definition of done:

- [ ] xAI compile path no longer depends on LiteLLM for structured output
- [ ] credential validation and compile runtime use the same provider stack

## Phase 8: Tests And Verification

Files:

- `tests/integration/test_milestone_a.py`
- other targeted tests if needed

Checklist:

- [ ] Update tests that patch `litellm.completion(...)` to patch backend adapter instead
- [ ] Add test: `xai/*` routes through `PydanticAI` backend
- [ ] Add test: non-`xai/*` still routes through LiteLLM path in phase 1
- [ ] Add test: summary path does not pass `max_tokens`
- [ ] Add test: page draft paths do not pass `max_tokens`
- [ ] Add test: taxonomy planning still uses bounded cap in phase 1
- [ ] Add test: field descriptions exist on key output schemas when inspected via `model_json_schema()`
- [ ] Keep existing planner/fallback tests green

Verification commands:

- `uv run python -m unittest tests.integration.test_milestone_a`
- `npx basedpyright`

Optional targeted verification:

- [ ] run one real xAI-backed compile on a short-doc workspace
- [ ] confirm summary, taxonomy planning, and page drafting complete without LiteLLM structured parsing

## Implementation Notes

### Credential Mapping

Phase 1 only needs direct xAI mapping:

- stored model: `xai/grok-4-1-fast-reasoning`
- runtime model object: `XaiModel("grok-4-1-fast-reasoning")`

### Usage Mapping

Map `PydanticAI` usage to existing `TokenUsageSummary` fields:

- `input_tokens -> prompt_tokens`
- `output_tokens -> completion_tokens`
- `total -> total_tokens`
- request count -> `calls`

### Error Handling

Preferred behavior for `xai/*` path:

- schema mismatch: let `PydanticAI` retry/self-correct
- final failure: raise exception to existing outer fallback or job failure path
- avoid adding new JSON truncation heuristics unless a real gap remains after migration

## Definition Of Done

- [ ] direct `xai/*` compile path uses `PydanticAI Agent` for structured output
- [ ] summary path no longer uses hard `max_tokens`
- [ ] taxonomy page drafting remains uncapped
- [ ] all `output_type` models expose useful `Field(description=...)`
- [ ] existing deterministic fallbacks still behave the same
- [ ] integration tests and `basedpyright` pass
