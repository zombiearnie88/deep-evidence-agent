"""Local runtime API for Evidence Brain."""

from __future__ import annotations

import os
from pathlib import Path
import re
import threading
from typing import Annotated

from evidence_compiler.api import (
    add_path,
    find_workspace_root,
    get_credentials_status,
    get_provider_catalog,
    get_status,
    init_workspace,
    list_documents,
    run_compile_job,
    set_workspace_credentials,
    validate_workspace_credentials,
)
from knowledge_models.compiler_api import (
    AddResult,
    CompileResult,
    CredentialStatus,
    DocumentsResponse,
    JobRecord,
    ProvidersResponse,
    WorkspaceCreatedResponse,
    WorkspaceInitResult,
    WorkspaceListItem,
    WorkspacesResponse,
)
from evidence_compiler.state import JobStore
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
import uvicorn


REPO_ROOT = Path(__file__).resolve().parents[4]
WORKSPACES_DIR = Path(
    os.environ.get(
        "EVIDENCE_BRAIN_WORKSPACES_DIR", str(REPO_ROOT / "var" / "dev-workspaces")
    )
).resolve()
WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Evidence Brain Service", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:1420",
        "http://127.0.0.1:1420",
        "tauri://localhost",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class CreateWorkspaceRequest(BaseModel):
    """Payload for creating or initializing one workspace."""

    name: str | None = Field(default=None, description="Workspace name slug")
    path: str | None = Field(
        default=None, description="Absolute or relative workspace path"
    )
    model: str | None = Field(default=None, description="Default compiler model")


class AddDocumentRequest(BaseModel):
    """Payload for ingesting files into a workspace."""

    workspace: str
    path: str


class CompileRequest(BaseModel):
    """Payload for enqueueing a compile job."""

    workspace: str


class CredentialRequest(BaseModel):
    """Payload for saving workspace LLM credentials."""

    provider: str
    model: str
    api_key: str


def _workspace_from_ref(ref: str) -> Path:
    """Resolve workspace reference from id-like or absolute path input.

    Args:
        ref: Workspace id or path from API payload/query.

    Returns:
        Absolute workspace path under configured root or the absolute input path.
    """
    raw = Path(ref).expanduser()
    if raw.is_absolute():
        return raw.resolve()
    candidate = (WORKSPACES_DIR / raw).resolve()
    discovered = find_workspace_root(candidate)
    if discovered is not None:
        return discovered
    return candidate


def _workspace_name(value: str | None) -> str:
    """Normalize user-provided workspace name to a safe slug.

    Args:
        value: Optional workspace display name from request payload.

    Returns:
        Lowercase slug safe for workspace directory naming.
    """
    if not value:
        return "workspace"
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-").lower()
    return slug or "workspace"


def _compile_in_background(workspace: Path, job_id: str) -> None:
    """Execute compile pipeline in a background thread.

    Args:
        workspace: Absolute workspace path.
        job_id: Existing compile job id to update during execution.

    Returns:
        None.
    """
    try:
        run_compile_job(workspace, job_id=job_id)
    except Exception as error:
        jobs = JobStore(workspace / ".brain" / "jobs")
        jobs.update(
            job_id,
            status="failed",
            stage="failed",
            progress=1.0,
            error=str(error),
            message="Compilation failed",
        )


@app.get("/health")
async def health() -> dict[str, str]:
    """Return process liveness status.

    Returns:
        Static object with `status=ok` when service process is alive.
    """
    return {"status": "ok"}


@app.get("/workspaces", response_model=WorkspacesResponse)
async def get_workspaces() -> WorkspacesResponse:
    """List workspaces under configured root with optional status snapshot.

    Returns:
        Workspace list including initialization flag and optional status/credential
        snapshots for initialized workspaces.
    """
    entries: list[WorkspaceListItem] = []
    for workspace in sorted(path for path in WORKSPACES_DIR.iterdir() if path.is_dir()):
        initialized = (workspace / ".brain" / "config.yaml").exists() and (
            workspace / ".brain" / "hashes.json"
        ).exists()
        entry = WorkspaceListItem(
            workspace_id=workspace.name,
            name=workspace.name,
            root_path=str(workspace),
            initialized=initialized,
        )
        if initialized:
            try:
                entry.status = get_status(workspace)
                entry.credentials = get_credentials_status(workspace)
            except ValueError:
                entry.status = None
                entry.credentials = None
        entries.append(entry)
    return WorkspacesResponse(items=entries)


@app.get("/providers", response_model=ProvidersResponse)
async def get_providers() -> ProvidersResponse:
    """Return curated LiteLLM provider catalog.

    Returns:
        Provider options used by desktop credential setup flow.
    """
    return ProvidersResponse(items=get_provider_catalog())


@app.post("/workspaces", response_model=WorkspaceCreatedResponse)
async def create_workspace(payload: CreateWorkspaceRequest) -> WorkspaceCreatedResponse:
    """Create or initialize one workspace and return its identity metadata.

    Args:
        payload: Workspace creation request with optional name/path/model.

    Returns:
        Workspace identity and whether this call created a new workspace.

    Raises:
        HTTPException: If workspace initialization fails.
    """
    workspace = (
        _workspace_from_ref(payload.path)
        if payload.path
        else WORKSPACES_DIR / _workspace_name(payload.name)
    )
    try:
        result: WorkspaceInitResult = init_workspace(workspace, model=payload.model)
    except Exception as error:  # pragma: no cover
        raise HTTPException(status_code=400, detail=str(error)) from error

    return WorkspaceCreatedResponse(
        workspace_id=workspace.name,
        name=workspace.name,
        root_path=str(workspace),
        created=result.created,
    )


@app.get("/documents", response_model=DocumentsResponse)
async def get_documents(
    workspace: Annotated[str, Query(description="Workspace id or path")],
) -> DocumentsResponse:
    """Return indexed documents for one workspace.

    Args:
        workspace: Workspace id or path passed as query parameter.

    Returns:
        Workspace reference and list of indexed document records.

    Raises:
        HTTPException: If workspace is not initialized or not found.
    """
    workspace_path = _workspace_from_ref(workspace)
    try:
        documents = list_documents(workspace_path)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return DocumentsResponse(workspace=str(workspace_path), items=documents)


@app.post("/documents/ingest", response_model=AddResult)
async def ingest_documents(payload: AddDocumentRequest) -> AddResult:
    """Ingest one file or directory into a workspace via compiler API.

    Args:
        payload: Workspace + input path payload.

    Returns:
        Ingestion result summary including added/skipped/unsupported counts.

    Raises:
        HTTPException: If workspace is invalid or source path cannot be resolved.
    """
    workspace_path = _workspace_from_ref(payload.workspace)
    source_path = Path(payload.path).expanduser().resolve()
    try:
        result = add_path(workspace_path, source_path)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except FileNotFoundError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return result


@app.post("/jobs/compile", response_model=CompileResult)
async def enqueue_compile(payload: CompileRequest) -> CompileResult:
    """Queue compilation for one workspace and return initial job metadata.

    Args:
        payload: Workspace compile request.

    Returns:
        Compile result containing queued job id and initial document count.

    Raises:
        HTTPException: If credentials are missing or workspace is invalid.
    """
    workspace_path = _workspace_from_ref(payload.workspace)
    try:
        documents = list_documents(workspace_path)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    if not documents:
        raise HTTPException(status_code=400, detail="No indexed documents found")

    credential_status = get_credentials_status(workspace_path)
    if not credential_status.has_api_key:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "missing_llm_credentials",
                "message": "Missing workspace credentials",
            },
        )

    jobs = JobStore(workspace_path / ".brain" / "jobs")
    active_compile_jobs = [
        job
        for job in jobs.list_jobs()
        if job.kind == "compile" and job.status in {"queued", "running"}
    ]
    if active_compile_jobs:
        active_job = active_compile_jobs[-1]
        raise HTTPException(
            status_code=409,
            detail={
                "code": "compile_already_running",
                "message": "A compile job is already queued or running for this workspace",
                "job_id": active_job.job_id,
            },
        )

    job = jobs.create(
        kind="compile",
        status="queued",
        payload={
            "document_hashes": [document.file_hash for document in documents],
            "document_count": len(documents),
        },
    )

    thread = threading.Thread(
        target=_compile_in_background,
        args=(workspace_path, job.job_id),
        daemon=True,
        name=f"compile-{job.job_id}",
    )
    thread.start()

    return CompileResult(
        workspace=workspace_path,
        processed_files=len(documents),
        created_pages=0,
        job_id=job.job_id,
    )


@app.get(
    "/workspaces/{workspace_id}/credentials/status", response_model=CredentialStatus
)
async def workspace_credentials_status(workspace_id: str) -> CredentialStatus:
    """Return credential status for workspace.

    Args:
        workspace_id: Workspace id from route path.

    Returns:
        Credential status object without exposing raw API key.

    Raises:
        HTTPException: If workspace is invalid or not initialized.
    """
    workspace_path = _workspace_from_ref(workspace_id)
    try:
        status = get_credentials_status(workspace_path)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return status


@app.put("/workspaces/{workspace_id}/credentials", response_model=CredentialStatus)
async def save_workspace_credentials_api(
    workspace_id: str, payload: CredentialRequest
) -> CredentialStatus:
    """Save workspace provider/model/api key into OS keychain.

    Args:
        workspace_id: Workspace id from route path.
        payload: Provider/model/api_key payload.

    Returns:
        Updated credential status.

    Raises:
        HTTPException: If payload validation or workspace checks fail.
    """
    workspace_path = _workspace_from_ref(workspace_id)
    try:
        status = set_workspace_credentials(
            workspace_path,
            provider=payload.provider,
            model=payload.model,
            api_key=payload.api_key,
        )
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return status


@app.post(
    "/workspaces/{workspace_id}/credentials/validate",
    response_model=CredentialStatus,
)
async def validate_workspace_credentials_api(workspace_id: str) -> CredentialStatus:
    """Validate current workspace credentials using LiteLLM.

    Args:
        workspace_id: Workspace id from route path.

    Returns:
        Credential status with updated validation marker and timestamp.

    Raises:
        HTTPException: If workspace is invalid or provider call fails.
    """
    workspace_path = _workspace_from_ref(workspace_id)
    try:
        status = validate_workspace_credentials(workspace_path)
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return status


@app.get("/jobs/{job_id}", response_model=JobRecord)
async def get_job(
    job_id: str,
    workspace: Annotated[str, Query(description="Workspace id or path")],
) -> JobRecord:
    """Return a single job record by id for one workspace.

    Args:
        job_id: Job identifier from route path.
        workspace: Workspace id or path passed as query parameter.

    Returns:
        One persisted job record with status, progress, and typed compile telemetry.

    Raises:
        HTTPException: If workspace is invalid or job id does not exist.
    """
    workspace_path = _workspace_from_ref(workspace)
    try:
        list_documents(workspace_path)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    jobs = JobStore(workspace_path / ".brain" / "jobs")
    try:
        job = jobs.read(job_id)
    except FileNotFoundError as error:
        raise HTTPException(
            status_code=404, detail=f"Job not found: {job_id}"
        ) from error
    return job


def run() -> None:
    """Run uvicorn server for local development.

    Returns:
        None. Starts local FastAPI app process.
    """
    uvicorn.run("brain_service.main:app", host="127.0.0.1", port=8787, reload=False)


if __name__ == "__main__":
    run()
