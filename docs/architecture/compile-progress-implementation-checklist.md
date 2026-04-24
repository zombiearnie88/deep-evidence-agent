# Compile Progress Implementation Checklist

## Goal

Hiển thị compile progress thực tế trên frontend theo từng stage của `evidence-compiler`, bao gồm:
- trạng thái job `queued/running/completed/failed`
- progress tổng và progress trong từng stage
- taxonomy plan summary ngay sau bước `planning-taxonomy`
- token usage theo stage và tổng usage khi provider trả về
- UX polling/resume tốt hơn cho compile job đang chạy

## Scope

Phạm vi thay đổi:
- shared schema giữa `brain-service`, `evidence-compiler`, và frontend
- compile job persistence
- compiler pipeline telemetry
- brain-service API behavior cho compile job
- frontend polling và progress UI
- integration tests và typecheck

Ngoài phạm vi:
- SSE/WebSocket
- cost estimation khi provider không trả usage
- redesign lớn của desktop shell UI
- thêm raw plan viewer đầy đủ dưới dạng JSON

## Proposed Output File

Suggested path:
`docs/architecture/compile-progress-implementation-checklist.md`

## Phase 1: Shared Schema Contract

Files:
`services/shared/knowledge-models/src/knowledge_models/compiler_api.py`
`services/shared/knowledge-models/src/knowledge_models/__init__.py`
`services/evidence-compiler/src/evidence_compiler/models.py`

Checklist:
- [ ] Thêm model `TokenUsageSummary`
- [ ] Thêm model `StageCounter`
- [ ] Thêm model `CompilePlanItem`
- [ ] Thêm model `CompilePlanBucket`
- [ ] Thêm model `CompilePlanDocument`
- [ ] Thêm model `CompilePlanSummary`
- [ ] Thêm model `CompileProgressDetails`
- [ ] Mở rộng `JobRecord` với field `compile: CompileProgressDetails | None = None`
- [ ] Giữ default values an toàn để parse được job JSON cũ
- [ ] Re-export các model mới từ `knowledge_models.__init__`
- [ ] Re-export các model mới từ `evidence_compiler.models`

Target shape:
- `TokenUsageSummary`
  - `prompt_tokens`
  - `completion_tokens`
  - `total_tokens`
  - `calls`
  - `available`
- `StageCounter`
  - `completed`
  - `total`
  - `unit`
  - `item_label`
- `CompilePlanBucket`
  - `create_count`
  - `update_count`
  - `related_count`
  - `create`
  - `update`
  - `related`
- `CompilePlanDocument`
  - `document_name`
  - `topics`
  - `regulations`
  - `procedures`
  - `conflicts`
  - `evidence_count`
- `CompileProgressDetails`
  - `counters`
  - `plan`
  - `usage_total`
  - `usage_by_stage`

Definition of done:
- [ ] `JobRecord.model_validate(...)` vẫn parse được record cũ không có field `compile`
- [ ] frontend có thể tạo type nội bộ bám đúng contract mới

## Phase 2: Job Store And Runtime Tracker

Files:
`services/evidence-compiler/src/evidence_compiler/state/registry.py`
`services/evidence-compiler/src/evidence_compiler/api.py`

Checklist:
- [ ] Mở rộng `JobStore.update(...)` để nhận thêm `compile`
- [ ] Giữ hành vi hiện tại cho `status/stage/progress/message/error/payload`
- [ ] Tạo tracker nhỏ trong `run_compile_job(...)` để giữ compile snapshot trong memory
- [ ] Đổi map progress từ `stage -> float` sang `stage -> (start, end)` để nội suy progress trong stage
- [ ] Thêm helper trong tracker để cập nhật:
- [ ] `set_stage(stage, message)`
- [ ] `set_counter(stage, completed, total, unit, item_label=None)`
- [ ] `set_plan(plan_summary)`
- [ ] `add_usage(stage, usage_delta)`
- [ ] `flush()`
- [ ] `complete()`
- [ ] `fail(error)`
- [ ] Khi `job_id is None`, create job như cũ rồi gắn tracker
- [ ] Khi reuse `job_id`, hydrate tracker từ record hiện tại nếu cần
- [ ] Chỉ flush khi state đổi có ý nghĩa để tránh ghi file JSON quá dày

Progress interpolation rules:
- `preparing`: fixed small range
- `indexing-long-docs`: interpolate theo documents materialized
- `summarizing`: interpolate theo summaries completed
- `planning-taxonomy`: interpolate theo documents planned
- `writing-topics`: interpolate theo pages drafted/written
- `writing-regulations`: interpolate theo pages drafted/written
- `writing-procedures`: interpolate theo pages drafted/written
- `writing-conflicts`: interpolate theo planner conflict pages và pair checks
- `writing-evidence`: interpolate theo evidence pages written
- `backlinking`: interpolate theo touched pages patched
- `updating-index`: fixed
- `linting`: fixed

Definition of done:
- [ ] `GET /jobs/{job_id}` trả về `compile` snapshot trong khi job đang chạy
- [ ] `progress` không còn chỉ nhảy cục giữa các stage dài

## Phase 3: Pipeline Telemetry Hooks

Files:
`services/evidence-compiler/src/evidence_compiler/compiler/pipeline.py`

Checklist:
- [ ] Thêm telemetry callback hoặc telemetry sink cho:
- [ ] stage update
- [ ] counter update
- [ ] plan summary update
- [ ] token usage update
- [ ] Thread telemetry từ `run_compile_job(...)` xuống `compile_documents(...)`
- [ ] Không đổi stage public names hiện có
- [ ] Emit counter cho `indexing-long-docs`
- [ ] Emit counter cho `summarizing`
- [ ] Emit counter cho `planning-taxonomy`
- [ ] Emit counter cho `writing-topics`
- [ ] Emit counter cho `writing-regulations`
- [ ] Emit counter cho `writing-procedures`
- [ ] Emit counter cho `writing-conflicts`
- [ ] Emit counter cho `writing-evidence`
- [ ] Emit progress-friendly message updates trong `writing-conflicts` khi chuyển từ planner conflicts sang pair detection

Implementation notes:
- Sau vòng `summarizing`, nên biết tổng documents và số đã completed
- Sau vòng `planning-taxonomy`, build plan summary rồi flush ngay để frontend render sớm
- Với `writing-topics/regulations/procedures/conflicts`, nên tính `total` từ action count thực tế trước khi chạy batch
- Với `writing-evidence`, `total` là số evidence pages sau khi merge theo claim key
- Với `backlinking`, có thể dùng số summary/derived pages cần patch để nội suy nếu muốn progress mượt hơn

Definition of done:
- [ ] taxonomy plan xuất hiện trên job record ngay sau `planning-taxonomy`
- [ ] stage message và counter phản ánh đúng phase đang chạy

## Phase 4: Taxonomy Plan Summary

Files:
`services/evidence-compiler/src/evidence_compiler/compiler/pipeline.py`
`services/evidence-compiler/src/evidence_compiler/api.py`

Checklist:
- [ ] Tạo helper chuyển `plans_by_hash` thành `CompilePlanSummary`
- [ ] Tính tổng `create/update/related` cho `topics`
- [ ] Tính tổng `create/update/related` cho `regulations`
- [ ] Tính tổng `create/update/related` cho `procedures`
- [ ] Tính tổng `create/update/related` cho `conflicts`
- [ ] Tính tổng `evidence_count`
- [ ] Tạo preview per-document với `document_name`
- [ ] Lưu danh sách item preview cho `create/update`
- [ ] Cap số item preview trên mỗi bucket để job file không phình quá lớn
- [ ] Giữ counts đầy đủ kể cả khi preview bị cap

Recommended caps:
- tối đa `10` items preview cho mỗi bucket trên mỗi document

Definition of done:
- [ ] frontend có thể render được summary dạng `8 topics, 4 regulations, 3 procedures, 2 conflicts, 19 evidence`
- [ ] frontend có thể mở rộng chi tiết theo document mà không cần parse raw plan

## Phase 5: Token Usage Collection

Files:
`services/evidence-compiler/src/evidence_compiler/compiler/pipeline.py`

Checklist:
- [ ] Tạo helper `_extract_usage(response)`
- [ ] Hỗ trợ response object-style và dict-style nếu LiteLLM/provider trả khác nhau
- [ ] Tạo helper cộng dồn usage vào `TokenUsageSummary`
- [ ] Cập nhật `_structured_completion(...)` để optionally report usage
- [ ] Cập nhật `_structured_acompletion(...)` để optionally report usage
- [ ] Truyền stage tương ứng vào từng call site

Stage mapping:
- `_summarize_document(...)` -> `summarizing`
- `_plan_taxonomy(...)` -> `planning-taxonomy`
- `_draft_topic_page(...)` -> `writing-topics`
- `_draft_regulation_page(...)` -> `writing-regulations`
- `_draft_procedure_page(...)` -> `writing-procedures`
- `_draft_conflict_page(...)` -> `writing-conflicts`
- `_confirm_conflict(...)` -> `writing-conflicts`

Failure and retry behavior:
- [ ] Nếu structured-output request fail rồi fallback free-form request chạy tiếp, cộng usage cho cả hai lần gọi
- [ ] Nếu provider không trả usage, đánh dấu `available=false`
- [ ] Không biến missing usage thành `0` giả

Definition of done:
- [ ] `usage_total` tăng dần trong lúc compile chạy
- [ ] `usage_by_stage` có số liệu cho các stage có LLM calls
- [ ] case provider không có usage vẫn an toàn

## Phase 6: Brain-Service API Behavior

Files:
`services/brain-service/src/brain_service/main.py`

Checklist:
- [ ] Giữ `GET /jobs/{job_id}` trả enriched `JobRecord`
- [ ] Cập nhật docstring nếu response shape thay đổi rõ rệt
- [ ] Thêm guard chống queue compile trùng cho cùng workspace
- [ ] Nếu đã có compile job `queued/running`, trả `409`
- [ ] Trả structured detail có `code=compile_already_running`
- [ ] Trả kèm `job_id` đang chạy để frontend có thể resume polling
- [ ] Giữ behavior hiện tại cho missing credentials và workspace invalid

Definition of done:
- [ ] double-click vào `Queue Compile` không tạo nhiều job chạy song song
- [ ] frontend có thể reuse `job_id` hiện có từ response `409`

## Phase 7: Frontend Polling State

Files:
`packages/app/src/pages/app-shell.tsx`

Checklist:
- [ ] Thêm type `JobRecord`
- [ ] Thêm type `CompileProgressDetails`
- [ ] Thêm type `StageCounter`
- [ ] Thêm type `CompilePlanSummary`
- [ ] Thêm type `TokenUsageSummary`
- [ ] Thêm state `activeCompileJobId`
- [ ] Thêm state `activeCompileJob`
- [ ] Thêm state `compilePollError`
- [ ] Tách compile-running state khỏi `busy`
- [ ] Sau `POST /jobs/compile`, đổi message từ `compile done` sang `compile queued`
- [ ] Lưu `job_id` vào state
- [ ] Nếu API trả `409 compile_already_running`, lấy `job_id` và resume polling thay vì báo lỗi cứng
- [ ] Tạo `loadJob(jobId, workspaceRef)` helper
- [ ] Tạo polling `useEffect` dùng `setTimeout`, không dùng `setInterval`
- [ ] Poll mỗi `750ms-1000ms`
- [ ] Dừng poll khi job `completed` hoặc `failed`
- [ ] Khi job kết thúc, refresh `loadOverview()` và `loadDocuments(...)`
- [ ] Không refresh toàn bộ overview ở mỗi tick polling
- [ ] Nếu poll lỗi tạm thời, giữ snapshot cũ và retry

Definition of done:
- [ ] compile job đang chạy vẫn theo dõi được sau khi queue
- [ ] UI không hiện `compile done` khi compile mới chỉ vừa enqueue

## Phase 8: Frontend Progress UI

Files:
`packages/app/src/pages/app-shell.tsx`

Checklist:
- [ ] Thêm `Compile Progress` card vào UI
- [ ] Hiển thị status badge `queued/running/completed/failed`
- [ ] Hiển thị progress bar tổng dựa trên `job.progress`
- [ ] Hiển thị `stage` và `message`
- [ ] Hiển thị `completed/total` của stage hiện tại nếu có counter
- [ ] Hiển thị taxonomy plan summary khi `job.compile.plan` đã có
- [ ] Hiển thị token usage tổng
- [ ] Hiển thị token usage theo stage
- [ ] Hiển thị error khi job failed
- [ ] Disable nút `Queue Compile` nếu workspace đang có compile active
- [ ] Cân nhắc disable ingest khi compile đang chạy cùng workspace
- [ ] Dùng `<details>` cho phần chi tiết per-document để card không quá dài

Recommended UI blocks:
- trạng thái tổng
- current stage
- current stage counter
- planned outputs
- token usage total
- token usage by stage
- failure details

Definition of done:
- [ ] user nhìn vào một card là hiểu job đang ở đâu
- [ ] taxonomy plan hiện lên ngay sau bước planning
- [ ] token usage nhìn được theo stage và tổng

## Phase 9: Tests

Files:
`tests/integration/test_milestone_a.py`

Checklist:
- [ ] Cập nhật `_fake_llm_completion(...)` để trả usage mock
- [ ] Cập nhật `_fake_llm_acompletion(...)` nếu cần
- [ ] Assert job polling cuối cùng trả `status=completed`
- [ ] Assert `job["compile"]` tồn tại
- [ ] Assert `job["compile"]["plan"]` tồn tại
- [ ] Assert `job["compile"]["usage_total"]["total_tokens"] > 0`
- [ ] Assert có ít nhất một stage usage trong `usage_by_stage`
- [ ] Thêm case provider không trả usage để confirm `available=false`
- [ ] Thêm test duplicate compile guard nếu implement `409 compile_already_running`

Definition of done:
- [ ] integration tests cover được compile telemetry path
- [ ] test không phụ thuộc vào LiteLLM thật

## Phase 10: Verification

Commands:
- Python integration tests liên quan compile/job flow
- Typecheck frontend app

Checklist:
- [ ] Chạy integration tests cho `brain-service` và `evidence-compiler` paths đã đổi
- [ ] Chạy `pnpm typecheck`
- [ ] Manual smoke test với 1 workspace có vài documents
- [ ] Confirm stage chuyển qua đủ các bước expected
- [ ] Confirm taxonomy plan hiện sau `planning-taxonomy`
- [ ] Confirm token total bằng tổng `usage_by_stage`
- [ ] Confirm failed job vẫn render error đúng

## Acceptance Criteria

- [ ] Frontend theo dõi được compile job từ lúc `queued` đến `completed/failed`
- [ ] Progress bar di chuyển trong các stage dài, không chỉ nhảy theo milestone lớn
- [ ] User thấy taxonomy plan summary ngay sau bước plan
- [ ] User thấy token usage tổng và theo stage khi provider hỗ trợ
- [ ] Nếu provider không trả usage, UI hiển thị `N/A` hoặc equivalent rõ ràng
- [ ] Job contract mới không làm hỏng việc đọc job record cũ
- [ ] Queue compile trùng không tạo nhiều background job cùng workspace

## Risks And Decisions

Open decisions:
- chọn cap bao nhiêu item preview cho mỗi document bucket
- có disable ingest trong lúc compile hay chỉ disable nút compile
- có thêm endpoint list latest compile job cho workspace ngay trong lần này không

Known risks:
- job JSON có thể to ra nếu lưu plan preview quá chi tiết
- token usage availability phụ thuộc provider/model
- `writing-conflicts` gồm nhiều loại công việc nên counter/message phải rõ để tránh khó hiểu

Recommended defaults:
- dùng `docs/architecture` cho tài liệu này
- dùng polling trước, chưa làm SSE/WebSocket
- chỉ hiển thị usage thật từ provider, không estimate
- preview taxonomy per-document bằng counts + vài titles/slugs, không render raw plan full

## Suggested Implementation Order

- [ ] Shared schema
- [ ] JobStore update contract
- [ ] Compile tracker in `api.py`
- [ ] Pipeline telemetry hooks
- [ ] Taxonomy plan summary
- [ ] Token usage extraction
- [ ] Brain-service duplicate job guard
- [ ] Frontend polling
- [ ] Frontend progress card
- [ ] Integration tests
- [ ] Typecheck and manual smoke

## Notes For Execution

- Giữ thay đổi nhỏ và cục bộ, tránh tách thêm nhiều helper một lần nếu không cần
- Không đổi stage names public đã có
- Không nhồi telemetry mới vào `payload` nếu đã có field typed `compile`
- Không hiển thị token `0` cho case provider không trả usage
- Không gọi compile là `done` ngay sau khi enqueue
