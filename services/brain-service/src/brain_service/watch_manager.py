"""Workspace watch lifecycle and auto-compile orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
import threading
import time
from typing import Any
from uuid import uuid4

from evidence_compiler.api import add_path, get_credentials_status, list_documents, run_compile_job
from evidence_compiler.converter import SUPPORTED_EXTENSIONS
from evidence_compiler.state import HashRegistry, JobStore, now_iso
from evidence_compiler.watcher import FileWatcherHandle, start_file_watcher
from knowledge_models.compiler_api import (
    AddResult,
    CompileResult,
    WatchBacklogItem,
    WatchBacklogResponse,
    WatchRequest,
    WatchStatus,
)

_STABILIZATION_INTERVAL_SECONDS = 0.25
_STABILIZATION_MAX_CHECKS = 16


class WatchManagerError(Exception):
    """Base exception carrying a stable API error code."""

    code: str

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class CompileAlreadyRunningError(WatchManagerError):
    """Raised when a workspace already has an active compile job."""

    job_id: str

    def __init__(self, job_id: str) -> None:
        super().__init__(
            "compile_already_running",
            "A compile job is already queued or running for this workspace",
        )
        self.job_id = job_id


class MissingCredentialsError(WatchManagerError):
    """Raised when a workspace is missing LLM credentials."""

    def __init__(self) -> None:
        super().__init__("missing_llm_credentials", "Missing workspace credentials")


@dataclass
class _WatchSession:
    session_id: str
    workspace: Path
    paths: list[Path]
    auto_compile: bool
    debounce_seconds: float
    handle: FileWatcherHandle
    last_ingest_job_id: str | None = None
    last_compile_job_id: str | None = None
    active_compile_job_id: str | None = None
    last_error: str | None = None
    dirty_after_compile: bool = False
    processing_paths: int = 0
    updated_at: str | None = None


class WatchManager:
    """Manage watch sessions and compile orchestration per workspace."""

    def __init__(self) -> None:
        self._sessions: dict[Path, _WatchSession] = {}
        self._runtime_locks: dict[Path, threading.RLock] = {}
        self._manager_lock: Any = threading.Lock()

    def _runtime_lock(self, workspace: Path) -> threading.RLock:
        with self._manager_lock:
            lock = self._runtime_locks.get(workspace)
            if lock is None:
                lock = threading.RLock()
                self._runtime_locks[workspace] = lock
            return lock

    def ensure_workspace_initialized(self, workspace: Path) -> None:
        """Validate that the workspace exists and has been initialized."""
        list_documents(workspace)

    @staticmethod
    def _raw_dir(workspace: Path) -> Path:
        return workspace / "raw"

    def ingest_path(self, workspace: Path, source_path: Path) -> AddResult:
        """Serialize manual ingest with watcher-triggered work for one workspace."""
        self.ensure_workspace_initialized(workspace)
        with self._runtime_lock(workspace):
            result = add_path(workspace, source_path)
            session = self._sessions.get(workspace)
            if session is not None:
                if result.job_id:
                    session.last_ingest_job_id = result.job_id
                session.last_error = None
                session.updated_at = now_iso()
            return result

    def enqueue_compile(self, workspace: Path) -> CompileResult:
        """Queue one compile job for a workspace if none is active."""
        self.ensure_workspace_initialized(workspace)
        with self._runtime_lock(workspace):
            return self._queue_compile_locked(workspace)

    def get_status(self, workspace: Path) -> WatchStatus:
        """Return current watch status for one workspace."""
        self.ensure_workspace_initialized(workspace)
        raw_dir = self._raw_dir(workspace)
        with self._runtime_lock(workspace):
            session = self._sessions.get(workspace)
            if session is None:
                return WatchStatus(
                    workspace=workspace,
                    enabled=False,
                    paths=[raw_dir],
                    updated_at=now_iso(),
                )
            return WatchStatus(
                workspace=workspace,
                enabled=True,
                paths=session.paths,
                auto_compile=session.auto_compile,
                debounce_seconds=session.debounce_seconds,
                pending_paths=session.processing_paths + session.handle.pending_count(),
                active_compile_job_id=session.active_compile_job_id,
                last_ingest_job_id=session.last_ingest_job_id,
                last_compile_job_id=session.last_compile_job_id,
                last_error=session.last_error,
                updated_at=session.updated_at,
            )

    def list_backlog(self, workspace: Path) -> WatchBacklogResponse:
        """Return raw-folder files that are present but not yet indexed."""
        self.ensure_workspace_initialized(workspace)
        raw_dir = self._raw_dir(workspace)
        if not raw_dir.exists():
            return WatchBacklogResponse(workspace=workspace, root=raw_dir)

        with self._runtime_lock(workspace):
            registry = HashRegistry(workspace / ".brain" / "hashes.json")
            indexed_raw_paths = {
                document.raw_path.resolve() for document in registry.list_documents()
            }
            known_hashes = set(registry.all_entries())

        items: list[WatchBacklogItem] = []
        for path in sorted(item for item in raw_dir.rglob("*") if item.is_file()):
            stable_path, error = self._prepare_watch_path(workspace, path)
            if stable_path is None or error is not None:
                continue
            if stable_path in indexed_raw_paths:
                continue
            try:
                file_hash = HashRegistry.hash_file(stable_path)
                stat_result = stable_path.stat()
            except OSError:
                continue
            if file_hash in known_hashes:
                continue
            items.append(
                WatchBacklogItem(
                    path=stable_path,
                    name=stable_path.name,
                    size_bytes=stat_result.st_size,
                    modified_at=datetime.fromtimestamp(
                        stat_result.st_mtime, UTC
                    ).isoformat(),
                )
            )

        return WatchBacklogResponse(
            workspace=workspace,
            root=raw_dir,
            items=items,
            total=len(items),
        )

    def ingest_backlog_paths(self, workspace: Path, raw_paths: list[str]) -> AddResult:
        """Ingest selected raw-folder backlog files after explicit user confirmation."""
        self.ensure_workspace_initialized(workspace)
        resolved_paths = self._resolve_backlog_paths(workspace, raw_paths)

        prepared_paths: list[Path] = []
        skipped_paths: list[Path] = []
        last_error: str | None = None
        for raw_path in resolved_paths:
            path, error = self._prepare_watch_path(workspace, raw_path)
            if error is not None:
                last_error = error
                skipped_paths.append(raw_path)
                continue
            if path is None:
                skipped_paths.append(raw_path)
                continue
            prepared_paths.append(path)

        with self._runtime_lock(workspace):
            session = self._sessions.get(workspace)
            return self._ingest_prepared_paths_locked(
                workspace,
                prepared_paths,
                session,
                discovered_files=len(resolved_paths),
                skipped_paths=skipped_paths,
                last_error=last_error,
            )

    def put_session(self, workspace: Path, request: WatchRequest) -> WatchStatus:
        """Start or replace raw-folder watch session for one workspace."""
        self.ensure_workspace_initialized(workspace)
        raw_dir = self._raw_dir(workspace)
        raw_dir.mkdir(parents=True, exist_ok=True)

        previous: _WatchSession | None = None
        with self._runtime_lock(workspace):
            previous = self._sessions.get(workspace)
            session_id = str(uuid4())

            def _on_paths(paths: list[Path]) -> None:
                self._handle_watch_paths(workspace, session_id, paths)

            handle = start_file_watcher(
                [raw_dir],
                _on_paths,
                debounce_seconds=request.debounce_seconds,
            )
            session = _WatchSession(
                session_id=session_id,
                workspace=workspace,
                paths=[raw_dir],
                auto_compile=request.auto_compile,
                debounce_seconds=request.debounce_seconds,
                handle=handle,
                last_ingest_job_id=(previous.last_ingest_job_id if previous else None),
                last_compile_job_id=(previous.last_compile_job_id if previous else None),
                active_compile_job_id=self._active_compile_job_id_locked(workspace),
                updated_at=now_iso(),
            )
            self._sessions[workspace] = session
            handle.start()

        if previous is not None:
            previous.handle.stop()
        return self.get_status(workspace)

    def stop_session(self, workspace: Path) -> WatchStatus:
        """Stop and remove one in-memory watch session."""
        self.ensure_workspace_initialized(workspace)
        raw_dir = self._raw_dir(workspace)
        session: _WatchSession | None = None
        with self._runtime_lock(workspace):
            session = self._sessions.pop(workspace, None)
        if session is not None:
            session.handle.stop()
        return WatchStatus(
            workspace=workspace,
            enabled=False,
            paths=[raw_dir],
            updated_at=now_iso(),
        )

    def stop_all(self) -> None:
        """Stop every active watcher, typically during process shutdown."""
        with self._manager_lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            session.handle.stop()

    def _handle_watch_paths(
        self, workspace: Path, session_id: str, paths: list[Path]
    ) -> None:
        if not paths:
            return
        with self._runtime_lock(workspace):
            session = self._sessions.get(workspace)
            if session is None or session.session_id != session_id:
                return
            session.processing_paths += len(paths)
            session.updated_at = now_iso()

        prepared_paths: list[Path] = []
        last_error: str | None = None
        seen_paths: set[Path] = set()
        for raw_path in paths:
            path, error = self._prepare_watch_path(workspace, raw_path)
            if error is not None:
                last_error = error
                continue
            if path is None or path in seen_paths:
                continue
            seen_paths.add(path)
            prepared_paths.append(path)

        with self._runtime_lock(workspace):
            session = self._sessions.get(workspace)
            if session is None or session.session_id != session_id:
                return

            try:
                self._ingest_prepared_paths_locked(
                    workspace,
                    prepared_paths,
                    session,
                    discovered_files=len(paths),
                    last_error=last_error,
                )
            finally:
                session.processing_paths = max(0, session.processing_paths - len(paths))

    def _ingest_prepared_paths_locked(
        self,
        workspace: Path,
        prepared_paths: list[Path],
        session: _WatchSession | None,
        *,
        discovered_files: int,
        skipped_paths: list[Path] | None = None,
        last_error: str | None = None,
    ) -> AddResult:
        result = AddResult(workspace=workspace, discovered_files=discovered_files)
        if skipped_paths:
            result.skipped_files.extend(skipped_paths)

        for path in prepared_paths:
            try:
                path_result = add_path(workspace, path)
            except Exception as error:  # pragma: no cover - defensive guard
                last_error = f"{path}: {error}"
                result.skipped_files.append(path)
                continue

            result.discovered_files += max(0, path_result.discovered_files - 1)
            result.added_documents.extend(path_result.added_documents)
            result.skipped_files.extend(path_result.skipped_files)
            result.unsupported_files.extend(path_result.unsupported_files)
            if path_result.job_id:
                result.job_id = path_result.job_id
                if session is not None:
                    session.last_ingest_job_id = path_result.job_id

        if session is not None:
            session.last_error = last_error
            session.updated_at = now_iso()
            if result.added_documents and session.auto_compile:
                try:
                    self._queue_compile_locked(workspace)
                except CompileAlreadyRunningError as error:
                    session.active_compile_job_id = error.job_id
                    session.dirty_after_compile = True
                    session.updated_at = now_iso()
                except WatchManagerError as error:
                    session.last_error = str(error)
                    session.updated_at = now_iso()

        return result

    def _prepare_watch_path(
        self, workspace: Path, path: Path
    ) -> tuple[Path | None, str | None]:
        raw_dir = self._raw_dir(workspace)
        stable_path, error = self._stabilize_path(path)
        if error is not None or stable_path is None:
            return stable_path, error
        if self._is_hidden_path(stable_path):
            return None, None
        if stable_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            return None, None
        if not self._is_relative_to(stable_path, raw_dir):
            return None, None
        return stable_path, None

    def _stabilize_path(self, path: Path) -> tuple[Path | None, str | None]:
        resolved = path.expanduser().resolve()
        previous_signature: tuple[int, int] | None = None
        for _ in range(_STABILIZATION_MAX_CHECKS):
            try:
                stat_result = resolved.stat()
            except FileNotFoundError:
                return None, None
            except OSError as error:
                return None, f"Failed to inspect watched path {resolved}: {error}"

            if not resolved.is_file():
                return None, None

            signature = (stat_result.st_size, stat_result.st_mtime_ns)
            if signature == previous_signature:
                return resolved, None
            previous_signature = signature
            time.sleep(_STABILIZATION_INTERVAL_SECONDS)
        return None, f"File did not stabilize before ingest: {resolved}"

    def _queue_compile_locked(self, workspace: Path) -> CompileResult:
        documents = list_documents(workspace)
        target_documents = [
            document for document in documents if document.status != "compiled"
        ]

        credential_status = get_credentials_status(workspace)
        if target_documents and not credential_status.has_api_key:
            raise MissingCredentialsError()

        active_job_id = self._active_compile_job_id_locked(workspace)
        if active_job_id is not None:
            raise CompileAlreadyRunningError(active_job_id)

        jobs = JobStore(workspace / ".brain" / "jobs")
        job = jobs.create(
            kind="compile",
            status="queued",
            payload={
                "document_hashes": [
                    document.file_hash for document in target_documents
                ],
                "document_count": len(target_documents),
                "provider": credential_status.provider,
                "model": credential_status.model,
            },
        )

        session = self._sessions.get(workspace)
        if session is not None:
            session.last_compile_job_id = job.job_id
            session.active_compile_job_id = job.job_id
            session.dirty_after_compile = False
            session.updated_at = now_iso()

        thread = threading.Thread(
            target=self._compile_in_background,
            args=(workspace, job.job_id),
            daemon=True,
            name=f"compile-{job.job_id}",
        )
        thread.start()

        return CompileResult(
            workspace=workspace,
            processed_files=len(target_documents),
            created_pages=0,
            job_id=job.job_id,
        )

    def _compile_in_background(self, workspace: Path, job_id: str) -> None:
        try:
            run_compile_job(workspace, job_id=job_id)
        except Exception as error:  # pragma: no cover - background thread failure path
            jobs = JobStore(workspace / ".brain" / "jobs")
            jobs.update(
                job_id,
                status="failed",
                stage="failed",
                progress=1.0,
                error=str(error),
                message="Compilation failed",
            )
        finally:
            self._after_compile_finished(workspace)

    def _after_compile_finished(self, workspace: Path) -> None:
        with self._runtime_lock(workspace):
            session = self._sessions.get(workspace)
            if session is None:
                return

            session.active_compile_job_id = self._active_compile_job_id_locked(workspace)
            session.updated_at = now_iso()

            if not session.dirty_after_compile or session.active_compile_job_id is not None:
                return

            session.dirty_after_compile = False
            try:
                self._queue_compile_locked(workspace)
            except CompileAlreadyRunningError as error:
                session.dirty_after_compile = True
                session.active_compile_job_id = error.job_id
                session.updated_at = now_iso()
            except WatchManagerError as error:
                session.last_error = str(error)
                session.updated_at = now_iso()

    def _active_compile_job_id_locked(self, workspace: Path) -> str | None:
        jobs = JobStore(workspace / ".brain" / "jobs")
        active_jobs = [
            job
            for job in jobs.list_jobs()
            if job.kind == "compile" and job.status in {"queued", "running"}
        ]
        if not active_jobs:
            return None
        return active_jobs[-1].job_id

    def _resolve_backlog_paths(self, workspace: Path, raw_paths: list[str]) -> list[Path]:
        if not raw_paths:
            raise WatchManagerError(
                "invalid_watch_backlog_request",
                "At least one backlog path is required",
            )

        raw_dir = self._raw_dir(workspace)
        resolved_paths: list[Path] = []
        seen_paths: set[Path] = set()
        for raw_path in raw_paths:
            candidate = Path(raw_path).expanduser()
            if not candidate.is_absolute():
                raise WatchManagerError(
                    "invalid_watch_backlog_request",
                    f"Backlog path must be absolute: {raw_path}",
                )
            path = candidate.resolve()
            if not self._is_relative_to(path, raw_dir):
                raise WatchManagerError(
                    "invalid_watch_backlog_request",
                    f"Backlog path must stay inside workspace raw: {path}",
                )
            if path in seen_paths:
                continue
            seen_paths.add(path)
            resolved_paths.append(path)
        return resolved_paths

    @staticmethod
    def _is_hidden_path(path: Path) -> bool:
        return any(part.startswith(".") for part in path.parts if part not in {path.anchor, ""})

    @staticmethod
    def _is_relative_to(path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
        except ValueError:
            return False
        return True
