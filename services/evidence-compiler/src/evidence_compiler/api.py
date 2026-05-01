"""Public API for the Evidence Brain compiler."""

from __future__ import annotations

import json
import shutil
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from evidence_compiler.compiler import compile_documents, rebuild_index
from evidence_compiler.config import DEFAULT_CONFIG, load_config, save_config
from evidence_compiler.converter import SUPPORTED_EXTENSIONS, convert_document
from evidence_compiler.credentials import (
    delete_workspace_credentials,
    get_workspace_credential_status,
    resolve_workspace_credentials,
    save_workspace_credentials,
    validate_credentials,
)
from evidence_compiler.lint import run_structural_lint
from evidence_compiler.models import (
    AddResult,
    CompilePlanSummary,
    CompileProgressDetails,
    CompileResult,
    CompilePlanPreviewResult,
    ConfigSnapshot,
    CredentialStatus,
    DocumentRecord,
    JobRecord,
    JobsResponse,
    ProviderOption,
    StageCounter,
    TokenUsageSummary,
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


_STAGE_PROGRESS_RANGES: dict[str, tuple[float, float]] = {
    "preparing": (0.02, 0.05),
    "indexing-long-docs": (0.05, 0.12),
    "summarizing": (0.12, 0.22),
    "planning-evidence": (0.22, 0.3),
    "drafting-evidence": (0.3, 0.38),
    "verifying-evidence": (0.38, 0.46),
    "planning-taxonomy": (0.46, 0.56),
    "writing-topics": (0.56, 0.66),
    "writing-regulations": (0.66, 0.74),
    "writing-procedures": (0.74, 0.8),
    "writing-evidence": (0.8, 0.86),
    "writing-conflicts": (0.86, 0.92),
    "backlinking": (0.92, 0.95),
    "updating-index": (0.95, 0.97),
    "linting": (0.97, 0.99),
    "completed": (1.0, 1.0),
    "failed": (1.0, 1.0),
}


def _merge_usage(
    current: TokenUsageSummary, delta: TokenUsageSummary
) -> TokenUsageSummary:
    current_has_data = (
        current.calls > 0
        or current.prompt_tokens > 0
        or current.completion_tokens > 0
        or current.total_tokens > 0
    )
    available = delta.available if not current_has_data else current.available and delta.available
    return TokenUsageSummary(
        prompt_tokens=current.prompt_tokens + delta.prompt_tokens,
        completion_tokens=current.completion_tokens + delta.completion_tokens,
        total_tokens=current.total_tokens + delta.total_tokens,
        calls=current.calls + delta.calls,
        available=available,
    )


class _CompileJobTracker:
    """Track compile progress in memory and persist only meaningful changes."""

    def __init__(self, jobs: JobStore, job: JobRecord) -> None:
        self._jobs: JobStore = jobs
        self.job_id: str = job.job_id
        self._payload: dict[str, object] = dict(job.payload)
        self._status: str = job.status
        self._stage: str | None = job.stage
        self._progress: float = float(job.progress) if job.progress is not None else 0.0
        self._message: str | None = job.message
        self._error: str | None = job.error
        self._compile: CompileProgressDetails = (
            job.compile.model_copy(deep=True)
            if job.compile is not None
            else CompileProgressDetails()
        )
        self._last_snapshot: str = self._snapshot()

    def _snapshot(self) -> str:
        return json.dumps(
            {
                "status": self._status,
                "stage": self._stage,
                "progress": round(self._progress, 6),
                "message": self._message,
                "error": self._error,
                "payload": self._payload,
                "compile": self._compile.model_dump(mode="json"),
            },
            sort_keys=True,
        )

    def _stage_progress(self, stage: str, completed: int | None = None, total: int | None = None) -> float:
        start, end = _STAGE_PROGRESS_RANGES.get(stage, (0.0, 1.0))
        if total is None or total <= 0 or completed is None:
            return start
        ratio = max(0.0, min(1.0, completed / total))
        return start + ((end - start) * ratio)

    def flush(self) -> None:
        snapshot = self._snapshot()
        if snapshot == self._last_snapshot:
            return
        self._jobs.update(
            self.job_id,
            status=self._status,
            stage=self._stage,
            progress=self._progress,
            message=self._message,
            error=self._error,
            payload=self._payload,
            compile=self._compile,
        )
        self._last_snapshot = snapshot

    def set_stage(self, stage: str, message: str) -> None:
        self._status = "running"
        self._stage = stage
        self._message = message
        self._error = None
        counter = self._compile.counters.get(stage)
        if counter is None:
            self._progress = self._stage_progress(stage)
        else:
            self._progress = self._stage_progress(stage, counter.completed, counter.total)
        self.flush()

    def set_counter(
        self,
        stage: str,
        completed: int,
        total: int,
        unit: str,
        item_label: str | None = None,
    ) -> None:
        self._compile.counters[stage] = StageCounter(
            completed=completed,
            total=total,
            unit=unit,
            item_label=item_label,
        )
        if self._stage == stage:
            self._progress = self._stage_progress(stage, completed, total)
        self.flush()

    def set_plan(self, plan_summary: CompilePlanSummary) -> None:
        self._compile.plan = plan_summary
        self.flush()

    def add_usage(self, stage: str, usage_delta: TokenUsageSummary) -> None:
        current_stage_usage = self._compile.usage_by_stage.get(stage, TokenUsageSummary())
        self._compile.usage_by_stage[stage] = _merge_usage(current_stage_usage, usage_delta)
        self._compile.usage_total = _merge_usage(self._compile.usage_total, usage_delta)
        self.flush()

    def complete(self, *, payload_updates: dict[str, object] | None = None) -> None:
        if payload_updates:
            self._payload = {**self._payload, **payload_updates}
        self._status = "completed"
        self._stage = "completed"
        self._progress = 1.0
        self._message = "Compilation completed"
        self._error = None
        self.flush()

    def fail(self, error: Exception) -> None:
        self._status = "failed"
        self._stage = "failed"
        self._progress = 1.0
        self._message = "Compilation failed"
        self._error = str(error)
        self.flush()


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


def delete_credentials(workspace: Path) -> CredentialStatus:
    """Delete the current workspace credential bundle.

    Args:
        workspace: Initialized workspace root.

    Returns:
        Updated credential status after deletion.

    Raises:
        ValueError: If workspace is not initialized.
    """
    workspace = _resolve_workspace(workspace)
    _assert_workspace_initialized(workspace)
    delete_workspace_credentials(workspace)
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


def list_jobs(workspace: Path) -> JobsResponse:
    """List persisted job records for one workspace.

    Args:
        workspace: Initialized workspace root.

    Returns:
        Workspace-scoped job list sorted by creation timestamp.

    Raises:
        ValueError: If workspace is not initialized.
    """
    workspace = _resolve_workspace(workspace)
    _assert_workspace_initialized(workspace)
    jobs = JobStore(workspace / ".brain" / "jobs").list_jobs()
    return JobsResponse(workspace=workspace, items=jobs)


def get_job(workspace: Path, job_id: str) -> JobRecord:
    """Return one persisted job record by id.

    Args:
        workspace: Initialized workspace root.
        job_id: Job identifier to load from `.brain/jobs`.

    Returns:
        Parsed job record.

    Raises:
        ValueError: If workspace is not initialized.
        FileNotFoundError: If the requested job file does not exist.
    """
    workspace = _resolve_workspace(workspace)
    _assert_workspace_initialized(workspace)
    return JobStore(workspace / ".brain" / "jobs").read(job_id)


def wait_for_job(
    workspace: Path,
    job_id: str,
    *,
    timeout_seconds: float | None = None,
    interval_seconds: float = 0.2,
) -> JobRecord:
    """Poll a workspace job until it reaches a terminal state.

    Args:
        workspace: Initialized workspace root.
        job_id: Job identifier to poll.
        timeout_seconds: Optional timeout budget before raising `TimeoutError`.
        interval_seconds: Polling interval between reads.

    Returns:
        Final job record once status becomes `completed` or `failed`.

    Raises:
        ValueError: If workspace is not initialized.
        FileNotFoundError: If the requested job file does not exist.
        TimeoutError: If the job does not finish before the timeout budget.
    """
    workspace = _resolve_workspace(workspace)
    _assert_workspace_initialized(workspace)
    deadline = time.monotonic() + timeout_seconds if timeout_seconds is not None else None
    while True:
        job = JobStore(workspace / ".brain" / "jobs").read(job_id)
        if job.status in {"completed", "failed"}:
            return job
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError(f"Timed out waiting for job: {job_id}")
        time.sleep(interval_seconds)


def get_config_snapshot(workspace: Path) -> ConfigSnapshot:
    """Return the effective compiler config for one workspace.

    Args:
        workspace: Initialized workspace root.

    Returns:
        Workspace path plus merged config values.

    Raises:
        ValueError: If workspace is not initialized.
    """
    workspace = _resolve_workspace(workspace)
    _assert_workspace_initialized(workspace)
    values = load_config(workspace / ".brain" / "config.yaml")
    return ConfigSnapshot(workspace=workspace, values=values)


def set_config_value(workspace: Path, key: str, value: object) -> ConfigSnapshot:
    """Persist one workspace-local compiler config value.

    Args:
        workspace: Initialized workspace root.
        key: Config key to write into `.brain/config.yaml`.
        value: Parsed scalar, list, or mapping value to persist.

    Returns:
        Updated merged config snapshot after save.

    Raises:
        ValueError: If workspace is not initialized or `key` is empty.
    """
    workspace = _resolve_workspace(workspace)
    _assert_workspace_initialized(workspace)
    normalized_key = key.strip()
    if not normalized_key:
        raise ValueError("Config key cannot be empty")
    config_path = workspace / ".brain" / "config.yaml"
    values = load_config(config_path)
    values[normalized_key] = value
    save_config(config_path, values)
    return ConfigSnapshot(workspace=workspace, values=load_config(config_path))


def preview_compile_plan(workspace: Path) -> CompilePlanPreviewResult:
    """Build a read-only compile preview using a temporary shadow workspace.

    Args:
        workspace: Initialized workspace root.

    Returns:
        Pending-document count plus the compile plan summary that a compile would emit.

    Raises:
        ValueError: If workspace is not initialized.
        MissingCredentialsError: If provider/model/api-key bundle is missing for a
            non-empty preview target set.
    """
    workspace = _resolve_workspace(workspace)
    _assert_workspace_initialized(workspace)
    target_documents = _select_compile_targets(list_documents(workspace))
    if not target_documents:
        return CompilePlanPreviewResult(workspace=workspace, document_count=0)

    try:
        resolve_workspace_credentials(workspace)
    except ValueError as error:
        raise MissingCredentialsError(str(error)) from error

    with tempfile.TemporaryDirectory(prefix="evidence-compiler-plan-") as temp_dir:
        shadow_workspace = Path(temp_dir) / workspace.name
        shutil.copytree(workspace, shadow_workspace, dirs_exist_ok=True)
        result = compile_workspace(shadow_workspace)
        if result.job_id is None:
            return CompilePlanPreviewResult(
                workspace=workspace,
                document_count=len(target_documents),
            )
        shadow_job = get_job(shadow_workspace, result.job_id)
        plan = shadow_job.compile.plan if shadow_job.compile is not None else None
        return CompilePlanPreviewResult(
            workspace=workspace,
            document_count=len(target_documents),
            plan=plan or CompilePlanSummary(),
        )


def _select_compile_targets(documents: list[DocumentRecord]) -> list[DocumentRecord]:
    """Return the document records that still need compile work."""
    return [document for document in documents if document.status != "compiled"]


def compile_workspace(workspace: Path) -> CompileResult:
    """Compile indexed documents into wiki taxonomy pages and lint report.

    This is the Milestone 2 compile pipeline. When uncompiled targets exist, it
    requires workspace credentials, runs extraction/synthesis, rebuilds
    `wiki/index.md`, emits structural lint, and updates per-document compile
    status plus compile job progress metadata.

    Args:
        workspace: Initialized workspace root.

    Returns:
        CompileResult with processed-target count, created page count, and job id.
        If no documents need compile work, returns a successful no-op with
        `processed_files = 0`.

    Raises:
        ValueError: If workspace is not initialized.
        MissingCredentialsError: If provider/model/api-key bundle is missing for a
            non-empty compile target set.
    """
    return run_compile_job(workspace, job_id=None)


def run_compile_job(workspace: Path, job_id: str | None) -> CompileResult:
    """Run compile pipeline using an existing or newly-created job record.

    Args:
        workspace: Initialized workspace root.
        job_id: Existing compile job id to reuse, or `None` to create a new job.

    Returns:
        CompileResult with processed-target count, created page count, and job id.
        If no documents need compile work, returns a successful no-op with
        `processed_files = 0`.

    Raises:
        ValueError: If workspace is not initialized.
        MissingCredentialsError: If provider/model/api-key bundle is missing for a
            non-empty compile target set.
    """
    workspace = _resolve_workspace(workspace)
    _assert_workspace_initialized(workspace)

    all_documents = list_documents(workspace)
    target_documents = _select_compile_targets(all_documents)

    provider: str | None = None
    model: str | None = None
    api_key: str | None = None
    payload: dict[str, object] = {
        "document_hashes": [document.file_hash for document in target_documents],
        "document_count": len(target_documents),
    }
    if target_documents:
        try:
            provider, model, api_key = resolve_workspace_credentials(workspace)
        except ValueError as error:
            raise MissingCredentialsError(str(error)) from error
        payload.update({"provider": provider, "model": model})

    config = load_config(workspace / ".brain" / "config.yaml")
    jobs = JobStore(workspace / ".brain" / "jobs")

    if job_id is None:
        job = jobs.create(kind="compile", status="running", payload=payload)
    else:
        job = jobs.update(
            job_id,
            status="running",
            payload=payload,
        )

    tracker = _CompileJobTracker(jobs, job)
    tracker.set_stage("preparing", "Preparing compile pipeline")

    if not target_documents:
        tracker.complete(payload_updates={**payload, "created_pages": 0})
        _append_log(workspace, "compile", f"job={job.job_id} docs=0 pages=0")
        return CompileResult(
            workspace=workspace,
            processed_files=0,
            created_pages=0,
            job_id=job.job_id,
        )

    assert provider is not None
    assert model is not None
    assert api_key is not None

    registry = HashRegistry(workspace / ".brain" / "hashes.json")

    try:
        artifacts = compile_documents(
            workspace,
            target_documents,
            provider=provider,
            model=model,
            api_key=api_key,
            language=str(config.get("language", DEFAULT_CONFIG["language"])),
            stage_callback=tracker.set_stage,
            counter_callback=tracker.set_counter,
            plan_callback=tracker.set_plan,
            usage_callback=tracker.add_usage,
        )

        tracker.set_stage("updating-index", "Updating wiki index")
        rebuild_index(workspace, artifacts)

        tracker.set_stage("linting", "Running structural lint checks")
        lint_report = run_structural_lint(workspace)
        lint_path = (
            workspace
            / "wiki"
            / "reports"
            / f"lint_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}.md"
        )
        lint_path.parent.mkdir(parents=True, exist_ok=True)
        lint_path.write_text(lint_report, encoding="utf-8")

        for document in target_documents:
            updates: dict[str, object] = {
                "status": "compiled",
                "requires_pageindex": False,
            }
            artifact_path = artifacts.pageindex_artifacts.get(document.file_hash)
            if artifact_path:
                updates["pageindex_artifact_path"] = artifact_path
            registry.update_document(document.file_hash, **updates)

        tracker.complete(
            payload_updates={
                **payload,
                "created_pages": artifacts.total_pages,
                "lint_report": str(lint_path),
            },
        )

        _append_log(
            workspace,
            "compile",
            f"job={job.job_id} docs={len(target_documents)} pages={artifacts.total_pages}",
        )
        return CompileResult(
            workspace=workspace,
            processed_files=len(target_documents),
            created_pages=artifacts.total_pages,
            job_id=job.job_id,
        )
    except Exception as error:
        tracker.fail(error)
        raise
