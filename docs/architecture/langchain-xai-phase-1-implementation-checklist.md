# LangChain xAI Phase 1 Implementation Checklist

## Goal

Chuyển lớp structured-output của `services/evidence-compiler` từ LiteLLM sang
`LangChain agent` cho các compile job dùng model `xai/*`, nhằm giảm lỗi kiểu:

- JSON bị cắt giữa chừng nhưng `finish_reason=stop`
- `Pydantic validation` fail vì output text không hoàn chỉnh
- schema drift ở các bước summary, taxonomy planning, và page drafting

Phase 1 phải giữ business logic compile pipeline gần như nguyên vẹn, chỉ thay lớp
gọi model và schema guidance.

## Locked Decisions

- Phase 1 chỉ hỗ trợ direct `xai/*` models.
- Không xử lý `vercel_ai_gateway/*`, `openai`, `anthropic`, hay `gemini` trong
  phase này.
- Dùng `langchain.agents.create_agent(...)` trong `evidence-compiler`, không dùng
  `deepagents` package trong compiler.
- Dùng LangChain structured output với `ProviderStrategy` mặc định cho `xai/*`.
- Chỉ dùng `ToolStrategy` như fallback có chủ đích theo từng stage nếu cần.
- Không tách `reasoning` và `non-reasoning` model ở phase 1.
- `summary` và page drafting không đặt hard `max_tokens` cap.
- Không refactor compile pipeline thành deep agent workflow lớn.
- Giữ deterministic fallbacks hiện có ở outer pipeline.

## Why LangChain Agent

`LangChain agent` phù hợp cho phase này vì:

- khớp với design gốc của repo, nơi `LangChain DeepAgent` là runtime direction
- `xAI` được LangChain support structured output native qua `ChatXAI`
- `create_agent(..., response_format=...)` nhận trực tiếp `Pydantic` schema
- current pipeline đã có `messages` theo dạng role/content rất gần format mà agent
  nhận qua `invoke({"messages": ...})`
- `Field(description=...)` và class docstring sẽ đi thẳng vào schema guidance

## DeepAgent Boundary

Thiết kế này vẫn tôn trọng boundary hiện có:

- `brain-service` mới là nơi dùng `DeepAgent` cho runtime reasoning
- `evidence-compiler` chỉ dùng LangChain agent như structured-output adapter mỏng
- compiler không trở thành multi-step tool-using agent loop

Nói cách khác, phase 1 dùng `LangChain agent`, nhưng không biến compiler thành
primary `DeepAgent` executor.

## Scope

Trong scope:

- backend structured-output mới cho `xai/*`
- mapping credential `provider/model/api_key` sang `LangChain ChatXAI`
- migrate sync structured calls cho summary, evidence planning, taxonomy planning,
  conflict confirmation
- migrate async structured calls cho evidence draft và taxonomy page drafting
- bổ sung `Field(description=...)` cho các output models dùng làm structured schema
- update tests để patch adapter/backend mới thay vì patch trực tiếp LiteLLM

Ngoài scope:

- migrate các provider khác
- đưa `deepagents` vào `evidence-compiler`
- refactor compile pipeline thành full agent workflow
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

- `services/evidence-compiler/src/evidence_compiler/compiler/langchain_agent_backend.py`
  nếu muốn tách riêng implementation khỏi generic backend façade

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

Add a small backend module dedicated to LangChain xAI structured calls.

Suggested API:

- `complete(...) -> ModelT`
- `acomplete(...) -> ModelT`
- `supports_langchain_xai(model: str) -> bool`
- `build_chat_model(...) -> ChatXAI`
- `build_agent(...)`
- `build_response_format(...)`
- `usage_to_token_summary(...) -> TokenUsageSummary`

Intent:

- keep `pipeline.py` orchestration stable
- keep current wrapper signatures as close as possible
- isolate all LangChain-specific code to one place

### 2. Model And Response Strategy

Default strategy for `xai/*`:

```python
agent = create_agent(
    model=chat_model,
    tools=[],
    response_format=ProviderStrategy(MySchema, strict=True),
)
```

Notes:

- `ChatXAI` supports structured output according to LangChain integration docs
- `ProviderStrategy` should be the primary path for xAI because native provider
  enforcement is the most reliable option when it works
- if a specific stage still behaves poorly under provider-native mode, phase 1 may
  allow a targeted fallback to `ToolStrategy(MySchema, handle_errors=...)`

### 3. Message Handling

Current pipeline prompt builders already produce role-tagged messages:

- `system`
- `assistant`
- `user`

Phase 1 should first try to pass these messages through directly:

```python
agent.invoke({"messages": messages})
```

If needed, add only a tiny normalizer in the backend to coerce message format, but
do not rewrite all prompt builders.

### 4. Structured Response Extraction

LangChain returns typed structured output in:

- `result["structured_response"]`

Phase 1 should treat that as the only structured payload source for the new xAI
path.

Do not use:

- manual JSON parsing
- `json_repair`
- custom truncation heuristics as the primary happy path

### 5. Backend Selection

Phase 1 backend routing rule:

- if `model.startswith("xai/")`: use LangChain xAI backend
- otherwise: keep current LiteLLM behavior unchanged

This keeps the migration safe and incremental.

## Schema Guidance Requirement

LangChain structured output works best when `Pydantic` models carry clear semantic
descriptions. Phase 1 should therefore add meaningful `Field(description=...)` to
every field on every `BaseModel` used as a structured output schema or nested
structured type.

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
  be considered during drafting
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
- topic page drafting
- regulation page drafting
- procedure page drafting
- conflict page drafting

Keep hard caps for now on:

- evidence planning
- taxonomy planning
- evidence quote drafting

Rationale:

- user explicitly wants no hard limit on summary and page drafting
- planning outputs are bounded structured lists, so keeping caps there is still a
  reasonable cost-control choice for phase 1
- if taxonomy planning still shows instability after LangChain migration, phase 2
  can remove `_TAXONOMY_PLAN_MAX_TOKENS` or move that stage to `ToolStrategy`

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
- manual response repair as the default retry mechanism

For `xai/*`, let LangChain structured output own provider-native validation, and
only use `ToolStrategy(handle_errors=...)` when a specific stage needs explicit
schema retry messaging.

## Phase 1: Dependency And Backend Scaffold

Files:

- `services/evidence-compiler/pyproject.toml`
- `services/evidence-compiler/src/evidence_compiler/compiler/structured_backend.py`

Checklist:

- [ ] Add `langchain` dependency to `evidence-compiler`
- [ ] Add `langchain-core` dependency to `evidence-compiler`
- [ ] Add `langchain-xai` dependency to `evidence-compiler`
- [ ] Do not add `deepagents` to `evidence-compiler`
- [ ] Add backend module for LangChain xAI structured calls
- [ ] Define typed sync API `complete(...)`
- [ ] Define typed async API `acomplete(...)`
- [ ] Add helper to detect supported `xai/*` models
- [ ] Add helper to normalize `xai/<model>` into `ChatXAI(model=<name>)`
- [ ] Add helper to build `ProviderStrategy` by default
- [ ] Add optional helper to build `ToolStrategy` for per-stage fallback
- [ ] Map LangChain usage into `TokenUsageSummary`
- [ ] Keep backend interface narrow and reusable from current wrappers

Definition of done:

- [ ] xAI backend can execute one typed sync request
- [ ] xAI backend can execute one typed async request
- [ ] usage callback can still receive a `TokenUsageSummary`

## Phase 2: Output Schema Hardening

Files:

- `services/evidence-compiler/src/evidence_compiler/compiler/pipeline.py`

Checklist:

- [ ] Add class docstring for each structured output model used by LangChain
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

- [ ] every structured schema field has a meaningful description
- [ ] nested structured models also have descriptions where the agent sees them

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
- [ ] Ensure summary path uses LangChain backend for `xai/*`
- [ ] Keep `_normalize_summary_markdown(...)` post-processing unchanged
- [ ] Keep summary prompt structure stable

Definition of done:

- [ ] summary compile path no longer depends on JSON text completion length
- [ ] summary still returns `SummaryStageResult`

## Phase 5: Taxonomy Planning Migration

Files:

- `services/evidence-compiler/src/evidence_compiler/compiler/pipeline.py`

Checklist:

- [ ] Migrate `_plan_taxonomy(...)` xAI path onto LangChain backend
- [ ] Keep `_TAXONOMY_PLAN_MAX_TOKENS` for initial rollout
- [ ] Start with `ProviderStrategy` for `TaxonomyPlanResult`
- [ ] If provider-native mode remains unstable, allow targeted fallback to `ToolStrategy`
- [ ] Preserve `_finalize_taxonomy_plan(...)` behavior unchanged
- [ ] Preserve anti-overcreation tests and post-processing invariants

Definition of done:

- [ ] `TaxonomyPlanResult` no longer depends on free-form JSON text parsing
- [ ] current planner post-processing tests still pass

## Phase 6: Drafting Migration

Files:

- `services/evidence-compiler/src/evidence_compiler/compiler/pipeline.py`

Checklist:

- [ ] Route `_draft_topic_page(...)` xAI path to LangChain backend
- [ ] Route `_draft_regulation_page(...)` xAI path to LangChain backend
- [ ] Route `_draft_procedure_page(...)` xAI path to LangChain backend
- [ ] Route `_draft_conflict_page(...)` xAI path to LangChain backend
- [ ] Keep these calls uncapped for `max_tokens`
- [ ] Preserve deterministic fallback behavior on exception

Definition of done:

- [ ] page drafting stays uncapped
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
- [ ] Add test: `xai/*` routes through LangChain backend
- [ ] Add test: non-`xai/*` still routes through LiteLLM path in phase 1
- [ ] Add test: summary path does not pass `max_tokens`
- [ ] Add test: page draft paths do not pass `max_tokens`
- [ ] Add test: taxonomy planning still uses bounded cap in phase 1
- [ ] Add test: key schemas expose field descriptions in `model_json_schema()`
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
- runtime model object: `ChatXAI(model="grok-4-1-fast-reasoning")`

### Usage Mapping

Map LangChain usage to existing `TokenUsageSummary` fields using `usage_metadata`
or provider token metadata when available:

- `input_tokens -> prompt_tokens`
- `output_tokens -> completion_tokens`
- `total_tokens -> total_tokens`
- request count -> `calls`

### Error Handling

Preferred behavior for `xai/*` path:

- provider-native schema mismatch: raise cleanly and let current outer fallback/job layer handle it
- stage-specific retry needs: use `ToolStrategy(handle_errors=...)` instead of JSON repair
- avoid adding new truncation heuristics unless a real gap remains after migration

## Definition Of Done

- [ ] direct `xai/*` compile path uses LangChain agent for structured output
- [ ] summary path no longer uses hard `max_tokens`
- [ ] page drafting remains uncapped
- [ ] all structured schemas expose useful `Field(description=...)`
- [ ] existing deterministic fallbacks still behave the same
- [ ] integration tests and `basedpyright` pass
