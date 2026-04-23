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
