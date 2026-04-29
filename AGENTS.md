# Agent Conventions

This file stores persistent implementation conventions for future coding sessions.

## 1) API Docstring Convention (Required)

- Keep docstrings on public APIs in:
  - `services/evidence-compiler/src/evidence_compiler/api.py`
  - `services/brain-service/src/brain_service/main.py`
- Do not remove docstrings during refactors unless the function is deleted.
- For public functions/endpoints, include these sections when applicable:
  - short purpose line
  - `Args`
  - `Returns`
  - `Raises`
- Keep behavior text current; do not leave stale milestone references.
- For helpers, at minimum keep a one-line docstring.
- For non-obvious helper functions anywhere in the repo, add a short comment or
  docstring that explains purpose, important assumptions, or why the helper
  exists when that materially improves reviewability.
- Keep helper comments concise; do not add line-by-line commentary for obvious
  code.

## 2) Shared Schema Contract

- Shared API contracts live in:
  - `services/shared/knowledge-models/src/knowledge_models/compiler_api.py`
- `brain-service` and `evidence-compiler` should import boundary models from
  `knowledge_models.compiler_api`.
- `evidence_compiler.models` is a compatibility re-export layer; prefer shared imports
  for new files.

## 3) Credential and Provider Policy

- Workspace credentials are stored per workspace in OS keychain.
- Credential bundle contains: `provider`, `model`, `api_key`.
- M2 official platform support: Windows + macOS.
- Default provider catalog:
  - `openai`
  - `anthropic`
  - `gemini`
  - `xai`
  - `vercel_ai_gateway`

## 4) Compile Pipeline Expectations (M2)

- Compile generates:
  - `summaries/`
  - `topics/`
  - `regulations/`
  - `procedures/`
  - `conflicts/`
  - `evidence/`
- Milestone 2 compile is taxonomy-native end-to-end; do not reintroduce
  `wiki/concepts/` as either a public artifact or an internal planning layer.
- Rebuild `wiki/index.md` after compile.
- Write structural lint report to `wiki/reports/`.
- Track compile job stage/progress/error.

## 4a) LiteLLM Structured Output Policy

- Prefer LiteLLM structured outputs for compiler LLM calls:
  `response_format`, `json_schema`, or Pydantic response models.
- Do not default to free-form JSON parsing plus repair helpers when the selected
  provider/model supports structured outputs.
- For new compile-time prompts, define explicit typed output models first, then
  write prompts against those contracts.
- Preferred pattern for typed outputs is:
  - `resp = litellm.completion(..., response_format=MyPydanticModel)`
  - `result = MyPydanticModel.model_validate_json(resp.choices[0].message.content)`
- Keep a small helper for safely extracting message content from LiteLLM responses
  to satisfy static typing (for example, when the response may be typed as
  `CustomStreamWrapper` by type checkers).
- DeepAgent may support QA, lint, or review workflows around compile, but it is
  not the primary Milestone 2 compile artifact writer.

## 5) Quality Gate Before Finishing

- Run Python integration tests relevant to changed API/compiler paths.
- Run Python typecheck with `npx basedpyright` when Python service/compiler code changes.
- Run TypeScript typecheck for app/desktop when API JSON contracts change.
- If API response shapes change, ensure UI model types are updated in
  `packages/app/src/pages/app-shell.tsx`.

## 6) Good Additions for Future Sessions

- Keep a short changelog section in this file for non-obvious architecture decisions.
- Record known provider/model caveats (for example, long-doc behavior differences).
- Record any temporary compatibility layers and planned removal criteria.
- For compiler and pipeline refactors, prefer fewer, slightly larger local code
  blocks over many single-use helper functions when that makes review easier.

## 7) Session Changelog

- `2026-04-17`: `POST /jobs/compile` in `brain-service` now queues compile jobs and
  runs `run_compile_job(...)` in a background thread so `/jobs/{job_id}` can be polled
  for realtime status transitions (`queued -> running -> completed/failed`).
- `2026-04-22`: Milestone 2 compile decisions are locked to taxonomy-native wiki
  outputs (`topics/regulations/procedures/conflicts/evidence`), no `concepts/`
  compatibility layer, and LiteLLM structured outputs should be preferred for
  compiler steps.
- `2026-04-22`: LiteLLM structured output usage is standardized to Pydantic
  `response_format` + `model_validate_json(...)`, with typed content-extraction
  helpers to avoid basedpyright attribute-access issues on response unions.
- `2026-04-23`: Drafting-stage concurrency should stay scoped to post-plan page
  drafting only, and pipeline reviewability should win over splitting logic into
  many tiny single-use helpers.
- `2026-04-24`: Quality gate now includes `npx basedpyright` for Python-side
  changes so compile/service refactors get a static typecheck before finishing.
- `2026-04-29`: Non-obvious helper functions across the repo should carry short,
  review-oriented comments or docstrings when that materially improves
  readability, while still avoiding commentary on obvious code.
