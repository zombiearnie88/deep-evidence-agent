"""Milestone A integration smoke tests.

These tests validate the minimum skeleton + ingestion flow through both the
compiler library API and the brain-service HTTP layer.
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from evidence_compiler.compiler import pipeline as compiler_pipeline
from evidence_compiler.api import (
    add_path,
    compile_workspace,
    set_workspace_credentials,
    get_status,
    init_workspace,
)
from evidence_compiler.state import HashRegistry, JobStore
from evidence_compiler.watcher import start_file_watcher


def _fake_llm_payload(*args, **kwargs) -> dict[str, object]:
    messages = kwargs.get("messages") or []
    message_text = "\n\n".join(
        str(message.get("content") or "") for message in messages if isinstance(message, dict)
    )
    evidence_ids = sorted(set(re.findall(r"evidence:[A-Za-z0-9:-]+", message_text)))

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
            "summary_markdown": (
                "## Overview\n"
                "This summary captures baseline dispensing controls for local workflows.\n\n"
                "## Key points\n"
                "- Verify dosage before dispensing.\n"
                "- Record a final safety check.\n\n"
                "## Workflow\n"
                "Pharmacy staff follow a workflow with step 1 verification and step 2 dispensing.\n\n"
                "## Conflict note\n"
                "A legacy SOP conflicts with the current medication-safety policy."
            ),
        }
    elif schema_name in {"evidence_plan_actions", "EvidencePlanActions"}:
        payload = {
            "create": [
                {
                    "page_slug": "dose-must-be-checked",
                    "claim": "Dose must be checked",
                    "title": "Dose Verification: Mandatory before dispensing",
                    "brief": "Mandatory verification requirement before dispensing.",
                }
            ],
            "update": [],
        }
    elif schema_name in {"evidence_draft_output", "EvidenceDraftOutput"}:
        payload = {
            "claim": "Dose must be checked",
            "title": "Dose Verification: Mandatory before dispensing",
            "brief": "Mandatory verification requirement before dispensing.",
            "quotes": [
                {
                    "quote": "Dose verification is mandatory",
                    "anchor": "Rule",
                    "page_ref": "",
                },
                {
                    "quote": "Unverifiable quote that should be dropped",
                    "anchor": "Rule",
                    "page_ref": "",
                },
            ],
        }
    elif schema_name in {"taxonomy_plan_result", "TaxonomyPlanResult"}:
        payload = {
            "topics": {
                "create": [
                    {
                        "slug": "medication-safety",
                        "title": "Medication Safety",
                        "brief": "Safe dispensing checks",
                        "candidate_evidence_ids": evidence_ids[:1],
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
                        "candidate_evidence_ids": evidence_ids[:1],
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
                        "candidate_evidence_ids": evidence_ids[:1],
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
                        "candidate_evidence_ids": evidence_ids[:1],
                    }
                ],
                "update": [],
                "related": [],
            },
        }
    elif schema_name in {"topic_page_output", "TopicPageOutput"}:
        payload = {
            "title": "Medication Safety",
            "brief": "Safe dispensing checks",
            "context_markdown": (
                "Medication safety explains why dosage verification and final "
                "dispensing checks matter before medication reaches the patient."
            ),
            "used_evidence_ids": evidence_ids[:1],
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
            "used_evidence_ids": evidence_ids[:1],
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
            "used_evidence_ids": evidence_ids[:1],
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
            "used_evidence_ids": evidence_ids[:1],
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
        return {
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

    return payload


def _build_fake_response(payload: dict[str, object], usage: dict[str, int] | None):
    class _Message:
        content = json.dumps(payload)

    class _Choice:
        message = _Message()

    class _Response:
        choices = [_Choice()]

    response = _Response()
    if usage is not None:
        response.usage = usage
    return response


def _fake_llm_completion(*args, **kwargs):
    return _build_fake_response(
        _fake_llm_payload(*args, **kwargs),
        {"prompt_tokens": 120, "completion_tokens": 60, "total_tokens": 180},
    )


def _fake_llm_completion_no_usage(*args, **kwargs):
    return _build_fake_response(_fake_llm_payload(*args, **kwargs), None)


async def _fake_llm_acompletion(*args, **kwargs):
    return _fake_llm_completion(*args, **kwargs)


async def _fake_llm_acompletion_no_usage(*args, **kwargs):
    return _fake_llm_completion_no_usage(*args, **kwargs)


class _MemoryKeyring:
    def __init__(self) -> None:
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, account: str) -> str | None:
        return self._store.get((service, account))

    def set_password(self, service: str, account: str, password: str) -> None:
        self._store[(service, account)] = password

    def delete_password(self, service: str, account: str) -> None:
        self._store.pop((service, account), None)


def _wait_until(predicate, timeout: float = 5.0, interval: float = 0.05) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError("condition was not met before timeout")


class MarkdownRenderFormattingTest(unittest.TestCase):
    """Validate paragraph-only markdown reflow behavior."""

    def test_reflow_wraps_paragraphs_but_preserves_structured_blocks(self) -> None:
        """Keep lists/tables/code blocks unchanged while wrapping prose lines."""
        paragraph = (
            "This paragraph is intentionally very long so we can verify that the "
            "markdown reflow helper wraps prose lines to the configured width while "
            "keeping semantic content unchanged across line boundaries."
        )
        bullet = (
            "- Bullet entries should stay untouched even when the line is very long "
            "and exceeds the wrap width by a large margin for readability checks."
        )
        markdown = (
            f"{paragraph}\n\n"
            "## Structured Section\n"
            f"{bullet}\n"
            "| Col A | Col B |\n"
            "| --- | --- |\n"
            "| x | y |\n\n"
            "```txt\n"
            f"{paragraph}\n"
            "```\n"
        )

        formatted = compiler_pipeline._reflow_markdown_paragraphs(markdown, width=48)

        self.assertIn(bullet, formatted)
        self.assertIn("| Col A | Col B |", formatted)
        self.assertIn("```txt", formatted)

        in_fence = False
        for line in formatted.splitlines():
            stripped = line.strip()
            if stripped.startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence or not stripped:
                continue
            if stripped.startswith("#") or stripped.startswith("-") or "|" in stripped:
                continue
            self.assertLessEqual(len(line), 48)

    def test_reflow_repairs_inline_headings_and_list_items(self) -> None:
        """Single-line pseudo-markdown should be normalized into real headings and lists."""
        markdown = (
            "## Uses for Amoxicillin/Clavulanate  ### Otitis Media  "
            "Treatment of acute otitis media caused by beta-lactamase producers.  "
            "- Use for severe AOM.  - Avoid when the pathogen is not susceptible."
        )

        formatted = compiler_pipeline._reflow_markdown_paragraphs(markdown, width=72)

        self.assertIn("## Uses for Amoxicillin/Clavulanate", formatted)
        self.assertIn("\n\n### Otitis Media\n\n", formatted)
        self.assertIn(
            "Treatment of acute otitis media caused by beta-lactamase producers.",
            formatted,
        )
        self.assertIn("\n- Use for severe AOM.\n", formatted)
        self.assertIn("\n- Avoid when the pathogen is not susceptible.", formatted)
        self.assertNotIn("### Otitis Media  Treatment", formatted)
        self.assertNotIn("producers.  - Use", formatted)


class TaxonomyPlannerPostprocessTest(unittest.TestCase):
    """Validate planner post-processing and anti-overcreation safeguards."""

    def _materialized(self, name: str) -> compiler_pipeline._MaterializedDocument:
        document = compiler_pipeline.DocumentRecord(
            doc_id="doc-1",
            name=name,
            file_hash="hash-1",
            file_type="md",
            raw_path=Path("/tmp/source.md"),
            source_path=Path("/tmp/wiki/source/source.md"),
            is_long_doc=False,
            requires_pageindex=False,
            page_count=None,
            status="ready",
            created_at="2026-04-24T00:00:00Z",
        )
        return compiler_pipeline._MaterializedDocument(
            document=document,
            summary_slug="test-summary",
            source_ref="wiki/sources/test-source.md",
            text_for_summary="",
            text_for_downstream="# Rule\n\nDose verification is mandatory.\n",
            downstream_source_ref="wiki/sources/test-source.md",
        )

    def test_taxonomy_planner_messages_use_stable_assistant_prefix(self) -> None:
        """Taxonomy planning should use assistant-held source and summary context."""
        summary = compiler_pipeline.SummaryStageResult(
            document_brief="Short planning brief.",
            summary_markdown="## Overview\nPlanner summary body.",
        )

        messages = compiler_pipeline._taxonomy_planner_messages(
            language="English",
            materialized=self._materialized("planner-source.md"),
            summary=summary,
            existing_briefs={
                "topics": {"drug-a": "Existing topic brief."},
                "regulations": {},
                "procedures": {},
                "conflicts": {},
            },
            document_evidence_briefs=[
                {
                    "evidence_id": "evidence:doc:1",
                    "title": "Dose Verification",
                    "claim": "Dose must be checked",
                    "brief": "Verification requirement.",
                    "quote": "Dose verification is mandatory",
                    "anchor": "Rule",
                    "page_ref": "",
                    "summary_link": "[[summaries/test-summary]]",
                    "source_ref": "wiki/sources/test-source.md",
                    "page_slug": "dose-must-be-checked",
                }
            ],
        )

        self.assertEqual(
            [message["role"] for message in messages],
            ["system", "assistant", "assistant", "assistant", "assistant", "assistant", "user"],
        )
        self.assertIn("Document name: planner-source.md", messages[1]["content"])
        self.assertIn("Dose verification is mandatory", messages[2]["content"])
        self.assertIn("Planner summary body.", messages[3]["content"])
        self.assertIn("drug-a", messages[4]["content"])
        self.assertIn("evidence:doc:1", messages[5]["content"])
        self.assertIn("candidate_evidence_ids", messages[6]["content"])

    def test_evidence_planner_messages_use_stable_assistant_prefix(self) -> None:
        """Evidence planning should also use assistant-held source and summary data."""
        summary = compiler_pipeline.SummaryStageResult(
            document_brief="Short planning brief.",
            summary_markdown="## Overview\nPlanner summary body.",
        )

        messages = compiler_pipeline._evidence_planner_messages(
            language="English",
            materialized=self._materialized("planner-source.md"),
            summary=summary,
            existing_evidence_briefs={
                "dose-must-be-checked": {
                    "page_slug": "dose-must-be-checked",
                    "claim_key": "dose-must-be-checked",
                    "claim": "Dose must be checked",
                    "title": "Dose Verification",
                    "brief": "Existing evidence brief.",
                }
            },
        )

        self.assertEqual(
            [message["role"] for message in messages],
            ["system", "assistant", "assistant", "assistant", "assistant", "user"],
        )
        self.assertIn("Dose verification is mandatory", messages[2]["content"])
        self.assertIn("Planner summary body.", messages[3]["content"])
        self.assertIn("dose-must-be-checked", messages[4]["content"])
        self.assertIn("page_slug", messages[5]["content"])

    def test_plan_taxonomy_uses_dedicated_max_tokens(self) -> None:
        """Taxonomy planning should use a bounded completion budget."""
        summary = compiler_pipeline.SummaryStageResult(
            document_brief="Short planning brief.",
            summary_markdown="## Overview\nPlanner summary body.",
        )
        existing_briefs = {
            "topics": {},
            "regulations": {},
            "procedures": {},
            "conflicts": {},
        }

        with patch(
            "evidence_compiler.compiler.pipeline._structured_completion"
        ) as structured_completion:
            structured_completion.return_value = compiler_pipeline.TaxonomyPlanResult()

            compiler_pipeline._plan_taxonomy(
                model="gpt-5.4-mini",
                language="English",
                materialized=self._materialized("planner-source.md"),
                summary=summary,
                existing_briefs=existing_briefs,
                document_evidence_briefs=[],
                document_evidence_ids=set(),
            )

        self.assertEqual(structured_completion.call_count, 1)
        self.assertEqual(
            structured_completion.call_args.kwargs["max_tokens"],
            compiler_pipeline._TAXONOMY_PLAN_MAX_TOKENS,
        )

    def test_reference_documents_keep_related_regulations_but_prune_overreach(
        self,
    ) -> None:
        """Monographs should keep relevant regulation links without creating other taxonomies."""
        summary = compiler_pipeline.SummaryStageResult(
            document_brief=(
                "Cefdinir monograph covering uses, administration, dosage, and guideline references."
            ),
            summary_markdown=(
                "## Overview\n"
                "This monograph summarizes cefdinir uses, administration, pediatric dosage, "
                "adult dosage, and special populations.\n\n"
                "## Guideline context\n"
                "IDSA guidelines do not recommend cefdinir as empiric monotherapy for acute "
                "sinusitis.\n"
                "AAP recommends amoxicillin first-line for acute otitis media."
            ),
        )
        plan = compiler_pipeline.TaxonomyPlanResult(
            topics=compiler_pipeline.PagePlanActions(
                create=[
                    compiler_pipeline.PagePlanItem(
                        slug="cefdinir",
                        title="Cefdinir",
                        brief="Drug use overview.",
                    )
                ]
            ),
            regulations=compiler_pipeline.PagePlanActions(
                create=[
                    compiler_pipeline.PagePlanItem(
                        slug="idsa-sinusitis-guidelines",
                        title="IDSA Acute Sinusitis Guidelines",
                        brief="Cefdinir is not recommended as empiric monotherapy.",
                        candidate_evidence_ids=["evidence:doc:1", "evidence:doc:missing"],
                    )
                ],
                update=[
                    compiler_pipeline.PagePlanItem(
                        slug="aap-otitis-media-guidelines",
                        title="AAP Otitis Media Guidelines",
                        brief="AAP prefers amoxicillin for initial therapy.",
                        candidate_evidence_ids=["evidence:doc:1"],
                    )
                ],
                related=[
                    "idsa-sinusitis-guidelines",
                    "aap-otitis-media-guidelines",
                    "missing-guideline",
                ],
            ),
            procedures=compiler_pipeline.PagePlanActions(
                create=[
                    compiler_pipeline.PagePlanItem(
                        slug="cefdinir-administration",
                        title="Cefdinir Administration",
                        brief="Administer cefdinir according to dosing guidance.",
                    )
                ]
            ),
            conflicts=compiler_pipeline.PagePlanActions(
                create=[
                    compiler_pipeline.PagePlanItem(
                        slug="cefdinir-hepatic-impairment",
                        title="Cefdinir in Hepatic Impairment",
                        brief=(
                            "No mismatch: source confirms no dosage adjustment required for hepatic impairment."
                        ),
                        candidate_evidence_ids=["evidence:doc:1"],
                    )
                ]
            ),
        )
        existing_briefs = {
            "topics": {},
            "regulations": {
                "idsa-sinusitis-guidelines": "Cefdinir is not recommended as empiric monotherapy.",
                "aap-otitis-media-guidelines": "AAP prefers amoxicillin for initial therapy.",
            },
            "procedures": {},
            "conflicts": {},
        }

        finalized = compiler_pipeline._finalize_taxonomy_plan(
            plan,
            materialized=self._materialized("cefdinir-monograph.md"),
            summary=summary,
            existing_briefs=existing_briefs,
            document_evidence_ids={"evidence:doc:1"},
        )

        self.assertEqual([item.slug for item in finalized.topics.create], ["cefdinir"])
        self.assertEqual(finalized.regulations.create, [])
        self.assertEqual(finalized.regulations.update, [])
        self.assertEqual(
            finalized.regulations.related,
            ["aap-otitis-media-guidelines", "idsa-sinusitis-guidelines"],
        )
        self.assertEqual(finalized.procedures, compiler_pipeline.PagePlanActions())
        self.assertEqual(finalized.conflicts, compiler_pipeline.PagePlanActions())
        self.assertEqual(
            finalized.regulations.related,
            ["aap-otitis-media-guidelines", "idsa-sinusitis-guidelines"],
        )

    def test_finalize_plan_reconciles_create_update_and_related(self) -> None:
        """Planner actions should be normalized against existing slugs deterministically."""
        summary = compiler_pipeline.SummaryStageResult(
            document_brief="Clinic policy for infusion center medication handling.",
            summary_markdown=(
                "## Overview\n"
                "This policy defines durable handling rules for infusion medications.\n\n"
                "## Workflow\n"
                "Pharmacy staff follow a workflow with step 1 intake and step 2 verification."
            ),
        )
        plan = compiler_pipeline.TaxonomyPlanResult(
            topics=compiler_pipeline.PagePlanActions(
                create=[
                    compiler_pipeline.PagePlanItem(
                        slug="existing-topic",
                        title="Existing Topic",
                        brief="Should move to update because the slug already exists.",
                        candidate_evidence_ids=["evidence:doc:1", "evidence:doc:missing"],
                    )
                ],
                update=[
                    compiler_pipeline.PagePlanItem(
                        slug="new-topic",
                        title="New Topic",
                        brief="Should move to create because the slug is new.",
                        candidate_evidence_ids=["evidence:doc:1"],
                    )
                ],
                related=["existing-topic", "new-topic", "missing-topic"],
            ),
        )
        existing_briefs = {
            "topics": {"existing-topic": "Current topic brief."},
            "regulations": {},
            "procedures": {},
            "conflicts": {},
        }

        finalized = compiler_pipeline._finalize_taxonomy_plan(
            plan,
            materialized=self._materialized("clinic-policy.md"),
            summary=summary,
            existing_briefs=existing_briefs,
            document_evidence_ids={"evidence:doc:1"},
        )

        self.assertEqual([item.slug for item in finalized.topics.create], ["new-topic"])
        self.assertEqual(
            [item.slug for item in finalized.topics.update], ["existing-topic"]
        )
        self.assertEqual(finalized.topics.related, [])
        self.assertEqual(
            finalized.topics.create[0].candidate_evidence_ids, ["evidence:doc:1"]
        )
        self.assertEqual(
            finalized.topics.update[0].candidate_evidence_ids, ["evidence:doc:1"]
        )

    def test_finalize_evidence_plan_reuses_existing_page_slug(self) -> None:
        """Evidence planning should normalize claims and reuse existing page identity."""
        plan = compiler_pipeline.EvidencePlanActions(
            create=[
                compiler_pipeline.EvidencePlanItem(
                    page_slug="",
                    claim=" Stable handling rule ",
                    title="Stable Handling Rule",
                    brief=" Verify before dispensing. ",
                )
            ],
            update=[
                compiler_pipeline.EvidencePlanItem(
                    page_slug="",
                    claim=" Dose must be checked ",
                    title="Dose Verification",
                    brief=" Double-check before dispensing. ",
                )
            ],
        )
        existing_pages = {
            "dose-must-be-checked": compiler_pipeline._EvidencePageState(
                page_slug="dose-must-be-checked",
                claim_key="dose-must-be-checked",
                canonical_claim="Dose must be checked",
                title="Dose Verification",
                brief="Existing evidence brief.",
            )
        }

        finalized = compiler_pipeline._finalize_evidence_plan(
            plan, existing_pages=existing_pages
        )

        self.assertEqual(
            [item.page_slug for item in finalized.create], ["stable-handling-rule"]
        )
        self.assertEqual(
            [item.page_slug for item in finalized.update], ["dose-must-be-checked"]
        )
        self.assertEqual(finalized.update[0].claim, "Dose must be checked")
        self.assertEqual(finalized.create[0].brief, "Verify before dispensing.")


class EvidenceVerificationTest(unittest.TestCase):
    """Validate manifest-ready evidence verification behavior."""

    def test_verify_evidence_output_drops_unverifiable_quote(self) -> None:
        document = compiler_pipeline.DocumentRecord(
            doc_id="doc-1",
            name="policy.md",
            file_hash="hash-1",
            file_type="md",
            raw_path=Path("/tmp/source.md"),
            source_path=Path("/tmp/wiki/source/source.md"),
            is_long_doc=False,
            requires_pageindex=False,
            page_count=None,
            status="ready",
            created_at="2026-04-24T00:00:00Z",
        )
        materialized = compiler_pipeline._MaterializedDocument(
            document=document,
            summary_slug="policy-summary",
            source_ref="wiki/sources/policy.md",
            text_for_summary="# Rule\n\nDose verification is mandatory.\n",
            text_for_downstream="# Rule\n\nDose verification is mandatory.\n",
            downstream_source_ref="wiki/sources/policy.md",
        )
        plan_item = compiler_pipeline.EvidencePlanItem(
            page_slug="dose-must-be-checked",
            claim="Dose must be checked",
            title="Dose Verification",
            brief="Mandatory verification.",
        )
        draft = compiler_pipeline.EvidenceDraftOutput(
            claim="Dose must be checked",
            title="Dose Verification",
            brief="Mandatory verification.",
            quotes=[
                compiler_pipeline.EvidenceDraftQuote(
                    quote="Dose verification is mandatory",
                    anchor="Rule",
                ),
                compiler_pipeline.EvidenceDraftQuote(
                    quote="This sentence is not in the source",
                    anchor="Rule",
                ),
            ],
        )

        verified, dropped = compiler_pipeline._verify_evidence_output(
            materialized=materialized,
            plan_item=plan_item,
            draft=draft,
        )

        self.assertEqual(len(verified), 1)
        self.assertEqual(len(dropped), 1)
        self.assertEqual(verified[0].anchor, "Rule")
        self.assertEqual(dropped[0].reason, "quote not found in source text")


class PageDraftPromptTest(unittest.TestCase):
    """Validate shared drafting prompt structure and page-type rules."""

    def _summary(self) -> compiler_pipeline.SummaryStageResult:
        return compiler_pipeline.SummaryStageResult(
            document_brief="Short drafting brief.",
            summary_markdown="## Overview\nDrafting summary body.",
        )

    def _materialized(self) -> compiler_pipeline._MaterializedDocument:
        document = compiler_pipeline.DocumentRecord(
            doc_id="doc-1",
            name="draft-source.md",
            file_hash="hash-1",
            file_type="md",
            raw_path=Path("/tmp/source.md"),
            source_path=Path("/tmp/wiki/source/source.md"),
            is_long_doc=False,
            requires_pageindex=False,
            page_count=None,
            status="ready",
            created_at="2026-04-24T00:00:00Z",
        )
        return compiler_pipeline._MaterializedDocument(
            document=document,
            summary_slug="draft-summary",
            source_ref="wiki/sources/draft-source.md",
            text_for_summary="# Rule\n\nDose verification is mandatory.\n",
            text_for_downstream="# Rule\n\nDose verification is mandatory.\n",
            downstream_source_ref="wiki/sources/draft-source.md",
        )

    def _item(self) -> compiler_pipeline.PagePlanItem:
        return compiler_pipeline.PagePlanItem(
            slug="draft-target",
            title="Draft Target",
            brief="Planner-provided brief.",
            candidate_evidence_ids=["evidence:doc:1"],
        )

    def _evidence_pack(self) -> list[compiler_pipeline.VerifiedEvidenceInstance]:
        return [
            compiler_pipeline.VerifiedEvidenceInstance(
                evidence_id="evidence:doc:1",
                page_slug="dose-must-be-checked",
                claim_key="dose-must-be-checked",
                canonical_claim="Dose must be checked",
                title="Dose Verification",
                brief="Mandatory verification.",
                quote="Dose verification is mandatory",
                anchor="Rule",
                page_ref="",
                source_ref="wiki/sources/draft-source.md",
                summary_link="[[summaries/draft-summary]]",
                document_hash="hash-1",
            )
        ]

    def test_page_draft_messages_use_stable_assistant_prefix(self) -> None:
        """Draft prompts should carry source, summary, and evidence in assistant turns."""
        messages = compiler_pipeline._page_draft_messages(
            language="English",
            page_type="topic",
            materialized=self._materialized(),
            summary=self._summary(),
            item=self._item(),
            evidence_pack=self._evidence_pack(),
            is_update=False,
            existing_body="",
            body_guidance=compiler_pipeline._TOPIC_BODY_GUIDANCE,
            type_rules=compiler_pipeline._TOPIC_TYPE_RULES,
            field_guide=compiler_pipeline._TOPIC_FIELD_GUIDE,
        )

        self.assertEqual(
            [message["role"] for message in messages],
            ["system", "assistant", "assistant", "assistant", "assistant", "assistant", "user"],
        )
        self.assertIn("Document name: draft-source.md", messages[1]["content"])
        self.assertIn("Dose verification is mandatory", messages[2]["content"])
        self.assertIn("Drafting summary body.", messages[3]["content"])
        self.assertIn("candidate_evidence_ids", messages[4]["content"])
        self.assertIn("evidence:doc:1", messages[5]["content"])
        self.assertIn("used_evidence_ids", messages[6]["content"])

    def test_page_draft_update_includes_existing_body_in_assistant_context(self) -> None:
        """Rewrite prompts should keep existing body context in an assistant block."""
        messages = compiler_pipeline._page_draft_messages(
            language="English",
            page_type="conflict",
            materialized=self._materialized(),
            summary=self._summary(),
            item=self._item(),
            evidence_pack=self._evidence_pack(),
            is_update=True,
            existing_body="## Existing\nCurrent body text.",
            body_guidance=compiler_pipeline._CONFLICT_BODY_GUIDANCE,
            type_rules=compiler_pipeline._CONFLICT_TYPE_RULES,
            field_guide=compiler_pipeline._CONFLICT_FIELD_GUIDE,
        )

        self.assertIn("Existing body for rewrite context only", messages[6]["content"])
        self.assertIn("## Existing\nCurrent body text.", messages[6]["content"])

    def test_procedure_prompt_omits_markdown_body_rules(self) -> None:
        """Procedure prompts should not include markdown-body-only rules."""
        procedure_messages = compiler_pipeline._page_draft_messages(
            language="English",
            page_type="procedure",
            materialized=self._materialized(),
            summary=self._summary(),
            item=self._item(),
            evidence_pack=self._evidence_pack(),
            is_update=False,
            existing_body="",
            body_guidance=compiler_pipeline._PROCEDURE_BODY_GUIDANCE,
            type_rules=compiler_pipeline._PROCEDURE_TYPE_RULES,
            field_guide=compiler_pipeline._PROCEDURE_FIELD_GUIDE,
        )
        topic_messages = compiler_pipeline._page_draft_messages(
            language="English",
            page_type="topic",
            materialized=self._materialized(),
            summary=self._summary(),
            item=self._item(),
            evidence_pack=self._evidence_pack(),
            is_update=False,
            existing_body="",
            body_guidance=compiler_pipeline._TOPIC_BODY_GUIDANCE,
            type_rules=compiler_pipeline._TOPIC_TYPE_RULES,
            field_guide=compiler_pipeline._TOPIC_FIELD_GUIDE,
        )

        self.assertNotIn("Markdown body rules:", procedure_messages[-1]["content"])
        self.assertIn("Markdown body rules:", topic_messages[-1]["content"])

    def test_page_type_body_guidance_is_specific(self) -> None:
        """Each page type should carry narrowly-scoped drafting guidance."""
        topic_messages = compiler_pipeline._page_draft_messages(
            language="English",
            page_type="topic",
            materialized=self._materialized(),
            summary=self._summary(),
            item=self._item(),
            evidence_pack=self._evidence_pack(),
            is_update=False,
            existing_body="",
            body_guidance=compiler_pipeline._TOPIC_BODY_GUIDANCE,
            type_rules=compiler_pipeline._TOPIC_TYPE_RULES,
            field_guide=compiler_pipeline._TOPIC_FIELD_GUIDE,
        )
        regulation_messages = compiler_pipeline._page_draft_messages(
            language="English",
            page_type="regulation",
            materialized=self._materialized(),
            summary=self._summary(),
            item=self._item(),
            evidence_pack=self._evidence_pack(),
            is_update=False,
            existing_body="",
            body_guidance=compiler_pipeline._REGULATION_BODY_GUIDANCE,
            type_rules=compiler_pipeline._REGULATION_TYPE_RULES,
            field_guide=compiler_pipeline._REGULATION_FIELD_GUIDE,
        )
        procedure_messages = compiler_pipeline._page_draft_messages(
            language="English",
            page_type="procedure",
            materialized=self._materialized(),
            summary=self._summary(),
            item=self._item(),
            evidence_pack=self._evidence_pack(),
            is_update=False,
            existing_body="",
            body_guidance=compiler_pipeline._PROCEDURE_BODY_GUIDANCE,
            type_rules=compiler_pipeline._PROCEDURE_TYPE_RULES,
            field_guide=compiler_pipeline._PROCEDURE_FIELD_GUIDE,
        )
        conflict_messages = compiler_pipeline._page_draft_messages(
            language="English",
            page_type="conflict",
            materialized=self._materialized(),
            summary=self._summary(),
            item=self._item(),
            evidence_pack=self._evidence_pack(),
            is_update=False,
            existing_body="",
            body_guidance=compiler_pipeline._CONFLICT_BODY_GUIDANCE,
            type_rules=compiler_pipeline._CONFLICT_TYPE_RULES,
            field_guide=compiler_pipeline._CONFLICT_FIELD_GUIDE,
        )

        self.assertIn(
            "Do not turn the page into a regulation, procedure, or conflict record.",
            topic_messages[-1]["content"],
        )
        self.assertIn(
            "requirement_markdown should state the binding recommendation, rule, or restriction",
            regulation_messages[-1]["content"],
        )
        self.assertIn(
            "Return 3-7 concise imperative steps as plain step strings",
            procedure_messages[-1]["content"],
        )
        self.assertIn(
            "Do not frame uncertainty, lack of evidence, or 'no conflict' as a conflict.",
            conflict_messages[-1]["content"],
        )

    def test_page_type_rules_are_specific(self) -> None:
        """Each page type should include its own semantic constraint block."""
        topic_messages = compiler_pipeline._page_draft_messages(
            language="English",
            page_type="topic",
            materialized=self._materialized(),
            summary=self._summary(),
            item=self._item(),
            evidence_pack=self._evidence_pack(),
            is_update=False,
            existing_body="",
            body_guidance=compiler_pipeline._TOPIC_BODY_GUIDANCE,
            type_rules=compiler_pipeline._TOPIC_TYPE_RULES,
            field_guide=compiler_pipeline._TOPIC_FIELD_GUIDE,
        )
        regulation_messages = compiler_pipeline._page_draft_messages(
            language="English",
            page_type="regulation",
            materialized=self._materialized(),
            summary=self._summary(),
            item=self._item(),
            evidence_pack=self._evidence_pack(),
            is_update=False,
            existing_body="",
            body_guidance=compiler_pipeline._REGULATION_BODY_GUIDANCE,
            type_rules=compiler_pipeline._REGULATION_TYPE_RULES,
            field_guide=compiler_pipeline._REGULATION_FIELD_GUIDE,
        )
        procedure_messages = compiler_pipeline._page_draft_messages(
            language="English",
            page_type="procedure",
            materialized=self._materialized(),
            summary=self._summary(),
            item=self._item(),
            evidence_pack=self._evidence_pack(),
            is_update=False,
            existing_body="",
            body_guidance=compiler_pipeline._PROCEDURE_BODY_GUIDANCE,
            type_rules=compiler_pipeline._PROCEDURE_TYPE_RULES,
            field_guide=compiler_pipeline._PROCEDURE_FIELD_GUIDE,
        )
        conflict_messages = compiler_pipeline._page_draft_messages(
            language="English",
            page_type="conflict",
            materialized=self._materialized(),
            summary=self._summary(),
            item=self._item(),
            evidence_pack=self._evidence_pack(),
            is_update=False,
            existing_body="",
            body_guidance=compiler_pipeline._CONFLICT_BODY_GUIDANCE,
            type_rules=compiler_pipeline._CONFLICT_TYPE_RULES,
            field_guide=compiler_pipeline._CONFLICT_FIELD_GUIDE,
        )

        self.assertIn("Type-specific rules for topic:", topic_messages[-1]["content"])
        self.assertIn(
            "Do not write operational steps, checklists, or workflow instructions.",
            topic_messages[-1]["content"],
        )
        self.assertIn(
            "Type-specific rules for regulation:",
            regulation_messages[-1]["content"],
        )
        self.assertIn(
            "Do not repeat the same sentence across requirement_markdown, applicability_markdown, and authority_markdown.",
            regulation_messages[-1]["content"],
        )
        self.assertIn(
            "Type-specific rules for procedure:",
            procedure_messages[-1]["content"],
        )
        self.assertIn(
            "Do not embed numbering, bullets, or markdown formatting inside step strings.",
            procedure_messages[-1]["content"],
        )
        self.assertIn("Type-specific rules for conflict:", conflict_messages[-1]["content"])
        self.assertIn(
            "Never write a 'no conflict' or 'resolved without mismatch' conflict page.",
            conflict_messages[-1]["content"],
        )

    def test_page_type_field_guides_are_specific(self) -> None:
        """Each page type should expose a narrow field contract."""
        topic_messages = compiler_pipeline._page_draft_messages(
            language="English",
            page_type="topic",
            materialized=self._materialized(),
            summary=self._summary(),
            item=self._item(),
            evidence_pack=self._evidence_pack(),
            is_update=False,
            existing_body="",
            body_guidance=compiler_pipeline._TOPIC_BODY_GUIDANCE,
            type_rules=compiler_pipeline._TOPIC_TYPE_RULES,
            field_guide=compiler_pipeline._TOPIC_FIELD_GUIDE,
        )
        regulation_messages = compiler_pipeline._page_draft_messages(
            language="English",
            page_type="regulation",
            materialized=self._materialized(),
            summary=self._summary(),
            item=self._item(),
            evidence_pack=self._evidence_pack(),
            is_update=False,
            existing_body="",
            body_guidance=compiler_pipeline._REGULATION_BODY_GUIDANCE,
            type_rules=compiler_pipeline._REGULATION_TYPE_RULES,
            field_guide=compiler_pipeline._REGULATION_FIELD_GUIDE,
        )
        procedure_messages = compiler_pipeline._page_draft_messages(
            language="English",
            page_type="procedure",
            materialized=self._materialized(),
            summary=self._summary(),
            item=self._item(),
            evidence_pack=self._evidence_pack(),
            is_update=False,
            existing_body="",
            body_guidance=compiler_pipeline._PROCEDURE_BODY_GUIDANCE,
            type_rules=compiler_pipeline._PROCEDURE_TYPE_RULES,
            field_guide=compiler_pipeline._PROCEDURE_FIELD_GUIDE,
        )
        conflict_messages = compiler_pipeline._page_draft_messages(
            language="English",
            page_type="conflict",
            materialized=self._materialized(),
            summary=self._summary(),
            item=self._item(),
            evidence_pack=self._evidence_pack(),
            is_update=False,
            existing_body="",
            body_guidance=compiler_pipeline._CONFLICT_BODY_GUIDANCE,
            type_rules=compiler_pipeline._CONFLICT_TYPE_RULES,
            field_guide=compiler_pipeline._CONFLICT_FIELD_GUIDE,
        )

        self.assertIn(
            "brief: one sentence under 180 chars defining the stable subject",
            topic_messages[-1]["content"],
        )
        self.assertIn(
            "requirement_markdown: normative requirement details only; do not write operational steps",
            regulation_messages[-1]["content"],
        )
        self.assertIn(
            "steps: 3-7 concise imperative action strings in execution order; no numbering, bullets, or extra commentary",
            procedure_messages[-1]["content"],
        )
        self.assertIn(
            "impacted_pages: explicit wiki links such as [[regulations/foo]] only when supported by context; otherwise []",
            conflict_messages[-1]["content"],
        )
        self.assertIn("used_evidence_ids", topic_messages[-1]["content"])


class DownstreamSourceContextTest(unittest.TestCase):
    """Validate downstream prompts prefer source plus summary context."""

    def test_taxonomy_planner_uses_downstream_source_not_summary_seed(self) -> None:
        """Long-doc downstream prompts should include the downstream source artifact text."""
        document = compiler_pipeline.DocumentRecord(
            doc_id="doc-1",
            name="long-doc.pdf",
            file_hash="hash-1",
            file_type="pdf",
            raw_path=Path("/tmp/source.pdf"),
            source_path=None,
            is_long_doc=True,
            requires_pageindex=True,
            page_count=30,
            status="ready",
            created_at="2026-04-24T00:00:00Z",
        )
        materialized = compiler_pipeline._MaterializedDocument(
            document=document,
            summary_slug="long-doc-summary",
            source_ref="wiki/sources/long-doc-pageindex.md",
            text_for_summary="Summary seed only.",
            text_for_downstream="### Page 7\nUnique downstream source phrase.",
            downstream_source_ref="wiki/sources/long-doc-pageindex.md",
            summary_seed_ref="wiki/sources/long-doc-summary-seed.md",
        )
        summary = compiler_pipeline.SummaryStageResult(
            document_brief="Long document brief.",
            summary_markdown="## Overview\nShort summary text.",
        )

        messages = compiler_pipeline._taxonomy_planner_messages(
            language="English",
            materialized=materialized,
            summary=summary,
            existing_briefs={
                "topics": {},
                "regulations": {},
                "procedures": {},
                "conflicts": {},
            },
            document_evidence_briefs=[],
        )

        self.assertIn("Unique downstream source phrase.", messages[2]["content"])
        self.assertNotIn("Summary seed only.", messages[2]["content"])


class CompilerMilestoneATest(unittest.TestCase):
    """Validate compiler workspace/job scaffolding for Milestone A."""

    def test_init_add_status_compile_flow(self) -> None:
        """Initialize workspace, ingest one doc, inspect status, and queue compile."""
        memory_keyring = _MemoryKeyring()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "workspace"
            source = root / "regulation.md"
            source.write_text(
                "# Rule\n\n"
                "Dose verification is mandatory.\n\n"
                "Verify dosage before dispensing.\n"
                "Record a final safety check.\n\n"
                "Pharmacy staff follow a workflow with step 1 verification and step 2 dispensing.\n\n"
                "A legacy SOP conflicts with the current medication-safety policy.\n",
                encoding="utf-8",
            )

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
            assert compile_result.job_id is not None

            jobs = JobStore(workspace / ".brain" / "jobs")
            compile_job = jobs.read(compile_result.job_id)
            self.assertEqual(compile_job.status, "completed")
            self.assertIsNotNone(compile_job.compile)
            assert compile_job.compile is not None
            self.assertIsNotNone(compile_job.compile.plan)
            assert compile_job.compile.plan is not None
            self.assertEqual(compile_job.compile.plan.evidence_count, 1)
            self.assertGreater(compile_job.compile.usage_total.total_tokens, 0)
            self.assertTrue(compile_job.compile.usage_total.available)
            self.assertTrue(compile_job.compile.usage_by_stage)
            self.assertIn("planning-evidence", compile_job.compile.usage_by_stage)
            self.assertIn("drafting-evidence", compile_job.compile.usage_by_stage)
            self.assertEqual(
                compile_job.compile.usage_total.total_tokens,
                sum(
                    usage.total_tokens
                    for usage in compile_job.compile.usage_by_stage.values()
                ),
            )

            summary_pages = list((workspace / "wiki" / "summaries").glob("*.md"))
            self.assertEqual(len(summary_pages), 1)
            summary_page = summary_pages[0].read_text(encoding="utf-8")

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
            evidence_page = (
                workspace / "wiki" / "evidence" / "dose-must-be-checked.md"
            ).read_text(encoding="utf-8")
            evidence_manifest = json.loads(
                (
                    workspace
                    / ".brain"
                    / "evidence"
                    / "by-document"
                    / f"{add_result.added_documents[0].file_hash}.json"
                ).read_text(encoding="utf-8")
            )
            evidence_validation_report = (
                workspace / "wiki" / "reports" / "evidence-validation.md"
            ).read_text(encoding="utf-8")

            topic_text = topic_page.replace("\n", " ")
            regulation_text = regulation_page.replace("\n", " ")
            procedure_text = procedure_page.replace("\n", " ")
            conflict_text = conflict_page.replace("\n", " ")

            self.assertIn("dosage verification and final dispensing checks", topic_text)
            self.assertIn("Pharmacy staff must double-check dosage", regulation_text)
            self.assertIn("Verify prescription details and dosage.", procedure_text)
            self.assertIn("current medication-safety policy", conflict_text)
            self.assertIn("## Overview", summary_page)
            self.assertIn("## Key points", summary_page)
            self.assertIn("- Verify dosage before dispensing.", summary_page)
            self.assertIn("[[evidence/dose-must-be-checked]]", summary_page)
            self.assertIn("## Related Evidence", topic_page)
            self.assertIn("[[evidence/dose-must-be-checked]]", topic_page)
            self.assertIn("## Related Evidence", regulation_page)
            self.assertIn("## Related Evidence", procedure_page)
            self.assertIn("## Related Evidence", conflict_page)
            self.assertIn("# Evidence: Dose Verification: Mandatory before dispensing", evidence_page)
            self.assertIn("Dose verification is mandatory", evidence_page)
            self.assertIn("- anchor: `Rule`", evidence_page)
            self.assertNotIn("Unverifiable quote that should be dropped", evidence_page)
            self.assertEqual(len(evidence_manifest["items"]), 1)
            self.assertEqual(evidence_manifest["items"][0]["page_slug"], "dose-must-be-checked")
            self.assertIn("quote not found in source text", evidence_validation_report)

    def test_compile_rerun_is_noop_when_no_new_docs(self) -> None:
        """Re-running compile without new documents should complete as a no-op."""
        memory_keyring = _MemoryKeyring()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "workspace"
            source = root / "regulation.md"
            source.write_text(
                "# Rule\n\n"
                "Dose verification is mandatory.\n\n"
                "Verify dosage before dispensing.\n"
                "Record a final safety check.\n",
                encoding="utf-8",
            )

            _ = init_workspace(workspace)
            _ = add_path(workspace, source)

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

                with patch(
                    "evidence_compiler.compiler.pipeline.litellm.completion",
                    side_effect=_fake_llm_completion,
                ) as completion_mock, patch(
                    "evidence_compiler.compiler.pipeline.litellm.acompletion",
                    side_effect=_fake_llm_acompletion,
                ) as acompletion_mock:
                    first = compile_workspace(workspace)
                    llm_calls_after_first = (
                        completion_mock.call_count,
                        acompletion_mock.call_count,
                    )
                    second = compile_workspace(workspace)

            self.assertEqual(first.processed_files, 1)
            self.assertEqual(second.processed_files, 0)
            self.assertEqual(
                (completion_mock.call_count, acompletion_mock.call_count),
                llm_calls_after_first,
            )
            evidence_pages = sorted((workspace / "wiki" / "evidence").glob("*.md"))
            self.assertEqual([page.name for page in evidence_pages], ["dose-must-be-checked.md"])
            assert second.job_id is not None
            jobs = JobStore(workspace / ".brain" / "jobs")
            second_job = jobs.read(second.job_id)
            self.assertEqual(second_job.status, "completed")
            self.assertEqual(second_job.payload["document_count"], 0)
            self.assertEqual(second_job.payload["document_hashes"], [])

    def test_incremental_compile_only_processes_new_documents(self) -> None:
        """Incremental compile should summarize only new documents and patch old summary backlinks."""
        memory_keyring = _MemoryKeyring()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "workspace"
            alpha_source = root / "alpha-policy.md"
            beta_source = root / "beta-policy.md"
            alpha_source.write_text(
                "# Alpha Policy\n\n"
                "Pharmacy staff follow a workflow with step 1 verification and step 2 release.\n"
                "The original medication verification flow is used before release.\n",
                encoding="utf-8",
            )
            beta_source.write_text(
                "# Beta Policy\n\n"
                "Teams must perform medication verification before release.\n"
                "This policy conflicts with the earlier workflow sequence.\n",
                encoding="utf-8",
            )

            _ = init_workspace(workspace)
            alpha_add = add_path(workspace, alpha_source)
            self.assertEqual(len(alpha_add.added_documents), 1)
            alpha_hash = alpha_add.added_documents[0].file_hash

            summary_calls: dict[str, int] = {}

            def _fake_incremental_summary(*, model, language, materialized, usage_callback=None):
                del model, language, usage_callback
                name = materialized.document.name
                call_number = summary_calls.get(name, 0) + 1
                summary_calls[name] = call_number
                if name == "alpha-policy.md":
                    summary_markdown = (
                        "## Overview\n"
                        f"Summary body for {name} call {call_number}.\n\n"
                        "Pharmacy staff follow a workflow with step 1 verification and "
                        "step 2 release."
                    )
                else:
                    summary_markdown = (
                        "## Overview\n"
                        f"Summary body for {name} call {call_number}.\n\n"
                        "Teams must perform medication verification before release.\n\n"
                        "This policy conflicts with the earlier workflow sequence."
                    )
                return compiler_pipeline.SummaryStageResult(
                    document_brief=f"Brief for {name} call {call_number}",
                    summary_markdown=summary_markdown,
                )

            def _incremental_completion(*args, **kwargs):
                messages = kwargs.get("messages") or []
                message_text = "\n\n".join(
                    str(message.get("content") or "")
                    for message in messages
                    if isinstance(message, dict)
                )
                response_format = kwargs.get("response_format")
                schema_name = ""
                if isinstance(response_format, type):
                    schema_name = response_format.__name__
                elif isinstance(response_format, dict):
                    schema = response_format.get("json_schema")
                    if isinstance(schema, dict):
                        schema_name = str(schema.get("name") or "")

                is_alpha = "alpha-policy.md" in message_text
                is_beta = "beta-policy.md" in message_text

                if schema_name in {"evidence_plan_actions", "EvidencePlanActions"}:
                    payload = {"create": [], "update": []}
                elif schema_name in {"evidence_draft_output", "EvidenceDraftOutput"}:
                    payload = {
                        "claim": "",
                        "title": "",
                        "brief": "",
                        "quotes": [],
                    }
                elif schema_name in {"taxonomy_plan_result", "TaxonomyPlanResult"}:
                    payload = {
                        "topics": {"create": [], "update": [], "related": []},
                        "regulations": {"create": [], "update": [], "related": []},
                        "procedures": {"create": [], "update": [], "related": []},
                        "conflicts": {"create": [], "update": [], "related": []},
                    }
                    if is_alpha:
                        payload["procedures"]["create"] = [
                            {
                                "slug": "medication-verification-flow",
                                "title": "Medication Verification Flow",
                                "brief": "Original verification workflow.",
                                "candidate_evidence_ids": [],
                            }
                        ]
                    elif is_beta:
                        payload["regulations"]["create"] = [
                            {
                                "slug": "medication-verification-rule",
                                "title": "Medication Verification Rule",
                                "brief": "Updated verification rule.",
                                "candidate_evidence_ids": [],
                            }
                        ]
                elif schema_name in {"procedure_page_output", "ProcedurePageOutput"}:
                    payload = {
                        "title": "Medication Verification Flow",
                        "brief": "Original verification workflow.",
                        "steps": [
                            "Verify the medication request.",
                            "Check the dose before dispensing.",
                            "Record the completed verification.",
                        ],
                        "used_evidence_ids": [],
                    }
                elif schema_name in {"regulation_page_output", "RegulationPageOutput"}:
                    payload = {
                        "title": "Medication Verification Rule",
                        "brief": "Updated verification rule.",
                        "requirement_markdown": (
                            "Teams must perform medication verification before the final release."
                        ),
                        "applicability_markdown": "Applies to medication dispensing workflows.",
                        "authority_markdown": "Derived from the updated beta policy summary.",
                        "used_evidence_ids": [],
                    }
                elif schema_name in {"conflict_check_result", "ConflictCheckResult"}:
                    payload = {
                        "is_conflict": True,
                        "title": "Medication verification mismatch",
                        "description": "The regulation and procedure disagree on the verification sequence.",
                    }
                else:
                    raise AssertionError(f"Unexpected schema in incremental test: {schema_name}")

                return _build_fake_response(
                    payload,
                    {"prompt_tokens": 80, "completion_tokens": 40, "total_tokens": 120},
                )

            async def _incremental_acompletion(*args, **kwargs):
                return _incremental_completion(*args, **kwargs)

            def _strip_derived_pages(text: str) -> str:
                return re.sub(
                    r"^## Derived Pages\n.*?(?=^## |\Z)",
                    "",
                    text,
                    flags=re.MULTILINE | re.DOTALL,
                ).strip()

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
                        "evidence_compiler.compiler.pipeline._summarize_document",
                        side_effect=_fake_incremental_summary,
                    ),
                    patch(
                        "evidence_compiler.compiler.pipeline.litellm.completion",
                        side_effect=_incremental_completion,
                    ),
                    patch(
                        "evidence_compiler.compiler.pipeline.litellm.acompletion",
                        side_effect=_incremental_acompletion,
                    ),
                ):
                    first = compile_workspace(workspace)
                    alpha_summary_path = next(
                        (workspace / "wiki" / "summaries").glob("alpha-policy-*.md")
                    )
                    alpha_summary_before = alpha_summary_path.read_text(encoding="utf-8")
                    beta_add = add_path(workspace, beta_source)
                    self.assertEqual(len(beta_add.added_documents), 1)
                    beta_hash = beta_add.added_documents[0].file_hash
                    second = compile_workspace(workspace)

            self.assertEqual(first.processed_files, 1)
            self.assertEqual(second.processed_files, 1)
            self.assertEqual(summary_calls["alpha-policy.md"], 1)
            self.assertEqual(summary_calls["beta-policy.md"], 1)

            alpha_summary_after = alpha_summary_path.read_text(encoding="utf-8")
            self.assertNotEqual(alpha_summary_before, alpha_summary_after)
            self.assertEqual(
                _strip_derived_pages(alpha_summary_before),
                _strip_derived_pages(alpha_summary_after),
            )
            self.assertIn("Summary body for alpha-policy.md call 1.", alpha_summary_after)
            self.assertNotIn("Summary body for alpha-policy.md call 2.", alpha_summary_after)
            self.assertRegex(
                alpha_summary_after,
                r"\[\[conflicts/medication-verification-mismatch-[a-f0-9]{6}\]\]",
            )

            assert first.job_id is not None
            assert second.job_id is not None
            alpha_job = JobStore(workspace / ".brain" / "jobs").read(first.job_id)
            second_job = JobStore(workspace / ".brain" / "jobs").read(second.job_id)
            self.assertEqual(alpha_job.payload["document_hashes"], [alpha_hash])
            self.assertEqual(second_job.payload["document_hashes"], [beta_hash])
            self.assertEqual(second_job.payload["document_count"], 1)


    def test_compile_job_marks_usage_unavailable_when_provider_omits_it(self) -> None:
        """Compile telemetry should keep usage safe when the provider reports none."""
        memory_keyring = _MemoryKeyring()
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "workspace"
            source = root / "policy.md"
            source.write_text("# Policy\n\nVerify each dose before release.", encoding="utf-8")

            _ = init_workspace(workspace)
            _ = add_path(workspace, source)

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
                        side_effect=_fake_llm_completion_no_usage,
                    ),
                    patch(
                        "evidence_compiler.compiler.pipeline.litellm.acompletion",
                        side_effect=_fake_llm_acompletion_no_usage,
                    ),
                ):
                    compile_result = compile_workspace(workspace)

            self.assertIsNotNone(compile_result.job_id)
            assert compile_result.job_id is not None
            jobs = JobStore(workspace / ".brain" / "jobs")
            compile_job = jobs.read(compile_result.job_id)
            self.assertIsNotNone(compile_job.compile)
            assert compile_job.compile is not None
            self.assertFalse(compile_job.compile.usage_total.available)
            self.assertGreater(compile_job.compile.usage_total.calls, 0)


class WatcherLifecycleTest(unittest.TestCase):
    """Validate low-level watcher lifecycle behavior used by watch mode."""

    def test_move_into_watched_folder_is_reported(self) -> None:
        """Moving a file into the watched folder should emit the destination path."""
        with tempfile.TemporaryDirectory() as temp_dir:
            watched = Path(temp_dir) / "watched"
            staging = Path(temp_dir) / "staging"
            watched.mkdir(parents=True, exist_ok=True)
            staging.mkdir(parents=True, exist_ok=True)

            observed: list[Path] = []
            changed = threading.Event()

            def _on_paths(paths: list[Path]) -> None:
                observed.extend(paths)
                changed.set()

            handle = start_file_watcher([watched], _on_paths, debounce_seconds=0.1)
            handle.start()
            try:
                source = staging / "policy.txt"
                source.write_text("Moved into watch root", encoding="utf-8")
                destination = watched / "policy.txt"
                source.replace(destination)

                self.assertTrue(changed.wait(3.0))
                self.assertIn(destination.resolve(), observed)
            finally:
                handle.stop()

    def test_hidden_files_are_ignored(self) -> None:
        """Hidden files should not be forwarded by the low-level watcher."""
        with tempfile.TemporaryDirectory() as temp_dir:
            watched = Path(temp_dir) / "watched"
            watched.mkdir(parents=True, exist_ok=True)

            observed: list[Path] = []
            changed = threading.Event()

            def _on_paths(paths: list[Path]) -> None:
                observed.extend(paths)
                changed.set()

            handle = start_file_watcher([watched], _on_paths, debounce_seconds=0.1)
            handle.start()
            try:
                (watched / ".hidden.txt").write_text("ignore me", encoding="utf-8")
                time.sleep(0.5)
                self.assertFalse(changed.is_set())
                self.assertEqual(observed, [])
            finally:
                handle.stop()


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
                        final_job: dict[str, object] = {}
                        for _ in range(40):
                            job = client.get(
                                f"/jobs/{job_id}", params={"workspace": workspace_id}
                            )
                            self.assertEqual(job.status_code, 200)
                            final_job = job.json()
                            final_status = str(final_job["status"])
                            if final_status in {"completed", "failed"}:
                                break
                            time.sleep(0.05)

                        self.assertEqual(final_status, "completed")
                        self.assertIn("compile", final_job)
                        compile_payload = final_job["compile"]
                        assert isinstance(compile_payload, dict)
                        self.assertIn("plan", compile_payload)
                        self.assertGreater(
                            int(compile_payload["usage_total"]["total_tokens"]), 0
                        )
                        self.assertTrue(compile_payload["usage_by_stage"])
            finally:
                if previous is None:
                    os.environ.pop("EVIDENCE_BRAIN_WORKSPACES_DIR", None)
                else:
                    os.environ["EVIDENCE_BRAIN_WORKSPACES_DIR"] = previous

    def test_duplicate_compile_request_returns_active_job(self) -> None:
        """Queueing compile twice should surface the existing active job id."""
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

                created = client.post("/workspaces", json={"name": "pilot-site"})
                self.assertEqual(created.status_code, 200)
                workspace_id = created.json()["workspace_id"]

                ingested = client.post(
                    "/documents/ingest",
                    json={"workspace": workspace_id, "path": str(source)},
                )
                self.assertEqual(ingested.status_code, 200)

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

                    with patch(
                        "brain_service.watch_manager.WatchManager._compile_in_background"
                    ):
                        first = client.post(
                            "/jobs/compile", json={"workspace": workspace_id}
                        )
                        self.assertEqual(first.status_code, 200)
                        first_job_id = first.json()["job_id"]

                        second = client.post(
                            "/jobs/compile", json={"workspace": workspace_id}
                        )
                        self.assertEqual(second.status_code, 409)
                        second_detail = second.json()["detail"]
                        self.assertEqual(
                            second_detail["code"], "compile_already_running"
                        )
                        self.assertEqual(second_detail["job_id"], first_job_id)
            finally:
                if previous is None:
                    os.environ.pop("EVIDENCE_BRAIN_WORKSPACES_DIR", None)
                else:
                    os.environ["EVIDENCE_BRAIN_WORKSPACES_DIR"] = previous

    def test_watch_endpoints_ingest_and_stop(self) -> None:
        """Raw-folder watch should ingest moved-in files, then stop cleanly."""
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_root = Path(temp_dir) / "workspaces"
            staging_root = Path(temp_dir) / "incoming"
            workspace_root.mkdir(parents=True, exist_ok=True)
            staging_root.mkdir(parents=True, exist_ok=True)

            previous = os.environ.get("EVIDENCE_BRAIN_WORKSPACES_DIR")
            os.environ["EVIDENCE_BRAIN_WORKSPACES_DIR"] = str(workspace_root)
            try:
                module = importlib.import_module("brain_service.main")
                module = importlib.reload(module)
                with TestClient(module.app) as client:
                    created = client.post("/workspaces", json={"name": "pilot-site"})
                    self.assertEqual(created.status_code, 200)
                    created_payload = created.json()
                    workspace_id = created_payload["workspace_id"]
                    raw_dir = Path(created_payload["root_path"]) / "raw"

                    started = client.put(
                        f"/workspaces/{workspace_id}/watch",
                        json={
                            "auto_compile": False,
                            "debounce_seconds": 0.1,
                        },
                    )
                    self.assertEqual(started.status_code, 200)
                    started_payload = started.json()
                    self.assertTrue(started_payload["enabled"])
                    self.assertEqual(started_payload["paths"], [str(raw_dir)])

                    staged = staging_root / "policy.txt"
                    staged.write_text("Watch ingest test", encoding="utf-8")
                    staged.replace(raw_dir / "policy.txt")

                    def _watch_ingested() -> bool:
                        watch_status = client.get(f"/workspaces/{workspace_id}/watch")
                        documents = client.get(
                            "/documents", params={"workspace": workspace_id}
                        )
                        return (
                            watch_status.status_code == 200
                            and documents.status_code == 200
                            and watch_status.json()["last_ingest_job_id"] is not None
                            and len(documents.json()["items"]) == 1
                        )

                    _wait_until(_watch_ingested)

                    stopped = client.delete(f"/workspaces/{workspace_id}/watch")
                    self.assertEqual(stopped.status_code, 200)
                    self.assertFalse(stopped.json()["enabled"])

                    (raw_dir / "ignored-after-stop.txt").write_text(
                        "Should not ingest", encoding="utf-8"
                    )
                    time.sleep(0.8)

                    documents_after_stop = client.get(
                        "/documents", params={"workspace": workspace_id}
                    )
                    self.assertEqual(documents_after_stop.status_code, 200)
                    self.assertEqual(len(documents_after_stop.json()["items"]), 1)
            finally:
                if previous is None:
                    os.environ.pop("EVIDENCE_BRAIN_WORKSPACES_DIR", None)
                else:
                    os.environ["EVIDENCE_BRAIN_WORKSPACES_DIR"] = previous

    def test_watch_backlog_lists_existing_raw_files_and_confirms_ingest(self) -> None:
        """Existing raw files should be reviewable first, then ingest on confirmation."""
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_root = Path(temp_dir) / "workspaces"
            workspace_root.mkdir(parents=True, exist_ok=True)

            previous = os.environ.get("EVIDENCE_BRAIN_WORKSPACES_DIR")
            os.environ["EVIDENCE_BRAIN_WORKSPACES_DIR"] = str(workspace_root)
            try:
                module = importlib.import_module("brain_service.main")
                module = importlib.reload(module)
                with TestClient(module.app) as client:
                    created = client.post("/workspaces", json={"name": "pilot-site"})
                    self.assertEqual(created.status_code, 200)
                    created_payload = created.json()
                    workspace_id = created_payload["workspace_id"]
                    raw_dir = Path(created_payload["root_path"]) / "raw"
                    raw_dir.mkdir(parents=True, exist_ok=True)

                    backlog_file = raw_dir / "policy.txt"
                    backlog_file.write_text("Existing raw file", encoding="utf-8")

                    backlog = client.get(f"/workspaces/{workspace_id}/watch/backlog")
                    self.assertEqual(backlog.status_code, 200)
                    backlog_payload = backlog.json()
                    self.assertEqual(backlog_payload["root"], str(raw_dir))
                    self.assertEqual(backlog_payload["total"], 1)
                    self.assertEqual(backlog_payload["items"][0]["path"], str(backlog_file))

                    confirmed = client.post(
                        f"/workspaces/{workspace_id}/watch/backlog/ingest",
                        json={"paths": [str(backlog_file)]},
                    )
                    self.assertEqual(confirmed.status_code, 200)
                    confirmed_payload = confirmed.json()
                    self.assertEqual(confirmed_payload["discovered_files"], 1)
                    self.assertEqual(len(confirmed_payload["added_documents"]), 1)

                    documents = client.get(
                        "/documents", params={"workspace": workspace_id}
                    )
                    self.assertEqual(documents.status_code, 200)
                    self.assertEqual(len(documents.json()["items"]), 1)

                    backlog_after = client.get(
                        f"/workspaces/{workspace_id}/watch/backlog"
                    )
                    self.assertEqual(backlog_after.status_code, 200)
                    self.assertEqual(backlog_after.json()["total"], 0)
            finally:
                if previous is None:
                    os.environ.pop("EVIDENCE_BRAIN_WORKSPACES_DIR", None)
                else:
                    os.environ["EVIDENCE_BRAIN_WORKSPACES_DIR"] = previous

    def test_watch_internal_raw_rename_is_noop(self) -> None:
        """Renaming an already indexed raw file should not create a new ingest job."""
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_root = Path(temp_dir) / "workspaces"
            workspace_root.mkdir(parents=True, exist_ok=True)

            previous = os.environ.get("EVIDENCE_BRAIN_WORKSPACES_DIR")
            os.environ["EVIDENCE_BRAIN_WORKSPACES_DIR"] = str(workspace_root)
            try:
                module = importlib.import_module("brain_service.main")
                module = importlib.reload(module)
                with TestClient(module.app) as client:
                    created = client.post("/workspaces", json={"name": "pilot-site"})
                    self.assertEqual(created.status_code, 200)
                    created_payload = created.json()
                    workspace_id = created_payload["workspace_id"]
                    raw_dir = Path(created_payload["root_path"]) / "raw"

                    started = client.put(
                        f"/workspaces/{workspace_id}/watch",
                        json={
                            "auto_compile": False,
                            "debounce_seconds": 0.1,
                        },
                    )
                    self.assertEqual(started.status_code, 200)

                    original = raw_dir / "policy.txt"
                    original.write_text("Rename me", encoding="utf-8")

                    def _ingested_once() -> bool:
                        watch_status = client.get(f"/workspaces/{workspace_id}/watch")
                        documents = client.get(
                            "/documents", params={"workspace": workspace_id}
                        )
                        return (
                            watch_status.status_code == 200
                            and documents.status_code == 200
                            and watch_status.json()["last_ingest_job_id"] is not None
                            and len(documents.json()["items"]) == 1
                        )

                    _wait_until(_ingested_once)
                    first_watch_status = client.get(f"/workspaces/{workspace_id}/watch")
                    self.assertEqual(first_watch_status.status_code, 200)
                    first_ingest_job_id = first_watch_status.json()["last_ingest_job_id"]

                    renamed = raw_dir / "policy-renamed.txt"
                    original.replace(renamed)
                    time.sleep(0.8)

                    second_watch_status = client.get(f"/workspaces/{workspace_id}/watch")
                    documents = client.get(
                        "/documents", params={"workspace": workspace_id}
                    )
                    backlog = client.get(f"/workspaces/{workspace_id}/watch/backlog")
                    self.assertEqual(second_watch_status.status_code, 200)
                    self.assertEqual(documents.status_code, 200)
                    self.assertEqual(backlog.status_code, 200)
                    self.assertEqual(second_watch_status.json()["last_ingest_job_id"], first_ingest_job_id)
                    self.assertEqual(len(documents.json()["items"]), 1)
                    self.assertEqual(backlog.json()["total"], 0)
            finally:
                if previous is None:
                    os.environ.pop("EVIDENCE_BRAIN_WORKSPACES_DIR", None)
                else:
                    os.environ["EVIDENCE_BRAIN_WORKSPACES_DIR"] = previous

    def test_watch_auto_compile_coalesces_follow_up_job(self) -> None:
        """New docs arriving during compile should produce only one follow-up compile."""
        memory_keyring = _MemoryKeyring()
        with tempfile.TemporaryDirectory() as temp_dir:
            workspace_root = Path(temp_dir) / "workspaces"
            workspace_root.mkdir(parents=True, exist_ok=True)

            compile_started = threading.Event()
            release_compile = threading.Event()
            compile_calls: list[str] = []
            compile_target_hashes: list[list[str]] = []

            def _fake_run_compile_job(workspace: Path, job_id: str | None):
                assert job_id is not None
                compile_calls.append(job_id)
                jobs = JobStore(workspace / ".brain" / "jobs")
                registry = HashRegistry(workspace / ".brain" / "hashes.json")
                job = jobs.read(job_id)
                target_hashes = [
                    str(file_hash)
                    for file_hash in job.payload.get("document_hashes", [])
                ]
                compile_target_hashes.append(target_hashes)
                jobs.update(
                    job_id,
                    status="running",
                    stage="planning",
                    progress=0.25,
                    message="Running test compile",
                )
                compile_started.set()
                release_compile.wait(timeout=5.0)
                for file_hash in target_hashes:
                    registry.update_document(
                        file_hash,
                        status="compiled",
                        requires_pageindex=False,
                    )
                jobs.update(
                    job_id,
                    status="completed",
                    stage="completed",
                    progress=1.0,
                    message="Completed test compile",
                )
                return None

            previous = os.environ.get("EVIDENCE_BRAIN_WORKSPACES_DIR")
            os.environ["EVIDENCE_BRAIN_WORKSPACES_DIR"] = str(workspace_root)
            try:
                module = importlib.import_module("brain_service.main")
                module = importlib.reload(module)
                with TestClient(module.app) as client:
                    created = client.post("/workspaces", json={"name": "pilot-site"})
                    self.assertEqual(created.status_code, 200)
                    payload = created.json()
                    workspace_id = payload["workspace_id"]
                    workspace_path = Path(payload["root_path"])
                    raw_dir = workspace_path / "raw"

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

                        with patch(
                            "brain_service.watch_manager.run_compile_job",
                            side_effect=_fake_run_compile_job,
                        ):
                            started = client.put(
                                f"/workspaces/{workspace_id}/watch",
                                json={
                                    "auto_compile": True,
                                    "debounce_seconds": 0.1,
                                },
                            )
                            self.assertEqual(started.status_code, 200)

                            (raw_dir / "first.txt").write_text(
                                "First watch file", encoding="utf-8"
                            )
                            self.assertTrue(compile_started.wait(5.0))

                            def _first_ingest_done() -> bool:
                                documents = client.get(
                                    "/documents", params={"workspace": workspace_id}
                                )
                                return (
                                    documents.status_code == 200
                                    and len(documents.json()["items"]) == 1
                                )

                            _wait_until(_first_ingest_done)

                            (raw_dir / "second.txt").write_text(
                                "Second watch file", encoding="utf-8"
                            )

                            def _second_ingest_done() -> bool:
                                documents = client.get(
                                    "/documents", params={"workspace": workspace_id}
                                )
                                return (
                                    documents.status_code == 200
                                    and len(documents.json()["items"]) == 2
                                )

                            _wait_until(_second_ingest_done)
                            release_compile.set()

                            def _compile_jobs_completed() -> bool:
                                jobs = JobStore(workspace_path / ".brain" / "jobs")
                                compile_jobs = [
                                    job
                                    for job in jobs.list_jobs()
                                    if job.kind == "compile"
                                ]
                                return (
                                    len(compile_jobs) == 2
                                    and all(
                                        job.status == "completed"
                                        for job in compile_jobs
                                    )
                                )

                            _wait_until(_compile_jobs_completed)

                    jobs = JobStore(workspace_path / ".brain" / "jobs")
                    compile_jobs = [
                        job for job in jobs.list_jobs() if job.kind == "compile"
                    ]
                    self.assertEqual(len(compile_jobs), 2)
                    self.assertEqual(len(compile_calls), 2)
                    self.assertEqual([len(item) for item in compile_target_hashes], [1, 1])
                    self.assertNotEqual(
                        compile_target_hashes[0][0],
                        compile_target_hashes[1][0],
                    )
                    self.assertEqual(
                        [job.payload["document_count"] for job in compile_jobs],
                        [1, 1],
                    )

                    status = client.get(f"/workspaces/{workspace_id}/watch")
                    self.assertEqual(status.status_code, 200)
                    status_payload = status.json()
                    self.assertEqual(
                        status_payload["last_compile_job_id"], compile_jobs[-1].job_id
                    )
                    self.assertIsNone(status_payload["active_compile_job_id"])
            finally:
                if previous is None:
                    os.environ.pop("EVIDENCE_BRAIN_WORKSPACES_DIR", None)
                else:
                    os.environ["EVIDENCE_BRAIN_WORKSPACES_DIR"] = previous


if __name__ == "__main__":
    unittest.main()
