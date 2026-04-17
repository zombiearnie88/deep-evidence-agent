"""Typed models used by the evidence compiler API."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class WorkspaceInitResult:
    """Result returned by workspace initialization."""

    workspace: Path
    created: bool


@dataclass
class DocumentRecord:
    """Indexed document metadata stored in hash registry."""

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


@dataclass
class AddResult:
    """Outcome summary for a single `add_path` request."""

    workspace: Path
    discovered_files: int
    added_documents: list[DocumentRecord] = field(default_factory=list)
    skipped_files: list[Path] = field(default_factory=list)
    unsupported_files: list[Path] = field(default_factory=list)
    job_id: str | None = None


@dataclass
class JobRecord:
    """Serialized background job metadata."""

    job_id: str
    kind: str
    status: str
    created_at: str
    updated_at: str
    payload: dict[str, object]


@dataclass
class WorkspaceStatus:
    """Aggregated counters that describe workspace health and progress."""

    workspace: Path
    indexed_documents: int
    raw_files: int
    source_pages: int
    long_documents_pending_pageindex: int
    queued_jobs: int
    completed_jobs: int
    failed_jobs: int


@dataclass
class CompileResult:
    """Result returned after enqueuing a compilation job."""

    workspace: Path
    processed_files: int
    created_pages: int
    job_id: str | None
