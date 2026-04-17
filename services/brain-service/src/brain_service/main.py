"""Local runtime API for Evidence Brain."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
import os
from pathlib import Path
import re
from typing import Any

from evidence_compiler.api import (
    add_path,
    compile_workspace,
    find_workspace_root,
    get_status,
    init_workspace,
    list_documents,
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


def _serialize(value: Any) -> Any:
    """Convert dataclasses and paths into JSON-serializable values."""
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value):
        return {key: _serialize(item) for key, item in asdict(value).items()}
    if isinstance(value, list):
        return [_serialize(item) for item in value]
    if isinstance(value, dict):
        return {key: _serialize(item) for key, item in value.items()}
    return value


def _workspace_from_ref(ref: str) -> Path:
    """Resolve workspace reference from id-like or absolute path input."""
    raw = Path(ref).expanduser()
    if raw.is_absolute():
        return raw.resolve()
    candidate = (WORKSPACES_DIR / raw).resolve()
    discovered = find_workspace_root(candidate)
    if discovered is not None:
        return discovered
    return candidate


def _workspace_name(value: str | None) -> str:
    """Normalize user-provided workspace name to a safe slug."""
    if not value:
        return "workspace"
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "-", value).strip("-").lower()
    return slug or "workspace"


@app.get("/health")
async def health() -> dict[str, str]:
    """Return process liveness status."""
    return {"status": "ok"}


@app.get("/workspaces")
async def get_workspaces() -> dict[str, object]:
    """List workspaces under configured root with optional status snapshot."""
    entries: list[dict[str, object]] = []
    for workspace in sorted(path for path in WORKSPACES_DIR.iterdir() if path.is_dir()):
        initialized = (workspace / ".brain" / "config.yaml").exists() and (
            workspace / ".brain" / "hashes.json"
        ).exists()
        entry: dict[str, object] = {
            "workspace_id": workspace.name,
            "name": workspace.name,
            "root_path": str(workspace),
            "initialized": initialized,
        }
        if initialized:
            try:
                entry["status"] = _serialize(get_status(workspace))
            except ValueError:
                entry["status"] = None
        entries.append(entry)
    return {"items": entries}


@app.post("/workspaces")
async def create_workspace(payload: CreateWorkspaceRequest) -> dict[str, object]:
    """Create or initialize one workspace and return its identity metadata."""
    workspace = (
        _workspace_from_ref(payload.path)
        if payload.path
        else WORKSPACES_DIR / _workspace_name(payload.name)
    )
    try:
        result = init_workspace(workspace, model=payload.model)
    except Exception as error:  # pragma: no cover
        raise HTTPException(status_code=400, detail=str(error)) from error

    return {
        "workspace_id": workspace.name,
        "name": workspace.name,
        "root_path": str(workspace),
        "created": result.created,
    }


@app.get("/documents")
async def get_documents(
    workspace: str = Query(..., description="Workspace id or path"),
) -> dict[str, object]:
    """Return indexed documents for one workspace."""
    workspace_path = _workspace_from_ref(workspace)
    try:
        documents = list_documents(workspace_path)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return {"workspace": str(workspace_path), "items": _serialize(documents)}


@app.post("/documents/ingest")
async def ingest_documents(payload: AddDocumentRequest) -> dict[str, object]:
    """Ingest one file or directory into a workspace via compiler API."""
    workspace_path = _workspace_from_ref(payload.workspace)
    source_path = Path(payload.path).expanduser().resolve()
    try:
        result = add_path(workspace_path, source_path)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except FileNotFoundError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return _serialize(result)


@app.post("/jobs/compile")
async def enqueue_compile(payload: CompileRequest) -> dict[str, object]:
    """Queue a compile job for one workspace."""
    workspace_path = _workspace_from_ref(payload.workspace)
    try:
        result = compile_workspace(workspace_path)
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return _serialize(result)


@app.get("/jobs/{job_id}")
async def get_job(
    job_id: str, workspace: str = Query(..., description="Workspace id or path")
) -> dict[str, object]:
    """Return a single job record by id for one workspace."""
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
    return _serialize(job)


def run() -> None:
    """Run uvicorn server for local development."""
    uvicorn.run("brain_service.main:app", host="127.0.0.1", port=8787, reload=False)


if __name__ == "__main__":
    run()
