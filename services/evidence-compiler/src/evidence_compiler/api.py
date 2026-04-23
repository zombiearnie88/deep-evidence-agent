"""Public API for the Evidence Brain compiler."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from evidence_compiler.compiler import compile_documents, rebuild_index
from evidence_compiler.config import DEFAULT_CONFIG, load_config, save_config
from evidence_compiler.converter import SUPPORTED_EXTENSIONS, convert_document
from evidence_compiler.credentials import (
    get_workspace_credential_status,
    resolve_workspace_credentials,
    save_workspace_credentials,
    validate_credentials,
)
from evidence_compiler.lint import run_structural_lint
from evidence_compiler.models import (
    AddResult,
    CompileResult,
    CredentialStatus,
    DocumentRecord,
    ProviderOption,
    WorkspaceInitResult,
    WorkspaceStatus,
)
from evidence_compiler.providers import list_provider_options, normalize_provider
from evidence_compiler.schema.workspace import (
    AGENTS_MD,
    WORKSPACE_DIRS,
    index_md_template,
    log_md_template,
    required_markers,
)
from evidence_compiler.state import HashRegistry, JobStore, now_iso


class MissingCredentialsError(ValueError):
    """Raised when workspace credential bundle is missing or incomplete."""


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
        if all(path.exists() for path in required_markers(current)):
            return current
        if current.parent == current:
            return None
        current = current.parent


def _append_log(workspace: Path, operation: str, description: str) -> None:
    """Append one operation entry to `wiki/log.md`."""
    log_path = workspace / "wiki" / "log.md"
    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S")
    with log_path.open("a", encoding="utf-8") as fh:
        fh.write(f"## [{timestamp}] {operation} | {description}\n\n")


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


def get_provider_catalog() -> list[ProviderOption]:
    """Return provider choices for credential setup UI."""
    return list_provider_options()


def get_credentials_status(workspace: Path) -> CredentialStatus:
    """Return workspace credential status."""
    workspace = _resolve_workspace(workspace)
    _assert_workspace_initialized(workspace)
    return get_workspace_credential_status(workspace)


def set_workspace_credentials(
    workspace: Path,
    *,
    provider: str,
    model: str,
    api_key: str,
) -> CredentialStatus:
    """Store provider/model/api_key for one workspace."""
    workspace = _resolve_workspace(workspace)
    _assert_workspace_initialized(workspace)
    provider_id = normalize_provider(provider)
    return save_workspace_credentials(
        workspace,
        provider=provider_id,
        model=model,
        api_key=api_key,
        validated=False,
        validated_at=None,
    )


def validate_workspace_credentials(workspace: Path) -> CredentialStatus:
    """Validate current workspace credential bundle with a tiny LiteLLM call."""
    workspace = _resolve_workspace(workspace)
    _assert_workspace_initialized(workspace)
    provider, model, api_key = resolve_workspace_credentials(workspace)
    validate_credentials(provider=provider, model=model, api_key=api_key)
    return save_workspace_credentials(
        workspace,
        provider=provider,
        model=model,
        api_key=api_key,
        validated=True,
        validated_at=now_iso(),
    )


def get_status(workspace: Path) -> WorkspaceStatus:
    """Compute high-level workspace status for UI/API consumption.

    Includes counts for indexed docs, raw/source artifacts, compile outputs,
    pending long-doc PageIndex work, and queued/completed/failed jobs.

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
    credential_status = get_workspace_credential_status(workspace)

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
    compiled_documents = sum(
        1 for document in documents if document.status == "compiled"
    )
    evidence_pages = len(list((workspace / "wiki" / "evidence").glob("*.md")))
    conflict_pages = len(list((workspace / "wiki" / "conflicts").glob("*.md")))

    return WorkspaceStatus(
        workspace=workspace,
        indexed_documents=len(documents),
        raw_files=raw_count,
        source_pages=source_count,
        long_documents_pending_pageindex=long_pending,
        queued_jobs=queued_jobs,
        completed_jobs=completed_jobs,
        failed_jobs=failed_jobs,
        compiled_documents=compiled_documents,
        evidence_pages=evidence_pages,
        conflict_pages=conflict_pages,
        credentials_ready=credential_status.has_api_key,
    )


def compile_workspace(workspace: Path) -> CompileResult:
    """Compile indexed documents into wiki taxonomy pages and lint report.

    This is the Milestone 2 compile pipeline. It requires workspace credentials,
    runs extraction/synthesis, rebuilds `wiki/index.md`, emits structural lint,
    and updates per-document compile status plus compile job progress metadata.

    Args:
        workspace: Initialized workspace root.

    Returns:
        CompileResult with processed-document count, created page count, and job id.

    Raises:
        ValueError: If workspace is not initialized or has no indexed documents.
        MissingCredentialsError: If provider/model/api-key bundle is missing.
    """
    return run_compile_job(workspace, job_id=None)


def run_compile_job(workspace: Path, job_id: str | None) -> CompileResult:
    """Run compile pipeline using an existing or newly-created job record.

    Args:
        workspace: Initialized workspace root.
        job_id: Existing compile job id to reuse, or `None` to create a new job.

    Returns:
        CompileResult with processed-document count, created page count, and job id.

    Raises:
        ValueError: If workspace is not initialized or has no indexed documents.
        MissingCredentialsError: If provider/model/api-key bundle is missing.
    """
    workspace = _resolve_workspace(workspace)
    _assert_workspace_initialized(workspace)

    documents = list_documents(workspace)
    if not documents:
        raise ValueError("No indexed documents found")

    try:
        provider, model, api_key = resolve_workspace_credentials(workspace)
    except ValueError as error:
        raise MissingCredentialsError(str(error)) from error

    config = load_config(workspace / ".brain" / "config.yaml")
    jobs = JobStore(workspace / ".brain" / "jobs")

    stage_progress: dict[str, float] = {
        "preparing": 0.05,
        "indexing-long-docs": 0.12,
        "summarizing": 0.24,
        "planning-taxonomy": 0.36,
        "writing-topics": 0.5,
        "writing-regulations": 0.62,
        "writing-procedures": 0.72,
        "writing-conflicts": 0.82,
        "writing-evidence": 0.9,
        "backlinking": 0.94,
        "updating-index": 0.97,
        "linting": 0.99,
        "completed": 1.0,
    }

    def _set_stage(stage: str, message: str) -> None:
        jobs.update(
            job.job_id,
            stage=stage,
            progress=stage_progress.get(stage, 0.5),
            message=message,
        )

    if job_id is None:
        job = jobs.create(
            kind="compile",
            status="running",
            payload={
                "document_hashes": [document.file_hash for document in documents],
                "document_count": len(documents),
                "provider": provider,
                "model": model,
            },
        )
        _set_stage("preparing", "Preparing compile pipeline")
    else:
        job = jobs.update(
            job_id,
            status="running",
            stage="preparing",
            progress=stage_progress["preparing"],
            message="Preparing compile pipeline",
            payload={
                "document_hashes": [document.file_hash for document in documents],
                "document_count": len(documents),
                "provider": provider,
                "model": model,
            },
        )

    registry = HashRegistry(workspace / ".brain" / "hashes.json")

    try:
        artifacts = compile_documents(
            workspace,
            documents,
            provider=provider,
            model=model,
            api_key=api_key,
            language=str(config.get("language", DEFAULT_CONFIG["language"])),
            stage_callback=_set_stage,
        )

        _set_stage("updating-index", "Updating wiki index")
        rebuild_index(workspace, artifacts)

        _set_stage("linting", "Running structural lint checks")
        lint_report = run_structural_lint(workspace)
        lint_path = (
            workspace
            / "wiki"
            / "reports"
            / f"lint_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.md"
        )
        lint_path.parent.mkdir(parents=True, exist_ok=True)
        lint_path.write_text(lint_report, encoding="utf-8")

        for document in documents:
            updates: dict[str, object] = {
                "status": "compiled",
                "requires_pageindex": False,
            }
            artifact_path = artifacts.pageindex_artifacts.get(document.file_hash)
            if artifact_path:
                updates["pageindex_artifact_path"] = artifact_path
            registry.update_document(document.file_hash, **updates)

        jobs.update(
            job.job_id,
            status="completed",
            stage="completed",
            progress=1.0,
            message="Compilation completed",
            payload={
                **job.payload,
                "created_pages": artifacts.total_pages,
                "lint_report": str(lint_path),
            },
        )

        _append_log(
            workspace,
            "compile",
            f"job={job.job_id} docs={len(documents)} pages={artifacts.total_pages}",
        )
        return CompileResult(
            workspace=workspace,
            processed_files=len(documents),
            created_pages=artifacts.total_pages,
            job_id=job.job_id,
        )
    except Exception as error:
        jobs.update(
            job.job_id,
            status="failed",
            stage="failed",
            progress=1.0,
            error=str(error),
            message="Compilation failed",
        )
        raise
