"""Persistence helpers for workspace hashes and jobs."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
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
        self._path = path
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
        payload = asdict(document)
        payload["raw_path"] = str(document.raw_path)
        payload["source_path"] = (
            str(document.source_path) if document.source_path else None
        )
        self._data[document.file_hash] = payload
        self._persist()

    def list_documents(self) -> list[DocumentRecord]:
        """Deserialize and return all documents sorted by creation timestamp."""
        documents: list[DocumentRecord] = []
        for payload in self._data.values():
            documents.append(
                DocumentRecord(
                    doc_id=str(payload["doc_id"]),
                    name=str(payload["name"]),
                    file_hash=str(payload["file_hash"]),
                    file_type=str(payload["file_type"]),
                    raw_path=Path(str(payload["raw_path"])),
                    source_path=Path(str(payload["source_path"]))
                    if payload.get("source_path")
                    else None,
                    is_long_doc=bool(payload["is_long_doc"]),
                    requires_pageindex=bool(payload["requires_pageindex"]),
                    page_count=int(payload["page_count"])
                    if payload.get("page_count") is not None
                    else None,
                    status=str(payload["status"]),
                    created_at=str(payload["created_at"]),
                )
            )
        return sorted(documents, key=lambda d: d.created_at)

    def _persist(self) -> None:
        """Write registry state to disk as JSON."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with self._path.open("w", encoding="utf-8") as fh:
            json.dump(self._data, fh, indent=2)

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
        self._jobs_dir = jobs_dir
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
        return JobRecord(**payload)

    def list_jobs(self) -> list[JobRecord]:
        """Return all jobs sorted by creation timestamp."""
        jobs: list[JobRecord] = []
        for path in sorted(self._jobs_dir.glob("*.json")):
            with path.open("r", encoding="utf-8") as fh:
                payload = json.load(fh)
            jobs.append(JobRecord(**payload))
        return sorted(jobs, key=lambda job: job.created_at)

    def _write(self, job: JobRecord) -> None:
        """Persist one job record to disk."""
        path = self._jobs_dir / f"{job.job_id}.json"
        with path.open("w", encoding="utf-8") as fh:
            json.dump(asdict(job), fh, indent=2)
