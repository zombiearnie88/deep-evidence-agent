"""Milestone A integration smoke tests.

These tests validate the minimum skeleton + ingestion flow through both the
compiler library API and the brain-service HTTP layer.
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from evidence_compiler.api import (
    add_path,
    compile_workspace,
    set_workspace_credentials,
    get_status,
    init_workspace,
)


def _fake_llm_completion(*args, **kwargs):
    response_format = kwargs.get("response_format")
    schema_name = ""
    if isinstance(response_format, type):
        schema_name = response_format.__name__
    elif isinstance(response_format, dict):
        schema = response_format.get("json_schema")
        if isinstance(schema, dict):
            schema_name = str(schema.get("name") or "")

    if schema_name in {"summary_stage_result", "SummaryStageResult"}:
        payload = {
            "document_brief": "Test summary brief",
            "summary_markdown": "Test summary",
        }
    elif schema_name in {"taxonomy_plan_result", "TaxonomyPlanResult"}:
        payload = {
            "topics": {
                "create": [
                    {
                        "slug": "medication-safety",
                        "title": "Medication Safety",
                        "brief": "Safe dispensing checks",
                    }
                ],
                "update": [],
                "related": [],
            },
            "regulations": {
                "create": [
                    {
                        "slug": "dispensing-rule",
                        "title": "Dispensing Rule",
                        "brief": "Double-check dosage",
                    }
                ],
                "update": [],
                "related": [],
            },
            "procedures": {
                "create": [
                    {
                        "slug": "dispense-flow",
                        "title": "Dispense Flow",
                        "brief": "Verify, prepare, counsel",
                    }
                ],
                "update": [],
                "related": [],
            },
            "conflicts": {
                "create": [
                    {
                        "slug": "policy-mismatch",
                        "title": "Policy mismatch",
                        "brief": "Legacy SOP differs",
                    }
                ],
                "update": [],
                "related": [],
            },
            "evidence": [
                {
                    "claim": "Dose must be checked",
                    "quote": "Dose verification is mandatory",
                    "anchor": "line:1-2",
                    "normalized_claim": "dose-must-be-checked",
                }
            ],
        }
    elif schema_name in {"topic_page_output", "TopicPageOutput"}:
        payload = {
            "title": "Medication Safety",
            "brief": "Safe dispensing checks",
            "context_markdown": (
                "Medication safety explains why dosage verification and final "
                "dispensing checks matter before medication reaches the patient."
            ),
        }
    elif schema_name in {"regulation_page_output", "RegulationPageOutput"}:
        payload = {
            "title": "Dispensing Rule",
            "brief": "Double-check dosage",
            "requirement_markdown": (
                "Pharmacy staff must double-check dosage before dispensing any medication."
            ),
            "applicability_markdown": "Applies to outpatient pharmacy dispensing.",
            "authority_markdown": "Derived from the hospital policy summary.",
        }
    elif schema_name in {"procedure_page_output", "ProcedurePageOutput"}:
        payload = {
            "title": "Dispense Flow",
            "brief": "Verify, prepare, counsel",
            "steps": [
                "Verify prescription details and dosage.",
                "Prepare and label the medication.",
                "Counsel the patient before release.",
            ],
        }
    elif schema_name in {"conflict_page_output", "ConflictPageOutput"}:
        payload = {
            "title": "Policy mismatch",
            "brief": "Legacy SOP differs",
            "description_markdown": (
                "The legacy SOP uses a different verification sequence than the "
                "current medication-safety policy."
            ),
            "impacted_pages": ["[[regulations/dispensing-rule]]"],
        }
    elif schema_name in {"conflict_check_result", "ConflictCheckResult"}:
        payload = {
            "is_conflict": False,
            "title": "",
            "description": "",
        }
    elif schema_name == "_CredentialValidationResult":
        payload = {"status": "OK"}
    else:
        payload = {
            "summary": "Test summary",
            "topics": [
                {"title": "Medication Safety", "summary": "Safe dispensing checks"}
            ],
            "regulations": [
                {
                    "title": "Dispensing Rule",
                    "requirement": "Double-check dosage",
                    "applicability": "Outpatient pharmacy",
                }
            ],
            "procedures": [
                {"title": "Dispense Flow", "steps": "Verify, prepare, counsel"}
            ],
            "conflicts": [
                {"title": "Policy mismatch", "description": "Legacy SOP differs"}
            ],
            "evidence": [
                {
                    "claim": "Dose must be checked",
                    "quote": "Dose verification is mandatory",
                    "anchor": "line:1-2",
                }
            ],
        }

    class _Message:
        content = json.dumps(payload)

    class _Choice:
        message = _Message()

    class _Response:
        choices = [_Choice()]

    return _Response()


async def _fake_llm_acompletion(*args, **kwargs):
    return _fake_llm_completion(*args, **kwargs)


class _MemoryKeyring:
    def __init__(self) -> None:
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, account: str) -> str | None:
        return self._store.get((service, account))

    def set_password(self, service: str, account: str, password: str) -> None:
        self._store[(service, account)] = password

    def delete_password(self, service: str, account: str) -> None:
        self._store.pop((service, account), None)


class CompilerMilestoneATest(unittest.TestCase):
    """Validate compiler workspace/job scaffolding for Milestone A."""

    def test_init_add_status_compile_flow(self) -> None:
        """Initialize workspace, ingest one doc, inspect status, and queue compile."""
        memory_keyring = _MemoryKeyring()
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

            with (
                patch(
                    "evidence_compiler.credentials.keyring.get_password",
                    side_effect=memory_keyring.get_password,
                ),
                patch(
                    "evidence_compiler.credentials.keyring.set_password",
                    side_effect=memory_keyring.set_password,
                ),
                patch(
                    "evidence_compiler.credentials.keyring.delete_password",
                    side_effect=memory_keyring.delete_password,
                ),
            ):
                set_workspace_credentials(
                    workspace,
                    provider="openai",
                    model="gpt-5.4-mini",
                    api_key="test-key",
                )

                with (
                    patch(
                        "evidence_compiler.compiler.pipeline.litellm.completion",
                        side_effect=_fake_llm_completion,
                    ),
                    patch(
                        "evidence_compiler.compiler.pipeline.litellm.acompletion",
                        side_effect=_fake_llm_acompletion,
                    ),
                ):
                    compile_result = compile_workspace(workspace)
            self.assertEqual(compile_result.processed_files, 1)
            self.assertIsNotNone(compile_result.job_id)

            topic_page = (
                workspace / "wiki" / "topics" / "medication-safety.md"
            ).read_text(encoding="utf-8")
            regulation_page = (
                workspace / "wiki" / "regulations" / "dispensing-rule.md"
            ).read_text(encoding="utf-8")
            procedure_page = (
                workspace / "wiki" / "procedures" / "dispense-flow.md"
            ).read_text(encoding="utf-8")
            conflict_page = (
                workspace / "wiki" / "conflicts" / "policy-mismatch.md"
            ).read_text(encoding="utf-8")

            self.assertIn("dosage verification and final dispensing checks", topic_page)
            self.assertIn("Pharmacy staff must double-check dosage", regulation_page)
            self.assertIn("Verify prescription details and dosage.", procedure_page)
            self.assertIn("current medication-safety policy", conflict_page)


class BrainServiceMilestoneATest(unittest.TestCase):
    """Validate brain-service direct integration with compiler API."""

    def test_workspace_ingest_and_job_endpoints(self) -> None:
        """Create workspace, ingest file, enqueue compile, and read queued job."""
        memory_keyring = _MemoryKeyring()
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

                with (
                    patch(
                        "evidence_compiler.credentials.keyring.get_password",
                        side_effect=memory_keyring.get_password,
                    ),
                    patch(
                        "evidence_compiler.credentials.keyring.set_password",
                        side_effect=memory_keyring.set_password,
                    ),
                    patch(
                        "evidence_compiler.credentials.keyring.delete_password",
                        side_effect=memory_keyring.delete_password,
                    ),
                ):
                    configured = client.put(
                        f"/workspaces/{workspace_id}/credentials",
                        json={
                            "provider": "openai",
                            "model": "gpt-5.4-mini",
                            "api_key": "test-key",
                        },
                    )
                    self.assertEqual(configured.status_code, 200)

                    with (
                        patch(
                            "evidence_compiler.compiler.pipeline.litellm.completion",
                            side_effect=_fake_llm_completion,
                        ),
                        patch(
                            "evidence_compiler.compiler.pipeline.litellm.acompletion",
                            side_effect=_fake_llm_acompletion,
                        ),
                    ):
                        queued = client.post(
                            "/jobs/compile", json={"workspace": workspace_id}
                        )
                        self.assertEqual(queued.status_code, 200)
                        queue_payload = queued.json()
                        job_id = queue_payload["job_id"]

                        final_status = "queued"
                        for _ in range(40):
                            job = client.get(
                                f"/jobs/{job_id}", params={"workspace": workspace_id}
                            )
                            self.assertEqual(job.status_code, 200)
                            final_status = job.json()["status"]
                            if final_status in {"completed", "failed"}:
                                break
                            time.sleep(0.05)

                        self.assertEqual(final_status, "completed")
            finally:
                if previous is None:
                    os.environ.pop("EVIDENCE_BRAIN_WORKSPACES_DIR", None)
                else:
                    os.environ["EVIDENCE_BRAIN_WORKSPACES_DIR"] = previous


if __name__ == "__main__":
    unittest.main()
