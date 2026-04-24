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

from evidence_compiler.compiler import pipeline as compiler_pipeline
from evidence_compiler.api import (
    add_path,
    compile_workspace,
    set_workspace_credentials,
    get_status,
    init_workspace,
)
from evidence_compiler.state import JobStore


def _fake_llm_payload(*args, **kwargs) -> dict[str, object]:
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
        )

    def test_planner_messages_use_assistant_summary_turn(self) -> None:
        """Planner prompt should place the summary in an assistant turn."""
        summary = compiler_pipeline.SummaryStageResult(
            document_brief="Short planning brief.",
            summary_markdown="## Overview\nPlanner summary body.",
        )

        messages = compiler_pipeline._planner_messages(
            language="English",
            document_name="planner-source.md",
            summary=summary,
            existing_briefs={"topics": {"drug-a": "Existing topic brief."}},
        )

        self.assertEqual(
            [message["role"] for message in messages],
            ["system", "user", "assistant", "user"],
        )
        self.assertIn("Document: planner-source.md", messages[1]["content"])
        self.assertIn("Brief: Short planning brief.", messages[1]["content"])
        self.assertIn("Planner summary body.", messages[2]["content"])
        self.assertIn("Using the summary above, return keys:", messages[3]["content"])
        self.assertIn("drug-a", messages[3]["content"])

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
            "evidence": {},
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
                    )
                ],
                update=[
                    compiler_pipeline.PagePlanItem(
                        slug="aap-otitis-media-guidelines",
                        title="AAP Otitis Media Guidelines",
                        brief="AAP prefers amoxicillin for initial therapy.",
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
                    )
                ]
            ),
            evidence=[
                compiler_pipeline.EvidencePlanItem(
                    claim=" Cefdinir can be given once daily or in 2 divided doses. ",
                    quote=" Once daily or q12h. ",
                    anchor=" line:89-91 ",
                )
            ],
        )
        existing_briefs = {
            "topics": {},
            "regulations": {
                "idsa-sinusitis-guidelines": "Cefdinir is not recommended as empiric monotherapy.",
                "aap-otitis-media-guidelines": "AAP prefers amoxicillin for initial therapy.",
            },
            "procedures": {},
            "conflicts": {},
            "evidence": {},
        }

        finalized = compiler_pipeline._finalize_taxonomy_plan(
            plan,
            materialized=self._materialized("cefdinir-monograph.md"),
            summary=summary,
            existing_briefs=existing_briefs,
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
            finalized.evidence[0].claim,
            "Cefdinir can be given once daily or in 2 divided doses.",
        )
        self.assertEqual(finalized.evidence[0].quote, "Once daily or q12h.")
        self.assertEqual(finalized.evidence[0].anchor, "line:89-91")

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
                    )
                ],
                update=[
                    compiler_pipeline.PagePlanItem(
                        slug="new-topic",
                        title="New Topic",
                        brief="Should move to create because the slug is new.",
                    )
                ],
                related=["existing-topic", "new-topic", "missing-topic"],
            ),
            evidence=[
                compiler_pipeline.EvidencePlanItem(
                    claim=" Stable handling rule ",
                    quote=" Verify before dispensing. ",
                    anchor=" section:workflow ",
                )
            ],
        )
        existing_briefs = {
            "topics": {"existing-topic": "Current topic brief."},
            "regulations": {},
            "procedures": {},
            "conflicts": {},
            "evidence": {},
        }

        finalized = compiler_pipeline._finalize_taxonomy_plan(
            plan,
            materialized=self._materialized("clinic-policy.md"),
            summary=summary,
            existing_briefs=existing_briefs,
        )

        self.assertEqual([item.slug for item in finalized.topics.create], ["new-topic"])
        self.assertEqual(
            [item.slug for item in finalized.topics.update], ["existing-topic"]
        )
        self.assertEqual(finalized.topics.related, [])
        self.assertEqual(finalized.evidence[0].claim, "Stable handling rule")
        self.assertEqual(finalized.evidence[0].quote, "Verify before dispensing.")
        self.assertEqual(finalized.evidence[0].anchor, "section:workflow")


class PageDraftPromptTest(unittest.TestCase):
    """Validate shared drafting prompt structure and page-type rules."""

    def _summary(self) -> compiler_pipeline.SummaryStageResult:
        return compiler_pipeline.SummaryStageResult(
            document_brief="Short drafting brief.",
            summary_markdown="## Overview\nDrafting summary body.",
        )

    def _item(self) -> compiler_pipeline.PagePlanItem:
        return compiler_pipeline.PagePlanItem(
            slug="draft-target",
            title="Draft Target",
            brief="Planner-provided brief.",
        )

    def test_page_draft_messages_use_assistant_summary_turn(self) -> None:
        """The source summary should be supplied in an assistant turn."""
        messages = compiler_pipeline._page_draft_messages(
            language="English",
            page_type="topic",
            document_name="draft-source.md",
            summary=self._summary(),
            item=self._item(),
            is_update=False,
            existing_body="",
            body_guidance=compiler_pipeline._TOPIC_BODY_GUIDANCE,
            type_rules=compiler_pipeline._TOPIC_TYPE_RULES,
            field_guide=compiler_pipeline._TOPIC_FIELD_GUIDE,
        )

        self.assertEqual(
            [message["role"] for message in messages],
            ["system", "user", "assistant", "user"],
        )
        self.assertIn(
            "You will receive the current source summary next.",
            messages[1]["content"],
        )
        self.assertIn("Drafting summary body.", messages[2]["content"])
        self.assertIn("Draft a topic page.", messages[3]["content"])

    def test_page_draft_update_uses_delimited_existing_page_block(self) -> None:
        """Rewrite prompts should delimit the current page body clearly."""
        messages = compiler_pipeline._page_draft_messages(
            language="English",
            page_type="conflict",
            document_name="draft-source.md",
            summary=self._summary(),
            item=self._item(),
            is_update=True,
            existing_body="## Existing\nCurrent body text.",
            body_guidance=compiler_pipeline._CONFLICT_BODY_GUIDANCE,
            type_rules=compiler_pipeline._CONFLICT_TYPE_RULES,
            field_guide=compiler_pipeline._CONFLICT_FIELD_GUIDE,
        )

        self.assertIn("<<<EXISTING_PAGE", messages[3]["content"])
        self.assertIn("## Existing\nCurrent body text.", messages[3]["content"])
        self.assertIn("EXISTING_PAGE", messages[3]["content"])

    def test_procedure_prompt_omits_markdown_body_rules(self) -> None:
        """Procedure prompts should not include markdown-body-only rules."""
        procedure_messages = compiler_pipeline._page_draft_messages(
            language="English",
            page_type="procedure",
            document_name="draft-source.md",
            summary=self._summary(),
            item=self._item(),
            is_update=False,
            existing_body="",
            body_guidance=compiler_pipeline._PROCEDURE_BODY_GUIDANCE,
            type_rules=compiler_pipeline._PROCEDURE_TYPE_RULES,
            field_guide=compiler_pipeline._PROCEDURE_FIELD_GUIDE,
        )
        topic_messages = compiler_pipeline._page_draft_messages(
            language="English",
            page_type="topic",
            document_name="draft-source.md",
            summary=self._summary(),
            item=self._item(),
            is_update=False,
            existing_body="",
            body_guidance=compiler_pipeline._TOPIC_BODY_GUIDANCE,
            type_rules=compiler_pipeline._TOPIC_TYPE_RULES,
            field_guide=compiler_pipeline._TOPIC_FIELD_GUIDE,
        )

        self.assertNotIn("Markdown body rules:", procedure_messages[3]["content"])
        self.assertIn("Markdown body rules:", topic_messages[3]["content"])

    def test_page_type_body_guidance_is_specific(self) -> None:
        """Each page type should carry narrowly-scoped drafting guidance."""
        topic_messages = compiler_pipeline._page_draft_messages(
            language="English",
            page_type="topic",
            document_name="draft-source.md",
            summary=self._summary(),
            item=self._item(),
            is_update=False,
            existing_body="",
            body_guidance=compiler_pipeline._TOPIC_BODY_GUIDANCE,
            type_rules=compiler_pipeline._TOPIC_TYPE_RULES,
            field_guide=compiler_pipeline._TOPIC_FIELD_GUIDE,
        )
        regulation_messages = compiler_pipeline._page_draft_messages(
            language="English",
            page_type="regulation",
            document_name="draft-source.md",
            summary=self._summary(),
            item=self._item(),
            is_update=False,
            existing_body="",
            body_guidance=compiler_pipeline._REGULATION_BODY_GUIDANCE,
            type_rules=compiler_pipeline._REGULATION_TYPE_RULES,
            field_guide=compiler_pipeline._REGULATION_FIELD_GUIDE,
        )
        procedure_messages = compiler_pipeline._page_draft_messages(
            language="English",
            page_type="procedure",
            document_name="draft-source.md",
            summary=self._summary(),
            item=self._item(),
            is_update=False,
            existing_body="",
            body_guidance=compiler_pipeline._PROCEDURE_BODY_GUIDANCE,
            type_rules=compiler_pipeline._PROCEDURE_TYPE_RULES,
            field_guide=compiler_pipeline._PROCEDURE_FIELD_GUIDE,
        )
        conflict_messages = compiler_pipeline._page_draft_messages(
            language="English",
            page_type="conflict",
            document_name="draft-source.md",
            summary=self._summary(),
            item=self._item(),
            is_update=False,
            existing_body="",
            body_guidance=compiler_pipeline._CONFLICT_BODY_GUIDANCE,
            type_rules=compiler_pipeline._CONFLICT_TYPE_RULES,
            field_guide=compiler_pipeline._CONFLICT_FIELD_GUIDE,
        )

        self.assertIn(
            "Do not turn the page into a regulation, procedure, or conflict record.",
            topic_messages[3]["content"],
        )
        self.assertIn(
            "requirement_markdown should state the binding recommendation, rule, or restriction",
            regulation_messages[3]["content"],
        )
        self.assertIn(
            "Return 3-7 concise imperative steps as plain step strings",
            procedure_messages[3]["content"],
        )
        self.assertIn(
            "Do not frame uncertainty, lack of evidence, or 'no conflict' as a conflict.",
            conflict_messages[3]["content"],
        )

    def test_page_type_rules_are_specific(self) -> None:
        """Each page type should include its own semantic constraint block."""
        topic_messages = compiler_pipeline._page_draft_messages(
            language="English",
            page_type="topic",
            document_name="draft-source.md",
            summary=self._summary(),
            item=self._item(),
            is_update=False,
            existing_body="",
            body_guidance=compiler_pipeline._TOPIC_BODY_GUIDANCE,
            type_rules=compiler_pipeline._TOPIC_TYPE_RULES,
            field_guide=compiler_pipeline._TOPIC_FIELD_GUIDE,
        )
        regulation_messages = compiler_pipeline._page_draft_messages(
            language="English",
            page_type="regulation",
            document_name="draft-source.md",
            summary=self._summary(),
            item=self._item(),
            is_update=False,
            existing_body="",
            body_guidance=compiler_pipeline._REGULATION_BODY_GUIDANCE,
            type_rules=compiler_pipeline._REGULATION_TYPE_RULES,
            field_guide=compiler_pipeline._REGULATION_FIELD_GUIDE,
        )
        procedure_messages = compiler_pipeline._page_draft_messages(
            language="English",
            page_type="procedure",
            document_name="draft-source.md",
            summary=self._summary(),
            item=self._item(),
            is_update=False,
            existing_body="",
            body_guidance=compiler_pipeline._PROCEDURE_BODY_GUIDANCE,
            type_rules=compiler_pipeline._PROCEDURE_TYPE_RULES,
            field_guide=compiler_pipeline._PROCEDURE_FIELD_GUIDE,
        )
        conflict_messages = compiler_pipeline._page_draft_messages(
            language="English",
            page_type="conflict",
            document_name="draft-source.md",
            summary=self._summary(),
            item=self._item(),
            is_update=False,
            existing_body="",
            body_guidance=compiler_pipeline._CONFLICT_BODY_GUIDANCE,
            type_rules=compiler_pipeline._CONFLICT_TYPE_RULES,
            field_guide=compiler_pipeline._CONFLICT_FIELD_GUIDE,
        )

        self.assertIn("Type-specific rules for topic:", topic_messages[3]["content"])
        self.assertIn(
            "Do not write operational steps, checklists, or workflow instructions.",
            topic_messages[3]["content"],
        )
        self.assertIn(
            "Type-specific rules for regulation:",
            regulation_messages[3]["content"],
        )
        self.assertIn(
            "Do not repeat the same sentence across requirement_markdown, applicability_markdown, and authority_markdown.",
            regulation_messages[3]["content"],
        )
        self.assertIn(
            "Type-specific rules for procedure:",
            procedure_messages[3]["content"],
        )
        self.assertIn(
            "Do not embed numbering, bullets, or markdown formatting inside step strings.",
            procedure_messages[3]["content"],
        )
        self.assertIn("Type-specific rules for conflict:", conflict_messages[3]["content"])
        self.assertIn(
            "Never write a 'no conflict' or 'resolved without mismatch' conflict page.",
            conflict_messages[3]["content"],
        )

    def test_page_type_field_guides_are_specific(self) -> None:
        """Each page type should expose a narrow field contract."""
        topic_messages = compiler_pipeline._page_draft_messages(
            language="English",
            page_type="topic",
            document_name="draft-source.md",
            summary=self._summary(),
            item=self._item(),
            is_update=False,
            existing_body="",
            body_guidance=compiler_pipeline._TOPIC_BODY_GUIDANCE,
            type_rules=compiler_pipeline._TOPIC_TYPE_RULES,
            field_guide=compiler_pipeline._TOPIC_FIELD_GUIDE,
        )
        regulation_messages = compiler_pipeline._page_draft_messages(
            language="English",
            page_type="regulation",
            document_name="draft-source.md",
            summary=self._summary(),
            item=self._item(),
            is_update=False,
            existing_body="",
            body_guidance=compiler_pipeline._REGULATION_BODY_GUIDANCE,
            type_rules=compiler_pipeline._REGULATION_TYPE_RULES,
            field_guide=compiler_pipeline._REGULATION_FIELD_GUIDE,
        )
        procedure_messages = compiler_pipeline._page_draft_messages(
            language="English",
            page_type="procedure",
            document_name="draft-source.md",
            summary=self._summary(),
            item=self._item(),
            is_update=False,
            existing_body="",
            body_guidance=compiler_pipeline._PROCEDURE_BODY_GUIDANCE,
            type_rules=compiler_pipeline._PROCEDURE_TYPE_RULES,
            field_guide=compiler_pipeline._PROCEDURE_FIELD_GUIDE,
        )
        conflict_messages = compiler_pipeline._page_draft_messages(
            language="English",
            page_type="conflict",
            document_name="draft-source.md",
            summary=self._summary(),
            item=self._item(),
            is_update=False,
            existing_body="",
            body_guidance=compiler_pipeline._CONFLICT_BODY_GUIDANCE,
            type_rules=compiler_pipeline._CONFLICT_TYPE_RULES,
            field_guide=compiler_pipeline._CONFLICT_FIELD_GUIDE,
        )

        self.assertIn(
            "brief: one sentence under 180 chars defining the stable subject",
            topic_messages[3]["content"],
        )
        self.assertIn(
            "requirement_markdown: normative requirement details only; do not write operational steps",
            regulation_messages[3]["content"],
        )
        self.assertIn(
            "steps: 3-7 concise imperative action strings in execution order; no numbering, bullets, or extra commentary",
            procedure_messages[3]["content"],
        )
        self.assertIn(
            "impacted_pages: explicit wiki links such as [[regulations/foo]] only when supported by context; otherwise []",
            conflict_messages[3]["content"],
        )


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
            assert compile_result.job_id is not None

            jobs = JobStore(workspace / ".brain" / "jobs")
            compile_job = jobs.read(compile_result.job_id)
            self.assertEqual(compile_job.status, "completed")
            self.assertIsNotNone(compile_job.compile)
            assert compile_job.compile is not None
            self.assertIsNotNone(compile_job.compile.plan)
            self.assertGreater(compile_job.compile.usage_total.total_tokens, 0)
            self.assertTrue(compile_job.compile.usage_total.available)
            self.assertTrue(compile_job.compile.usage_by_stage)
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

                    with patch("brain_service.main._compile_in_background"):
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


if __name__ == "__main__":
    unittest.main()
