"""Milestone A integration smoke tests.

These tests validate the minimum skeleton + ingestion flow through both the
compiler library API and the brain-service HTTP layer.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
import tempfile
import unittest

from fastapi.testclient import TestClient

from evidence_compiler.api import (
    add_path,
    compile_workspace,
    get_status,
    init_workspace,
)


class CompilerMilestoneATest(unittest.TestCase):
    """Validate compiler workspace/job scaffolding for Milestone A."""

    def test_init_add_status_compile_flow(self) -> None:
        """Initialize workspace, ingest one doc, inspect status, and queue compile."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "workspace"
            source = root / "regulation.md"
            source.write_text("# Rule\n\nUse local workspace mode.", encoding="utf-8")

            init_result = init_workspace(workspace)
            self.assertTrue(init_result.created)

            add_result = add_path(workspace, source)
            self.assertEqual(add_result.discovered_files, 1)
            self.assertEqual(len(add_result.added_documents), 1)
            self.assertIsNotNone(add_result.job_id)

            status = get_status(workspace)
            self.assertEqual(status.indexed_documents, 1)
            self.assertEqual(status.raw_files, 1)
            self.assertEqual(status.source_pages, 1)

            compile_result = compile_workspace(workspace)
            self.assertEqual(compile_result.processed_files, 1)
            self.assertIsNotNone(compile_result.job_id)


class BrainServiceMilestoneATest(unittest.TestCase):
    """Validate brain-service direct integration with compiler API."""

    def test_workspace_ingest_and_job_endpoints(self) -> None:
        """Create workspace, ingest file, enqueue compile, and read queued job."""
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_root = Path(temp_dir) / "workspaces"
            workspace_root.mkdir(parents=True, exist_ok=True)

            source = Path(temp_dir) / "policy.txt"
            source.write_text("Hospital policy A", encoding="utf-8")

            previous = os.environ.get("EVIDENCE_BRAIN_WORKSPACES_DIR")
            os.environ["EVIDENCE_BRAIN_WORKSPACES_DIR"] = str(workspace_root)
            try:
                module = importlib.import_module("brain_service.main")
                module = importlib.reload(module)
                client = TestClient(module.app)

                health = client.get("/health")
                self.assertEqual(health.status_code, 200)

                created = client.post("/workspaces", json={"name": "pilot-site"})
                self.assertEqual(created.status_code, 200)
                payload = created.json()
                workspace_id = payload["workspace_id"]

                ingested = client.post(
                    "/documents/ingest",
                    json={"workspace": workspace_id, "path": str(source)},
                )
                self.assertEqual(ingested.status_code, 200)
                ingest_payload = ingested.json()
                self.assertEqual(ingest_payload["discovered_files"], 1)
                self.assertEqual(len(ingest_payload["added_documents"]), 1)

                queued = client.post("/jobs/compile", json={"workspace": workspace_id})
                self.assertEqual(queued.status_code, 200)
                queue_payload = queued.json()
                job_id = queue_payload["job_id"]

                job = client.get(f"/jobs/{job_id}", params={"workspace": workspace_id})
                self.assertEqual(job.status_code, 200)
                self.assertEqual(job.json()["status"], "queued")
            finally:
                if previous is None:
                    os.environ.pop("EVIDENCE_BRAIN_WORKSPACES_DIR", None)
                else:
                    os.environ["EVIDENCE_BRAIN_WORKSPACES_DIR"] = previous


if __name__ == "__main__":
    unittest.main()
