# Evidence Brain Final Implementation Plan

## 1. Product Scope

- Product name: `Evidence Brain` (working name)
- Primary users in MVP: clinical doctors and pharmacists in hospitals, clinics, and pharmacies
- MVP focus: compliance and administrative support
- Data policy for MVP: no patient-level data
- Geography and policy coverage: Vietnam + international references + institution internal documents
- Retrieval strategy: no vector-based RAG
- Core strategy: LLM-Wiki compilation + PageIndex tree indexing for long and complex documents

## 2. Core Architecture Decisions

- Desktop application only in phase 1 (`apps/web` is deferred)
- Frontend stack: React + Tailwind CSS + shadcn/ui
- Desktop shell: Tauri
- Runtime orchestration: LangChain DeepAgent (Python)
- Compile-time extraction and rewriting: LiteLLM
- Long-document engine: PageIndex
- OpenKB usage mode: donor code only (selective port), not product core
- Monorepo structure: `services/` + `services/shared/`

## 3. Repository Structure

```text
apps/
  desktop/

packages/
  ui/
  app/

services/
  brain-service/
  evidence-compiler/
  shared/
    pageindex-adapter/
    knowledge-models/

third_party/
  openkb-src/
  pageindex-src/

docs/
tests/
var/
```

## 4. Module Responsibilities

- `apps/desktop`: Tauri host process, native shell behavior, app lifecycle
- `packages/ui`: reusable UI primitives and design system wrapper
- `packages/app`: shared React app logic and feature modules
- `services/brain-service`: runtime API, DeepAgent orchestration, sessions, jobs, audit
- `services/evidence-compiler`: ingestion, conversion, compilation, structural lint, watch/rebuild workflows
- `services/shared/pageindex-adapter`: stable adapter around PageIndex APIs
- `services/shared/knowledge-models`: shared Pydantic schemas across service and compiler
- `third_party/openkb-src` and `third_party/pageindex-src`: reference-only source trees

## 5. Why Compiler Is Library-First (Not CLI-Only)

- The compiler will be implemented as a Python package that exports a direct API.
- CLI remains a thin wrapper for developer workflows (`init`, `add`, `watch`, `lint`, `status`, `rebuild`).
- `brain-service` imports compiler modules directly and runs compilation in background jobs.
- This avoids subprocess-only coupling, improves typed integration, and simplifies progress/error handling.

## 6. Why Compiler Is Not a Separate HTTP Service in MVP

- `brain-service` and `evidence-compiler` are both local Python modules in the same product runtime.
- A separate API adds protocol overhead, process management complexity, and more failure modes.
- MVP requirement is local-first and simple operations; direct import + background jobs is the lowest-risk design.
- The architecture still supports future extraction into a standalone worker service if needed.

## 7. Workspace Runtime Layout

```text
workspace/
  raw/
  wiki/
    sources/
    summaries/
    topics/
    regulations/
    procedures/
    conflicts/
    evidence/
    reports/
    index.md
    log.md
    AGENTS.md
  .brain/
    config.yaml
    hashes.json
    jobs/
```

## 8. Domain Expansion in Wiki (Compliance-Centric)

This is locked in for the final plan:

- `topics/`: central operational topic pages
- `regulations/`: binding requirements and applicability pages
- `procedures/`: execution workflow pages for departments and roles
- `conflicts/`: explicit conflict records between sources and policies
- `evidence/`: exact evidence blocks (quote, anchor, source references)

Rationale:

- Generic `summary + concept` structure is insufficient for compliance/admin workflows.
- Users need answer traceability, authority context, and explicit conflict visibility.
- This taxonomy allows auditable answers and operationally useful outputs.

## 9. Data Storage Strategy

SQLite (minimum scope):

- workspaces
- sessions
- chat history
- jobs
- document registry
- audit events

Filesystem (source-of-truth artifacts):

- raw documents
- converted sources
- PageIndex trees and page content
- compiled wiki markdown

## 10. OpenKB and PageIndex Integration Plan

### OpenKB donor modules to port

- converter
- config
- state/hash registry
- watcher
- structural lint
- compiler pipeline core

### OpenKB modules to exclude

- query
- chat
- OpenAI Agents SDK runtime wrappers

### PageIndex integration

- Use a dedicated `pageindex-adapter` package to isolate API drift
- Support long-document tree generation at compile time
- Support targeted tree/page retrieval at query time

## 11. DeepAgent Integration Plan

DeepAgent is used in `brain-service` for runtime query and reasoning.

### Installation

```bash
uv add deepagents fastapi uvicorn pydantic-settings aiosqlite
uv add "langchain[openai]"
```

Optional provider packages:

```bash
uv add "langchain[anthropic]"
uv add "langchain[google-genai]"
```

Optional checkpointing support:

```bash
uv add langgraph-checkpoint-sqlite
```

### Initial runtime integration

- Build a `create_deep_agent(...)` in `brain-service/agents`
- Register tools that read wiki pages, list sources, fetch evidence blocks, and call PageIndex adapter
- Keep compiler operations out of runtime tool loop; compiler stays background-job based

## 12. Tooling Boundaries

- LiteLLM is used by `evidence-compiler` for deterministic compile-time transformations.
- DeepAgent is used by `brain-service` for interactive runtime reasoning and multi-step tool usage.
- This split optimizes reliability, observability, and cost for the MVP.

## 13. API Surface (MVP)

- `GET /health`
- `GET /workspaces`
- `POST /workspaces`
- `POST /jobs/compile`
- `GET /jobs/{job_id}`
- `POST /chat`
- `GET /wiki/pages`
- `GET /documents`
- `GET /audit`

## 14. MVP Milestones

### Milestone 1: Skeleton + Ingestion

- Monorepo scaffold complete
- Desktop app bootstrapped
- Brain service health endpoint available
- Compiler package and CLI wrapper available
- Basic workspace/job scaffolding in place

### Milestone 2: Compilation Pipeline

- Port OpenKB donor pieces into compiler
- Integrate PageIndex via adapter
- Generate `summaries/topics/regulations/procedures/conflicts/evidence`
- Structural lint and status/list commands working

### Milestone 3: Runtime Agent

- DeepAgent integrated into brain-service
- Query over compiled wiki and source anchors
- Context drill-down via PageIndex
- Answer formatting for compliance/admin support

### Milestone 4: Quality and Governance

- Audit event trail
- Conflict review loop
- Semantic lint sub-agent (DeepAgent-based)

## 15. Explicit Non-Goals for MVP

- Patient-level reasoning
- EMR integration
- Multi-user collaboration
- Web app deployment
- Vector database integration
