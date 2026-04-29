"""Shared compiler and service API contract schemas."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field


class WorkspaceInitResult(BaseModel):
    workspace: Path
    created: bool


class DocumentRecord(BaseModel):
    doc_id: str
    name: str
    file_hash: str
    file_type: str
    raw_path: Path
    source_path: Path | None
    is_long_doc: bool
    requires_pageindex: bool
    page_count: int | None
    status: str
    created_at: str
    pageindex_artifact_path: Path | None = None


class AddResult(BaseModel):
    workspace: Path
    discovered_files: int
    added_documents: list[DocumentRecord] = Field(default_factory=list)
    skipped_files: list[Path] = Field(default_factory=list)
    unsupported_files: list[Path] = Field(default_factory=list)
    job_id: str | None = None


class WatchRequest(BaseModel):
    auto_compile: bool = True
    debounce_seconds: float = Field(default=2.0, gt=0.0)


class WatchStatus(BaseModel):
    workspace: Path
    enabled: bool = False
    paths: list[Path] = Field(default_factory=list)
    auto_compile: bool = True
    debounce_seconds: float = 2.0
    pending_paths: int = 0
    active_compile_job_id: str | None = None
    last_ingest_job_id: str | None = None
    last_compile_job_id: str | None = None
    last_error: str | None = None
    updated_at: str | None = None


class WatchBacklogItem(BaseModel):
    path: Path
    name: str
    size_bytes: int
    modified_at: str


class WatchBacklogResponse(BaseModel):
    workspace: Path
    root: Path
    items: list[WatchBacklogItem] = Field(default_factory=list)
    total: int = 0


class WatchBacklogIngestRequest(BaseModel):
    paths: list[str] = Field(default_factory=list)


class TokenUsageSummary(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0
    available: bool = False


class StageCounter(BaseModel):
    completed: int = 0
    total: int = 0
    unit: str = "items"
    item_label: str | None = None


class CompilePlanItem(BaseModel):
    slug: str
    title: str
    brief: str = ""


class CompilePlanBucket(BaseModel):
    create_count: int = 0
    update_count: int = 0
    related_count: int = 0
    create: list[CompilePlanItem] = Field(default_factory=list)
    update: list[CompilePlanItem] = Field(default_factory=list)
    related: list[str] = Field(default_factory=list)


class CompilePlanDocument(BaseModel):
    document_name: str
    topics: CompilePlanBucket = Field(default_factory=CompilePlanBucket)
    regulations: CompilePlanBucket = Field(default_factory=CompilePlanBucket)
    procedures: CompilePlanBucket = Field(default_factory=CompilePlanBucket)
    conflicts: CompilePlanBucket = Field(default_factory=CompilePlanBucket)
    evidence_count: int = 0


class CompilePlanSummary(BaseModel):
    topics: CompilePlanBucket = Field(default_factory=CompilePlanBucket)
    regulations: CompilePlanBucket = Field(default_factory=CompilePlanBucket)
    procedures: CompilePlanBucket = Field(default_factory=CompilePlanBucket)
    conflicts: CompilePlanBucket = Field(default_factory=CompilePlanBucket)
    evidence_count: int = 0
    documents: list[CompilePlanDocument] = Field(default_factory=list)


class CompileProgressDetails(BaseModel):
    counters: dict[str, StageCounter] = Field(default_factory=dict)
    plan: CompilePlanSummary | None = None
    usage_total: TokenUsageSummary = Field(default_factory=TokenUsageSummary)
    usage_by_stage: dict[str, TokenUsageSummary] = Field(default_factory=dict)


class JobRecord(BaseModel):
    job_id: str
    kind: str
    status: str
    created_at: str
    updated_at: str
    payload: dict[str, object]
    stage: str | None = None
    progress: float | None = None
    message: str | None = None
    error: str | None = None
    compile: CompileProgressDetails | None = None


class WorkspaceStatus(BaseModel):
    workspace: Path
    indexed_documents: int
    raw_files: int
    source_pages: int
    long_documents_pending_pageindex: int
    queued_jobs: int
    completed_jobs: int
    failed_jobs: int
    compiled_documents: int
    evidence_pages: int
    conflict_pages: int
    credentials_ready: bool


class CompileResult(BaseModel):
    workspace: Path
    processed_files: int
    created_pages: int
    job_id: str | None


class CredentialStatus(BaseModel):
    workspace: Path
    provider: str | None
    model: str | None
    has_api_key: bool
    validated: bool
    validated_at: str | None


class ProviderOption(BaseModel):
    provider_id: str
    label: str
    description: str
    model_examples: list[str]
    supports_custom_model: bool = True


class WorkspaceCreatedResponse(BaseModel):
    workspace_id: str
    name: str
    root_path: str
    created: bool


class WorkspaceListItem(BaseModel):
    workspace_id: str
    name: str
    root_path: str
    initialized: bool
    status: WorkspaceStatus | None = None
    credentials: CredentialStatus | None = None


class WorkspacesResponse(BaseModel):
    items: list[WorkspaceListItem]


class ProvidersResponse(BaseModel):
    items: list[ProviderOption]


class DocumentsResponse(BaseModel):
    workspace: str
    items: list[DocumentRecord]
