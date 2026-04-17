"""Public API for the Evidence Brain compiler."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from evidence_compiler.config import DEFAULT_CONFIG, load_config, save_config
from evidence_compiler.converter import SUPPORTED_EXTENSIONS, convert_document
from evidence_compiler.models import (
    AddResult,
    CompileResult,
    DocumentRecord,
    WorkspaceInitResult,
    WorkspaceStatus,
)
from evidence_compiler.schema.workspace import (
    AGENTS_MD,
    WORKSPACE_DIRS,
    index_md_template,
    log_md_template,
    required_markers,
)
from evidence_compiler.state import HashRegistry, JobStore, now_iso


def _resolve_workspace(path: Path) -> Path:
    """Return an absolute resolved workspace path."""
    return path.resolve()


def _assert_workspace_initialized(workspace: Path) -> None:
    """Ensure workspace has required compiler markers.

    Raises:
        ValueError: If workspace does not contain required `.brain` marker files.
    """
    missing = [path for path in required_markers(workspace) if not path.exists()]
    if missing:
        raise ValueError(f"Workspace is not initialized: {workspace}")


def find_workspace_root(start: Path) -> Path | None:
    """Find nearest initialized workspace by walking upward from `start`.

    Args:
        start: File or directory path used as lookup starting point.

    Returns:
        The nearest workspace root if marker files are found, otherwise `None`.
    """
    current = start.resolve()
    while True:
        markers = required_markers(current)
        if all(path.exists() for path in markers):
            return current
        if current.parent == current:
            return None
        current = current.parent


def init_workspace(workspace: Path, model: str | None = None) -> WorkspaceInitResult:
    """Initialize workspace folders and baseline compiler state.

    Creates the expected workspace layout, writes schema templates (AGENTS/index/log)
    when missing, and initializes `.brain/config.yaml` plus `.brain/hashes.json`.

    Args:
        workspace: Target workspace directory.
        model: Optional model override written into workspace config.

    Returns:
        WorkspaceInitResult describing the resolved workspace and whether it was newly
        initialized.

    Example:
        >>> from pathlib import Path
        >>> from evidence_compiler.api import init_workspace
        >>> result = init_workspace(Path("var/dev-workspaces/demo"), model="gpt-5.4-mini")
        >>> result.workspace.name
        'demo'
    """
    workspace = _resolve_workspace(workspace)
    config_path = workspace / ".brain" / "config.yaml"
    hashes_path = workspace / ".brain" / "hashes.json"

    already_initialized = config_path.exists() and hashes_path.exists()

    for relative in WORKSPACE_DIRS:
        (workspace / relative).mkdir(parents=True, exist_ok=True)

    wiki_dir = workspace / "wiki"
    agents_path = wiki_dir / "AGENTS.md"
    if not agents_path.exists():
        agents_path.write_text(AGENTS_MD, encoding="utf-8")

    index_path = wiki_dir / "index.md"
    if not index_path.exists():
        index_path.write_text(index_md_template(), encoding="utf-8")

    log_path = wiki_dir / "log.md"
    if not log_path.exists():
        log_path.write_text(log_md_template(), encoding="utf-8")

    config = load_config(config_path)
    if model:
        config["model"] = model
    if not config_path.exists() or model:
        save_config(config_path, config)

    if not hashes_path.exists():
        hashes_path.write_text("{}\n", encoding="utf-8")

    return WorkspaceInitResult(workspace=workspace, created=not already_initialized)


def _discover_files(path: Path) -> list[Path]:
    """Collect files from a file-or-directory input path."""
    if path.is_file():
        return [path]
    if path.is_dir():
        return sorted(item for item in path.rglob("*") if item.is_file())
    return []


def _append_log(workspace: Path, operation: str, description: str) -> None:
    """Append one operation entry to `wiki/log.md`."""
    log_path = workspace / "wiki" / "log.md"
    from datetime import UTC, datetime

    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(f"## [{timestamp}] {operation} | {description}\n\n")


def add_path(workspace: Path, path: Path) -> AddResult:
    """Ingest one file or a directory of files into a workspace.

    The function validates workspace markers, discovers eligible files, performs
    conversion and deduplication, registers document metadata in hash registry,
    and records an ingest job when at least one file is added.

    Args:
        workspace: Initialized workspace root.
        path: File or directory to ingest.

    Returns:
        AddResult with discovered/added/skipped/unsupported breakdown and optional
        ingest job id.

    Raises:
        ValueError: If workspace is not initialized.
        FileNotFoundError: If the input path does not exist.

    Example:
        >>> from pathlib import Path
        >>> from evidence_compiler.api import add_path, init_workspace
        >>> ws = Path("var/dev-workspaces/demo")
        >>> _ = init_workspace(ws)
        >>> result = add_path(ws, Path("docs/FINAL_PLAN.md"))
        >>> result.discovered_files >= 1
        True
    """
    workspace = _resolve_workspace(workspace)
    _assert_workspace_initialized(workspace)

    target = path.resolve()
    if not target.exists():
        raise FileNotFoundError(f"Path does not exist: {target}")
    files = _discover_files(target)
    if not files:
        return AddResult(workspace=workspace, discovered_files=0)

    config = load_config(workspace / ".brain" / "config.yaml")
    threshold = int(
        config.get("pageindex_threshold", DEFAULT_CONFIG["pageindex_threshold"])
    )
    registry = HashRegistry(workspace / ".brain" / "hashes.json")
    jobs = JobStore(workspace / ".brain" / "jobs")

    result = AddResult(workspace=workspace, discovered_files=len(files))
    added_hashes: list[str] = []

    for file_path in files:
        suffix = file_path.suffix.lower()
        if suffix not in SUPPORTED_EXTENSIONS:
            result.unsupported_files.append(file_path)
            continue

        converted = convert_document(file_path, workspace, threshold, registry)
        if converted.skipped:
            result.skipped_files.append(file_path)
            continue

        doc = DocumentRecord(
            doc_id=str(uuid4()),
            name=file_path.name,
            file_hash=converted.file_hash,
            file_type=("long_pdf" if converted.is_long_doc else suffix.lstrip(".")),
            raw_path=converted.raw_path,
            source_path=converted.source_path,
            is_long_doc=converted.is_long_doc,
            requires_pageindex=converted.is_long_doc,
            page_count=converted.page_count,
            status=("pending_pageindex" if converted.is_long_doc else "ingested"),
            created_at=now_iso(),
        )
        registry.add_document(doc)
        result.added_documents.append(doc)
        added_hashes.append(doc.file_hash)

    if added_hashes:
        job = jobs.create(
            kind="ingest",
            payload={
                "document_hashes": added_hashes,
                "added": len(result.added_documents),
                "skipped": len(result.skipped_files),
            },
            status="completed",
        )
        result.job_id = job.job_id
        _append_log(
            workspace,
            "ingest",
            f"added={len(result.added_documents)} skipped={len(result.skipped_files)}",
        )

    return result


def list_documents(workspace: Path) -> list[DocumentRecord]:
    """List all indexed document records for a workspace.

    Args:
        workspace: Initialized workspace root.

    Returns:
        Document records sorted by creation timestamp.

    Raises:
        ValueError: If workspace is not initialized.
    """
    workspace = _resolve_workspace(workspace)
    _assert_workspace_initialized(workspace)
    registry = HashRegistry(workspace / ".brain" / "hashes.json")
    return registry.list_documents()


def get_status(workspace: Path) -> WorkspaceStatus:
    """Compute high-level workspace status for UI/API consumption.

    Includes counts for indexed docs, raw/source artifacts, pending long-doc
    PageIndex work, and queued/completed/failed jobs.

    Args:
        workspace: Initialized workspace root.

    Returns:
        Aggregated workspace status snapshot.

    Raises:
        ValueError: If workspace is not initialized.
    """
    workspace = _resolve_workspace(workspace)
    _assert_workspace_initialized(workspace)

    documents = list_documents(workspace)
    jobs = JobStore(workspace / ".brain" / "jobs").list_jobs()

    queued_jobs = sum(1 for job in jobs if job.status == "queued")
    completed_jobs = sum(1 for job in jobs if job.status == "completed")
    failed_jobs = sum(1 for job in jobs if job.status == "failed")

    raw_count = len(
        [path for path in (workspace / "raw").glob("**/*") if path.is_file()]
    )
    source_count = len(
        [
            path
            for path in (workspace / "wiki" / "sources").glob("*.md")
            if path.is_file()
        ]
    )
    long_pending = sum(1 for document in documents if document.requires_pageindex)

    return WorkspaceStatus(
        workspace=workspace,
        indexed_documents=len(documents),
        raw_files=raw_count,
        source_pages=source_count,
        long_documents_pending_pageindex=long_pending,
        queued_jobs=queued_jobs,
        completed_jobs=completed_jobs,
        failed_jobs=failed_jobs,
    )


def compile_workspace(workspace: Path) -> CompileResult:
    """Queue a compilation job for current workspace documents.

    Milestone A behavior is intentionally minimal: this function records a
    placeholder compile job and logs the operation, without producing final wiki
    synthesis pages yet.

    Args:
        workspace: Initialized workspace root.

    Returns:
        CompileResult with processed-file count and queued job id.

    Raises:
        ValueError: If workspace is not initialized.
    """
    workspace = _resolve_workspace(workspace)
    _assert_workspace_initialized(workspace)
    documents = list_documents(workspace)
    jobs = JobStore(workspace / ".brain" / "jobs")

    payload: dict[str, object] = {
        "document_hashes": [document.file_hash for document in documents],
        "document_count": len(documents),
        "note": "Compilation pipeline placeholder for Milestone A",
    }
    job = jobs.create(kind="compile", payload=payload, status="queued")
    _append_log(workspace, "compile", f"job={job.job_id} docs={len(documents)}")

    return CompileResult(
        workspace=workspace,
        processed_files=len(documents),
        created_pages=0,
        job_id=job.job_id,
    )
