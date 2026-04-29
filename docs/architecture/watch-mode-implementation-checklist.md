# Watch Mode Implementation Checklist

## Goal

Triển khai watch mode end-to-end cho desktop/app với phạm vi MVP rõ ràng:
- app cho phép chọn một hoặc nhiều source folder để watch
- `brain-service` owns watcher lifecycle và orchestration
- file mới được đưa vào watched folder sẽ auto-ingest sau debounce và stabilization check
- có thể bật `auto_compile` để queue compile sau khi ingest tạo ra document mới
- app hiển thị watch status, last activity, last error, và compile progress hiện có

## Locked Decisions For V1

- Watch orchestration nằm ở `services/brain-service`, không nằm ở Tauri host.
- `services/evidence-compiler` chỉ giữ low-level watcher utility và ingest/compile primitives.
- V1 dùng semantics `inbox-only`, không cố giải bài toán full sync.
- `workspace/raw` là `compiler-managed`, không phải user-managed watch root.
- App watch mode chỉ watch source folders bên ngoài workspace.
- Move hoặc rename file đã tồn tại bên trong `workspace/raw/**` không được trigger ingest hay re-compile.
- Chỉ queue compile khi sau một batch ingest thực sự có `added_documents` mới.
- Nếu compile đang chạy mà có batch mới, coalesce thành tối đa một follow-up compile.
- V1 không hỗ trợ delete reconciliation.
- V1 không cần persistence của watch session qua restart process.

## Scope

Phạm vi thay đổi:
- shared schema cho watch status/request
- low-level watcher lifecycle trong `evidence-compiler`
- watch manager trong `brain-service`
- watch API endpoints
- app UI cho start/stop watch, folder selection, polling status
- integration tests cho watch -> ingest -> compile flow

Ngoài phạm vi:
- watch trong Tauri/Rust host
- SSE/WebSocket
- full sync replace semantics cho file bị sửa nội dung nhiều lần
- rename/delete reconciliation đầy đủ
- watch `workspace/raw`, `workspace/wiki`, hoặc toàn workspace root
- persistence watch session qua restart của service

## Architecture Summary

### Ownership

- `apps/desktop`: chỉ cung cấp native folder picker cho app
- `packages/app`: watch controls, polling status, render UX
- `services/brain-service`: watch session manager, concurrency guard, API surface
- `services/evidence-compiler`: reusable watcher handle + existing `add_path(...)` và `run_compile_job(...)`

### Watch Roots

Watch root hợp lệ:
- folder ngoài workspace được người dùng chọn từ app

Watch root không hợp lệ:
- `workspace/raw`
- `workspace/wiki`
- `.brain`
- workspace root nói chung

### Compile Trigger Rule

`auto_compile` chỉ queue compile khi thỏa cả hai điều kiện:
- batch ingest có `added_documents.length > 0`
- không có compile job `queued/running` cho workspace đó

Nếu condition đầu đúng nhưng đang có compile chạy:
- đánh dấu workspace `dirty_after_compile = true`
- khi compile hiện tại kết thúc, queue đúng một compile tiếp theo

### Important Semantics

- Event filesystem tự nó không có nghĩa là cần compile.
- `add_path(...)` mới là nguồn sự thật để biết có ingest mới hay không.
- Move nội bộ trong `raw/` là ngoài watch scope của app mode, nên phải là no-op đối với watch orchestration.

## API Proposal

### `PUT /workspaces/{workspace_id}/watch`

Purpose:
- start hoặc update watch session cho một workspace

Suggested request body:

```json
{
  "paths": ["/absolute/source-folder"],
  "auto_compile": true,
  "debounce_seconds": 2.0
}
```

Validation rules:
- mọi path phải là absolute path
- mọi path phải tồn tại và là directory
- reject path nằm trong workspace root
- reject duplicate path sau khi resolve

### `GET /workspaces/{workspace_id}/watch`

Purpose:
- trả current watch status để app poll

Suggested response shape:

```json
{
  "workspace": "/abs/workspace",
  "enabled": true,
  "paths": ["/absolute/source-folder"],
  "auto_compile": true,
  "debounce_seconds": 2.0,
  "pending_paths": 0,
  "active_compile_job_id": "...",
  "last_ingest_job_id": "...",
  "last_compile_job_id": "...",
  "last_error": null,
  "updated_at": "2026-04-24T00:00:00Z"
}
```

### `DELETE /workspaces/{workspace_id}/watch`

Purpose:
- stop watcher cho workspace và clear in-memory session

## Suggested Shared Models

Files:
- `services/shared/knowledge-models/src/knowledge_models/compiler_api.py`
- `services/shared/knowledge-models/src/knowledge_models/__init__.py`
- `services/evidence-compiler/src/evidence_compiler/models.py`

Checklist:
- [ ] Thêm `WatchRequest`
- [ ] Thêm `WatchStatus`
- [ ] Thêm default values an toàn để UI parse dễ hơn
- [ ] Re-export các model mới từ `knowledge_models.__init__`
- [ ] Re-export các model mới từ `evidence_compiler.models`

Suggested fields:
- `WatchRequest`
  - `paths: list[str]`
  - `auto_compile: bool = True`
  - `debounce_seconds: float = 2.0`
- `WatchStatus`
  - `workspace: Path`
  - `enabled: bool`
  - `paths: list[Path] = []`
  - `auto_compile: bool = True`
  - `debounce_seconds: float = 2.0`
  - `pending_paths: int = 0`
  - `active_compile_job_id: str | None = None`
  - `last_ingest_job_id: str | None = None`
  - `last_compile_job_id: str | None = None`
  - `last_error: str | None = None`
  - `updated_at: str | None = None`

## Phase 1: Reusable Watcher Lifecycle

Files:
- `services/evidence-compiler/src/evidence_compiler/watcher.py`
- `services/evidence-compiler/src/evidence_compiler/cli/__main__.py`

Checklist:
- [ ] Refactor watcher để có lifecycle handle thay vì chỉ blocking loop
- [ ] Giữ CLI `watch` behavior hiện có bằng wrapper mỏng
- [ ] Thêm `on_moved` support cho move vào watched folder
- [ ] Giữ ignore cho hidden files
- [ ] Chưa xử lý `on_deleted` trong v1
- [ ] Thêm docstring rõ về recursive watch behavior

Implementation notes:
- Low-level API nên cho phép `start()`, `stop()`, và callback debounced paths.
- CLI có thể tiếp tục dùng helper blocking để không đổi UX developer flow.

Definition of done:
- [ ] `brain-service` có thể start và stop watcher mà không block request thread
- [ ] CLI watch cũ vẫn hoạt động

## Phase 2: Brain-Service Watch Manager

Files:
- `services/brain-service/src/brain_service/main.py`
- có thể thêm file mới như `services/brain-service/src/brain_service/watch_manager.py`

Checklist:
- [ ] Tạo watch manager in-memory theo workspace
- [ ] Mỗi workspace chỉ có tối đa một active watch session
- [ ] Lưu config `paths`, `auto_compile`, `debounce_seconds`
- [ ] Lưu runtime state `pending_paths`, `last_error`, `updated_at`
- [ ] Lưu `last_ingest_job_id` và `last_compile_job_id`
- [ ] Lưu cờ `dirty_after_compile`
- [ ] Khi stop watch, cleanup observer và session state

Concurrency checklist:
- [ ] Thêm workspace-level runtime lock cho ingest/watch/compile orchestration
- [ ] Tránh gọi `add_path(...)` song song với callback watch khác cho cùng workspace
- [ ] Tránh race giữa watcher ingest và compile completion logic
- [ ] Không queue compile trùng nếu đã có compile `queued/running`

Rationale:
- `HashRegistry` và `JobStore` hiện là JSON file-backed, chưa có locking nội bộ.
- Watch mode sẽ làm tăng xác suất overlap giữa ingest và compile nếu không serialize theo workspace.

Definition of done:
- [ ] Có thể start/update/stop watch session cho workspace từ service runtime
- [ ] Batch event dồn đúng vào một flow ingest serialized

## Phase 3: File Stabilization And Ingest Rules

Files:
- `services/brain-service/src/brain_service/watch_manager.py`
- có thể reuse helper nhỏ trong cùng file

Checklist:
- [ ] Trước khi ingest, chờ file ổn định thay vì ingest ngay khi có event
- [ ] Kiểm tra file còn tồn tại và là file thường
- [ ] Bỏ qua hidden file
- [ ] Bỏ qua file không thuộc supported extensions
- [ ] Bỏ qua path nằm trong workspace root
- [ ] Nếu `add_path(...)` không tạo `added_documents`, không queue compile
- [ ] Nếu ingest fail cho một file, ghi lỗi nhưng không làm hỏng toàn session

Recommended stabilization rule:
- đợi ít nhất 2 lần check liên tiếp có cùng `size` và `mtime`
- mỗi lần cách nhau `200-500ms`

Definition of done:
- [ ] file lớn đang copy vào watched folder không bị ingest quá sớm
- [ ] event modify lặp lại không tự động kéo theo compile nếu không có doc mới

## Phase 4: Auto-Compile Coalescing

Files:
- `services/brain-service/src/brain_service/main.py`
- `services/brain-service/src/brain_service/watch_manager.py`

Checklist:
- [ ] Reuse guard compile hiện có để phát hiện job `queued/running`
- [ ] Nếu không có active compile và batch ingest có doc mới, queue compile ngay
- [ ] Nếu đang có active compile, set `dirty_after_compile = true`
- [ ] Khi compile hiện tại hoàn tất, nếu dirty thì queue đúng một compile follow-up
- [ ] Clear `dirty_after_compile` sau khi follow-up compile được queue
- [ ] Cập nhật `active_compile_job_id` trong watch status

Important rule:
- [ ] Move/rename file bên trong `workspace/raw/**` không tạo đường đi nào tới auto-compile vì `raw/` không phải watch root hợp lệ

Definition of done:
- [ ] nhiều file mới rơi vào watched folder trong lúc compile chạy chỉ sinh tối đa 2 compile jobs liên tiếp

## Phase 5: Brain-Service API Endpoints

Files:
- `services/brain-service/src/brain_service/main.py`

Checklist:
- [ ] Thêm `PUT /workspaces/{workspace_id}/watch`
- [ ] Thêm `GET /workspaces/{workspace_id}/watch`
- [ ] Thêm `DELETE /workspaces/{workspace_id}/watch`
- [ ] Validate path và reject watch root nằm trong workspace
- [ ] Trả lỗi `400` với message rõ ràng nếu path không hợp lệ
- [ ] Trả `404` khi workspace không tồn tại hoặc chưa init
- [ ] Giữ docstring API cập nhật theo convention trong repo

Suggested error codes:
- `invalid_watch_path`
- `watch_path_inside_workspace`
- `watch_already_running`

Definition of done:
- [ ] app có thể điều khiển watch session hoàn toàn qua HTTP API hiện có

## Phase 6: Frontend Watch Controls And Polling

Files:
- `packages/app/src/pages/app-shell.tsx`
- `apps/desktop/src/main.tsx`

Checklist:
- [ ] Thêm types nội bộ cho `WatchRequest` và `WatchStatus`
- [ ] Thêm state cho watch status, watch poll error, và local form state
- [ ] Thêm nút `Watch Folder`
- [ ] Reuse `pickImportPaths("folder")` để chọn watch folder
- [ ] Cho phép add nhiều folder bằng cách chọn lặp lại từng folder
- [ ] Thêm toggle `Auto-compile`
- [ ] Thêm nút `Stop Watching`
- [ ] Poll `GET /watch` theo `setTimeout`, không dùng `setInterval`
- [ ] Khi `last_compile_job_id` đổi, reuse `loadJob(...)` để hiện compile progress panel sẵn có
- [ ] Refresh `loadOverview()` và `loadDocuments()` khi watch status đổi có ý nghĩa

UX text guidance:
- dùng wording `Watch Source Folder` hoặc `Watch Inbox Folder`
- không dùng wording khiến user hiểu rằng app đang watch `raw/`

Definition of done:
- [ ] user có thể bật watch từ desktop app mà không cần CLI
- [ ] compile progress hiện tiếp tục reuse panel sẵn có

## Phase 7: Guardrails Around `raw/`

Files:
- `services/brain-service/src/brain_service/main.py`
- `services/evidence-compiler/src/evidence_compiler/compiler/pipeline.py`

Checklist:
- [ ] Reject watch path nằm trong `workspace/raw`
- [ ] Reject watch path nằm trong `workspace/wiki`
- [ ] Cân nhắc fail sớm với lỗi rõ ràng nếu compile gặp `document.raw_path` không còn tồn tại
- [ ] Không thêm logic tự động repair bằng re-ingest trong v1

Rationale:
- `raw/` là compiler-managed artifact area.
- Nếu user tự move file trong `raw/`, đó là thao tác ngoài supported flow.

Definition of done:
- [ ] move file nội bộ trong `raw/` không trigger watch activity
- [ ] compile failure do raw artifact missing, nếu xảy ra, có lỗi đủ rõ để chẩn đoán

## Phase 8: Tests And Verification

Files:
- `tests/integration/test_milestone_a.py`
- có thể thêm test file mới nếu cần tách scope

Backend integration checklist:
- [ ] Start watch cho workspace với external folder hợp lệ
- [ ] Tạo file mới trong watched folder -> ingest xảy ra
- [ ] Với `auto_compile=true` và credentials hợp lệ -> compile được queue
- [ ] Khi compile đang chạy và có file mới nữa -> chỉ có một follow-up compile
- [ ] Watch path nằm trong workspace -> API reject
- [ ] Stop watch -> event mới không còn ingest nữa

Watcher behavior checklist:
- [ ] debounce gom nhiều modify/create event cho cùng file
- [ ] hidden files bị ignore
- [ ] move vào watched folder được xử lý như file mới
- [ ] move nội bộ trong `raw/` không thuộc app watch scope

Quality gate checklist:
- [ ] chạy Python integration tests liên quan `brain-service` và `evidence-compiler`
- [ ] chạy `npx basedpyright` sau khi đổi Python service/compiler code
- [ ] chạy `pnpm --filter @evidence-brain/app typecheck` nếu đổi app
- [ ] chạy `pnpm --filter @evidence-brain/desktop typecheck` nếu đổi desktop shell props/imports

## Recommended Implementation Order

- [ ] Phase 1: shared models
- [ ] Phase 2: reusable watcher lifecycle
- [ ] Phase 3: watch manager + workspace lock
- [ ] Phase 4: auto-compile coalescing
- [ ] Phase 5: API endpoints
- [ ] Phase 6: frontend controls + polling
- [ ] Phase 7: raw guardrails
- [ ] Phase 8: tests + typecheck

## Acceptance Criteria

- [ ] Desktop app có thể bật watch mode mà không dùng CLI
- [ ] Watch root chỉ là external source folders, không phải `workspace/raw`
- [ ] File mới vào watched folder sẽ auto-ingest
- [ ] `auto_compile` chỉ chạy khi có document mới thực sự được thêm
- [ ] Compile không bị spam khi nhiều file đến liên tiếp
- [ ] Move nội bộ trong `workspace/raw/**` không trigger re-compile
- [ ] API/UI hiển thị được watch status và compile progress liên quan
- [ ] Có integration coverage cho watch happy path và invalid-path guard
