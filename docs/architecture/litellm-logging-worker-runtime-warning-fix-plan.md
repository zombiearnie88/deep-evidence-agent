# LiteLLM Logging Worker Runtime Warning Fix Plan

## Goal

Remove the runtime warning below without changing compile behavior:

```text
RuntimeWarning: coroutine 'Logging.async_success_handler' was never awaited
```

The fix should preserve current compiler outputs while making the async compile
path compatible with LiteLLM's background logging worker.

## Symptom

The warning appears after CLI or library compile runs that execute multiple
LiteLLM async calls during one process lifetime.

Observed warning source:

- `.venv/lib/python3.13/site-packages/litellm/litellm_core_utils/logging_worker.py:75`

Observed local async call sites that can trigger it:

- `services/evidence-compiler/src/evidence_compiler/compiler/pipeline.py`
- `services/evidence-compiler/src/evidence_compiler/compiler/pages.py`

## Root Cause Summary

LiteLLM creates async success-handler coroutines for background logging and binds
its internal logging worker queue to the current running event loop.

Our compiler currently calls `asyncio.run(...)` in multiple places during a
single compile flow:

- one-off evidence drafting calls in `pipeline.py`
- page drafting batches in `pages.py`

Each `asyncio.run(...)` creates and closes a separate event loop. When LiteLLM
sees the loop change, it resets its worker queue state. That can orphan queued
logging coroutines from the previous loop before they are awaited, which then
surfaces as:

```text
RuntimeWarning: coroutine 'Logging.async_success_handler' was never awaited
```

## Non-Goals

- Do not suppress the warning with filters.
- Do not disable LiteLLM logging/callback machinery globally as the primary fix.
- Do not change compile semantics or page outputs as part of this patch.
- Do not replace async drafting with sync completions unless needed as a short
  emergency rollback.

## Fix Strategy

Use one event loop for the full async drafting portion of a compile instead of
creating many short-lived loops.

Target state:

- `compile_documents(...)` remains sync at the public API boundary.
- Internally, compile creates one async orchestration block for all async draft
  work.
- All LiteLLM `acompletion(...)` calls for a compile run on that single loop.
- No helper under the compile path calls `asyncio.run(...)` repeatedly.

## Implementation Plan

### 1. Introduce One Async Draft Orchestrator

In `services/evidence-compiler/src/evidence_compiler/compiler/pipeline.py`:

- Add a private async helper for compile drafting stages, for example:
  `_run_async_compile_stages(...)`
- Keep `compile_documents(...)` sync, but have it call `asyncio.run(...)` once
  around that helper.
- Move these phases into the shared loop:
  - evidence drafting
  - typed page drafting

Definition of done:

- `compile_documents(...)` contains at most one top-level `asyncio.run(...)`
  call for compile drafting.

### 2. Make Page Action Application Async-Friendly

In `services/evidence-compiler/src/evidence_compiler/compiler/pages.py`:

- Convert `_apply_actions(...)` into an async function, or add a dedicated
  `_apply_actions_async(...)` variant.
- Replace the local `asyncio.run(_draft_batch())` usage with direct `await`
  inside that async function.
- Preserve concurrency behavior using the existing semaphore-based batch draft
  logic.

Definition of done:

- `pages.py` no longer creates its own event loop during compile drafting.

### 3. Keep Patch Compatibility Stable

In `services/evidence-compiler/src/evidence_compiler/compiler/pipeline.py`:

- Preserve the existing wrapper functions for patch-sensitive test helpers.
- Keep `_draft_evidence(...)`, `_draft_topic_page(...)`,
  `_draft_regulation_page(...)`, `_draft_procedure_page(...)`, and
  `_draft_conflict_page(...)` patchable from `pipeline.py`.
- If helper signatures change internally, keep compatibility at the facade.

Definition of done:

- Existing integration tests that patch `compiler_pipeline` async draft helpers
  continue to work.

### 4. Verify LiteLLM Warning Is Gone

Run compile flows with runtime warnings promoted to errors:

```bash
PYTHONWARNINGS=error::RuntimeWarning uv run evidence-compiler rebuild --workspace <workspace>
```

Also test the read-only preview path:

```bash
PYTHONWARNINGS=error::RuntimeWarning uv run evidence-compiler plan --workspace <workspace>
```

Definition of done:

- No `RuntimeWarning` is emitted for `Logging.async_success_handler` during
  compile or plan flows.

## Suggested Edit Scope

Expected primary files:

- `services/evidence-compiler/src/evidence_compiler/compiler/pipeline.py`
- `services/evidence-compiler/src/evidence_compiler/compiler/pages.py`

Possible secondary files:

- `services/evidence-compiler/src/evidence_compiler/compiler/evidence.py`
- `tests/integration/test_milestone_a.py`

## Risks

- Moving drafting into one shared loop can accidentally change execution order.
- Async refactor may alter when stage/counter callbacks fire.
- Page drafting concurrency may change if batch orchestration is rewritten too
  aggressively.
- Test patch points can break if wrappers are removed instead of reused.

## Review Rules

- Make the smallest change that removes repeated event-loop creation.
- Do not combine this fix with prompt, planner, or rendering behavior changes.
- Keep public compiler API sync and unchanged.
- Prefer one local async orchestration block over introducing many new helper
  layers.

## Verification Checklist

- [ ] Remove repeated `asyncio.run(...)` calls from compile drafting path.
- [ ] Keep compile outputs stable for unchanged fixtures.
- [ ] Run `uv run --with pytest python -m pytest tests/integration/test_milestone_a.py -q`.
- [ ] Run `npx basedpyright`.
- [ ] Run at least one real compile or plan command with
      `PYTHONWARNINGS=error::RuntimeWarning`.

## Completion Criteria

- [ ] Compiler uses one event loop for a compile's async drafting work.
- [ ] LiteLLM logging worker warning no longer appears.
- [ ] Existing compile behavior and patch-sensitive tests remain stable.
