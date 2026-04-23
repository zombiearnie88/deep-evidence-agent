"""Persistence helpers for workspace hashes and jobs."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from evidence_compiler.models import DocumentRecord, JobRecord


def now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(UTC).isoformat()


class HashRegistry:
    """Persistent registry mapping SHA-256 hash to metadata."""

    def __init__(self, path: Path) -> None:
        """Initialize registry from JSON file if it exists.

        Args:
            path: Path to `.brain/hashes.json`.
        """
        self._path: Path = path
        if path.exists():
            with path.open("r", encoding="utf-8") as fh:
                self._data: dict[str, dict[str, object]] = json.load(fh)
        else:
            self._data = {}

    def is_known(self, file_hash: str) -> bool:
        """Return whether `file_hash` is already indexed."""
        return file_hash in self._data

    def all_entries(self) -> dict[str, dict[str, object]]:
        """Return a shallow copy of all registry entries."""
        return dict(self._data)

    def add_document(self, document: DocumentRecord) -> None:
        """Store one document record under its file hash key.

        Args:
            document: Document metadata to persist.
        """
        payload = document.model_dump(mode="json")
        self._data[document.file_hash] = payload
        self._persist()

    def list_documents(self) -> list[DocumentRecord]:
        """Deserialize and return all documents sorted by creation timestamp."""
        documents: list[DocumentRecord] = []
        for payload in self._data.values():
            documents.append(DocumentRecord.model_validate(payload))
        return sorted(documents, key=lambda d: d.created_at)

    def update_document(self, file_hash: str, **fields: object) -> None:
        """Update one existing document entry and persist changes."""
        current = self._data.get(file_hash)
        if not current:
            return
        updated = dict(current)
        for key, value in fields.items():
            if isinstance(value, Path):
                updated[key] = str(value)
            else:
                updated[key] = value
        self._data[file_hash] = updated
        self._persist()

    def _persist(self) -> None:
        """Write registry state to disk as JSON."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self._path.with_suffix(f"{self._path.suffix}.tmp")
        with tmp_path.open("w", encoding="utf-8") as fh:
            json.dump(self._data, fh, indent=2)
        tmp_path.replace(self._path)

    @staticmethod
    def hash_file(path: Path) -> str:
        """Compute SHA-256 hash for file content.

        Args:
            path: Path to file that should be hashed.

        Returns:
            Lowercase SHA-256 digest string.
        """
        hasher = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(65536), b""):
                hasher.update(chunk)
        return hasher.hexdigest()


class JobStore:
    """File-backed job store under .brain/jobs."""

    def __init__(self, jobs_dir: Path) -> None:
        """Create job store rooted at `jobs_dir`.

        Args:
            jobs_dir: Directory that stores one JSON file per job.
        """
        self._jobs_dir: Path = jobs_dir
        self._jobs_dir.mkdir(parents=True, exist_ok=True)

    def create(
        self, kind: str, payload: dict[str, object], status: str = "queued"
    ) -> JobRecord:
        """Create and persist a new job record.

        Args:
            kind: Job category, such as `ingest` or `compile`.
            payload: Arbitrary job payload for later processing.
            status: Initial job status.

        Returns:
            Newly created job record.
        """
        timestamp = now_iso()
        job = JobRecord(
            job_id=str(uuid4()),
            kind=kind,
            status=status,
            created_at=timestamp,
            updated_at=timestamp,
            payload=payload,
        )
        self._write(job)
        return job

    def update_status(self, job_id: str, status: str) -> JobRecord:
        """Update status for an existing job.

        Args:
            job_id: Identifier of the job file.
            status: New status string.

        Returns:
            Updated job record.
        """
        job = self.read(job_id)
        job.status = status
        job.updated_at = now_iso()
        self._write(job)
        return job

    def update(
        self,
        job_id: str,
        *,
        status: str | None = None,
        stage: str | None = None,
        progress: float | None = None,
        message: str | None = None,
        error: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> JobRecord:
        """Update selected fields for an existing job."""
        job = self.read(job_id)
        if status is not None:
            job.status = status
        if stage is not None:
            job.stage = stage
        if progress is not None:
            job.progress = progress
        if message is not None:
            job.message = message
        if error is not None:
            job.error = error
        if payload is not None:
            job.payload = payload
        job.updated_at = now_iso()
        self._write(job)
        return job

    def read(self, job_id: str) -> JobRecord:
        """Read one job by id.

        Args:
            job_id: Identifier of the job file.

        Returns:
            Parsed job record.
        """
        path = self._jobs_dir / f"{job_id}.json"
        with path.open("r", encoding="utf-8") as fh:
            payload = json.load(fh)
        return JobRecord.model_validate(payload)

    def list_jobs(self) -> list[JobRecord]:
        """Return all jobs sorted by creation timestamp."""
        jobs: list[JobRecord] = []
        for path in sorted(self._jobs_dir.glob("*.json")):
            with path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
            jobs.append(JobRecord.model_validate(payload))
        return sorted(jobs, key=lambda job: job.created_at)

    def _write(self, job: JobRecord) -> None:
        """Persist one job record to disk."""
        path = self._jobs_dir / f"{job.job_id}.json"
        tmp_path = path.with_suffix(".json.tmp")
        with tmp_path.open("w", encoding="utf-8") as fh:
            json.dump(job.model_dump(mode="json"), fh, indent=2)
        tmp_path.replace(path)
