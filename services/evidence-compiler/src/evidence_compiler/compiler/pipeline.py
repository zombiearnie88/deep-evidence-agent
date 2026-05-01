"""Milestone 2 compilation pipeline orchestration facade."""

from __future__ import annotations

import asyncio
import hashlib
import re
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar

import litellm
from pydantic import BaseModel, ValidationError

from evidence_compiler.compiler.evidence import (
    _EVIDENCE_DRAFT_MAX_TOKENS,
    _anchor_exists,
    _bootstrap_evidence_pages_from_wiki,
    _collapse_whitespace,
    _document_evidence_briefs,
    _draft_evidence as _draft_evidence_impl,
    _draft_evidence_messages,
    _evidence_manifest_path,
    _existing_evidence_pages,
    _extract_section,
    _group_evidence_pages,
    _infer_anchor_from_match,
    _line_range_for_span,
    _load_evidence_manifests,
    _quote_search_pattern,
    _remove_stale_evidence_pages,
    _render_evidence_page,
    _stable_evidence_id,
    _summary_link,
    _verify_evidence_output,
    _write_evidence_manifest,
    _write_evidence_validation_report,
)
from evidence_compiler.compiler.llm import (
    _StructuredResponseTruncatedError,
    _add_completion_error_note,
    _extract_completion_content,
    _extract_finish_reason,
    _extract_first_choice,
    _extract_usage,
    _is_json_invalid_validation,
    _is_truncated_structured_validation,
    _looks_like_truncated_json,
    _preview_text,
    _response_field,
    _safe_json,
    _should_retry_without_structured_output,
    _structured_acompletion as _structured_acompletion_impl,
    _structured_completion as _structured_completion_impl,
    _validate_structured_response,
    _validate_unstructured_response,
)
from evidence_compiler.compiler.models import (
    CompileArtifacts,
    ConflictCheckResult,
    ConflictPageOutput,
    DraftCreateUpdateFn,
    EvidenceDocumentManifest,
    EvidenceDraftOutput,
    EvidenceDraftQuote,
    EvidencePlanActions,
    EvidencePlanItem,
    EvidenceValidationIssue,
    PagePlanActions,
    PagePlanItem,
    ProcedurePageOutput,
    RegulationPageOutput,
    SummaryStageResult,
    TaxonomyPlanResult,
    TopicPageOutput,
    VerifiedEvidenceInstance,
    _EvidencePageState,
    _MaterializedDocument,
)
from evidence_compiler.compiler.pages import (
    _CONFLICT_BODY_GUIDANCE,
    _CONFLICT_FIELD_GUIDE,
    _CONFLICT_TYPE_RULES,
    _DRAFT_CONCURRENCY,
    _MANAGED_PAGE_SECTION_HEADINGS,
    _PROCEDURE_BODY_GUIDANCE,
    _PROCEDURE_FIELD_GUIDE,
    _PROCEDURE_TYPE_RULES,
    _REGULATION_BODY_GUIDANCE,
    _REGULATION_FIELD_GUIDE,
    _REGULATION_TYPE_RULES,
    _STOPWORDS,
    _TOPIC_BODY_GUIDANCE,
    _TOPIC_FIELD_GUIDE,
    _TOPIC_TYPE_RULES,
    _add_related_summary,
    _append_unique,
    _apply_actions,
    _brief_for_index,
    _confirm_conflict as _confirm_conflict_impl,
    _draft_conflict,
    _draft_conflict_page as _draft_conflict_page_impl,
    _draft_procedure,
    _draft_procedure_page as _draft_procedure_page_impl,
    _draft_regulation,
    _draft_regulation_page as _draft_regulation_page_impl,
    _draft_topic,
    _draft_topic_page as _draft_topic_page_impl,
    _ensure_links_in_section,
    _existing_page_briefs,
    _extract_title,
    _list_from_meta,
    _page_draft_messages,
    _read_page,
    _render_conflict_page,
    _render_frontmatter,
    _render_procedure_page,
    _render_regulation_page,
    _render_topic_page,
    _split_frontmatter,
    _strip_managed_sections,
    _tokenize_subject,
    _upsert_typed_page,
    _write_page,
)
from evidence_compiler.compiler.planning import (
    _EVIDENCE_PLAN_MAX_TOKENS,
    _PLAN_PREVIEW_LIMIT,
    _TAXONOMY_PLAN_MAX_TOKENS,
    _build_compile_plan_summary,
    _build_plan_bucket,
    _contains_any,
    _downstream_messages,
    _evidence_planner_messages,
    _filter_candidate_evidence_ids,
    _finalize_evidence_plan,
    _finalize_taxonomy_plan,
    _has_explicit_conflict_signal,
    _has_explicit_role_workflow,
    _has_normative_reference_signal,
    _is_informational_reference_document,
    _item_implies_no_conflict,
    _json_blob,
    _merge_plan_buckets,
    _normalize_claim_key,
    _normalize_evidence_plan_item,
    _normalize_plan_item,
    _plan_evidence as _plan_evidence_impl,
    _plan_taxonomy as _plan_taxonomy_impl,
    _planning_context_text,
    _reconcile_evidence_actions,
    _reconcile_page_actions,
    _sanitize_evidence_plan,
    _sanitize_taxonomy_plan,
    _taxonomy_planner_messages,
)
from evidence_compiler.compiler.summaries import (
    _SUMMARY_MAX_TOKENS,
    _derive_brief,
    _fence_marker,
    _is_structured_markdown_line,
    _materialize_long_document,
    _materialize_short_document,
    _normalize_inline_markdown_structure,
    _normalize_summary_markdown,
    _reflow_markdown_paragraphs,
    _relative_ref,
    _slugify,
    _summarize_document as _summarize_document_impl,
    _summary_messages,
    _to_int,
    _write_summary_page,
)
from evidence_compiler.credentials import provider_env
from knowledge_models.compiler_api import (
    CompilePlanBucket,
    CompilePlanDocument,
    CompilePlanItem as CompilePlanPreviewItem,
    CompilePlanSummary,
    DocumentRecord,
    TokenUsageSummary,
)

StageCallback = Callable[[str, str], None]
CounterCallback = Callable[[str, int, int, str, str | None], None]
PlanCallback = Callable[[CompilePlanSummary], None]
UsageCallback = Callable[[str, TokenUsageSummary], None]
UsageDeltaCallback = Callable[[TokenUsageSummary], None]
ModelT = TypeVar("ModelT", bound=BaseModel)

_SECTION_SPECS: list[tuple[str, str]] = [
    ("Summaries", "summaries"),
    ("Topics", "topics"),
    ("Regulations", "regulations"),
    ("Procedures", "procedures"),
    ("Conflicts", "conflicts"),
    ("Evidence", "evidence"),
]


def _emit_stage(callback: StageCallback | None, stage: str, message: str) -> None:
    if callback is not None:
        callback(stage, message)


def _emit_counter(
    callback: CounterCallback | None,
    stage: str,
    completed: int,
    total: int,
    unit: str,
    item_label: str | None = None,
) -> None:
    if callback is not None:
        callback(stage, completed, total, unit, item_label)


def _emit_plan(callback: PlanCallback | None, plan_summary: CompilePlanSummary) -> None:
    if callback is not None:
        callback(plan_summary)


def _stage_usage_reporter(
    callback: UsageCallback | None, stage: str
) -> UsageDeltaCallback | None:
    if callback is None:
        return None
    return lambda usage: callback(stage, usage)


def _structured_completion(
    *,
    model: str,
    messages: list[dict[str, str]],
    response_model: type[ModelT],
    max_tokens: int | None = None,
    usage_callback: UsageDeltaCallback | None = None,
) -> ModelT:
    return _structured_completion_impl(
        model=model,
        messages=messages,
        response_model=response_model,
        max_tokens=max_tokens,
        usage_callback=usage_callback,
    )


async def _structured_acompletion(
    *,
    model: str,
    messages: list[dict[str, str]],
    response_model: type[ModelT],
    max_tokens: int | None = None,
    usage_callback: UsageDeltaCallback | None = None,
) -> ModelT:
    return await _structured_acompletion_impl(
        model=model,
        messages=messages,
        response_model=response_model,
        max_tokens=max_tokens,
        usage_callback=usage_callback,
    )


def _summarize_document(
    *,
    model: str,
    language: str,
    materialized: _MaterializedDocument,
    usage_callback: UsageDeltaCallback | None = None,
) -> SummaryStageResult:
    return _summarize_document_impl(
        model=model,
        language=language,
        materialized=materialized,
        usage_callback=usage_callback,
        structured_completion=_structured_completion,
    )


def _plan_evidence(
    *,
    model: str,
    language: str,
    materialized: _MaterializedDocument,
    summary: SummaryStageResult,
    existing_evidence_briefs: dict[str, dict[str, str]],
    existing_evidence_pages: dict[str, _EvidencePageState],
    usage_callback: UsageDeltaCallback | None = None,
) -> EvidencePlanActions:
    return _plan_evidence_impl(
        model=model,
        language=language,
        materialized=materialized,
        summary=summary,
        existing_evidence_briefs=existing_evidence_briefs,
        existing_evidence_pages=existing_evidence_pages,
        usage_callback=usage_callback,
        structured_completion=_structured_completion,
    )


def _plan_taxonomy(
    *,
    model: str,
    language: str,
    materialized: _MaterializedDocument,
    summary: SummaryStageResult,
    existing_briefs: dict[str, dict[str, str]],
    document_evidence_briefs: list[dict[str, str]],
    document_evidence_ids: set[str],
    usage_callback: UsageDeltaCallback | None = None,
) -> TaxonomyPlanResult:
    return _plan_taxonomy_impl(
        model=model,
        language=language,
        materialized=materialized,
        summary=summary,
        existing_briefs=existing_briefs,
        document_evidence_briefs=document_evidence_briefs,
        document_evidence_ids=document_evidence_ids,
        usage_callback=usage_callback,
        structured_completion=_structured_completion,
    )


async def _draft_evidence(
    *,
    model: str,
    language: str,
    materialized: _MaterializedDocument,
    summary: SummaryStageResult,
    item: EvidencePlanItem,
    usage_callback: UsageDeltaCallback | None = None,
) -> EvidenceDraftOutput:
    return await _draft_evidence_impl(
        model=model,
        language=language,
        materialized=materialized,
        summary=summary,
        item=item,
        usage_callback=usage_callback,
        structured_acompletion=_structured_acompletion,
    )


async def _draft_topic_page(
    *,
    model: str,
    language: str,
    materialized: _MaterializedDocument,
    summary: SummaryStageResult,
    item: PagePlanItem,
    evidence_pack: list[VerifiedEvidenceInstance],
    is_update: bool,
    existing_body: str,
    usage_callback: UsageDeltaCallback | None = None,
) -> TopicPageOutput:
    return await _draft_topic_page_impl(
        model=model,
        language=language,
        materialized=materialized,
        summary=summary,
        item=item,
        evidence_pack=evidence_pack,
        is_update=is_update,
        existing_body=existing_body,
        usage_callback=usage_callback,
        structured_acompletion=_structured_acompletion,
    )


async def _draft_regulation_page(
    *,
    model: str,
    language: str,
    materialized: _MaterializedDocument,
    summary: SummaryStageResult,
    item: PagePlanItem,
    evidence_pack: list[VerifiedEvidenceInstance],
    is_update: bool,
    existing_body: str,
    usage_callback: UsageDeltaCallback | None = None,
) -> RegulationPageOutput:
    return await _draft_regulation_page_impl(
        model=model,
        language=language,
        materialized=materialized,
        summary=summary,
        item=item,
        evidence_pack=evidence_pack,
        is_update=is_update,
        existing_body=existing_body,
        usage_callback=usage_callback,
        structured_acompletion=_structured_acompletion,
    )


async def _draft_procedure_page(
    *,
    model: str,
    language: str,
    materialized: _MaterializedDocument,
    summary: SummaryStageResult,
    item: PagePlanItem,
    evidence_pack: list[VerifiedEvidenceInstance],
    is_update: bool,
    existing_body: str,
    usage_callback: UsageDeltaCallback | None = None,
) -> ProcedurePageOutput:
    return await _draft_procedure_page_impl(
        model=model,
        language=language,
        materialized=materialized,
        summary=summary,
        item=item,
        evidence_pack=evidence_pack,
        is_update=is_update,
        existing_body=existing_body,
        usage_callback=usage_callback,
        structured_acompletion=_structured_acompletion,
    )


async def _draft_conflict_page(
    *,
    model: str,
    language: str,
    materialized: _MaterializedDocument,
    summary: SummaryStageResult,
    item: PagePlanItem,
    evidence_pack: list[VerifiedEvidenceInstance],
    is_update: bool,
    existing_body: str,
    usage_callback: UsageDeltaCallback | None = None,
) -> ConflictPageOutput:
    return await _draft_conflict_page_impl(
        model=model,
        language=language,
        materialized=materialized,
        summary=summary,
        item=item,
        evidence_pack=evidence_pack,
        is_update=is_update,
        existing_body=existing_body,
        usage_callback=usage_callback,
        structured_acompletion=_structured_acompletion,
    )


def _confirm_conflict(
    *,
    model: str,
    language: str,
    left_title: str,
    right_title: str,
    left_text: str,
    right_text: str,
    usage_callback: UsageDeltaCallback | None = None,
) -> ConflictCheckResult:
    return _confirm_conflict_impl(
        model=model,
        language=language,
        left_title=left_title,
        right_title=right_title,
        left_text=left_text,
        right_text=right_text,
        usage_callback=usage_callback,
        structured_completion=_structured_completion,
    )


def compile_documents(
    workspace: Path,
    documents: list[DocumentRecord],
    *,
    provider: str,
    model: str,
    api_key: str,
    language: str,
    stage_callback: StageCallback | None = None,
    counter_callback: CounterCallback | None = None,
    plan_callback: PlanCallback | None = None,
    usage_callback: UsageCallback | None = None,
) -> CompileArtifacts:
    """Compile documents into taxonomy-native wiki pages for Milestone 2."""
    artifacts = CompileArtifacts()
    materialized_docs: list[_MaterializedDocument] = []
    summaries_by_hash: dict[str, SummaryStageResult] = {}
    summary_slug_by_hash: dict[str, str] = {}
    summary_to_links: dict[str, set[str]] = defaultdict(set)
    summary_to_pages: dict[str, set[Path]] = defaultdict(set)
    related_conflicts_by_page: dict[Path, set[str]] = defaultdict(set)
    backlink_candidate_pages: set[Path] = set()
    evidence_plans_by_hash: dict[str, EvidencePlanActions] = {}
    evidence_drafts_by_hash: dict[str, list[tuple[EvidencePlanItem, EvidenceDraftOutput]]] = {}
    verified_evidence_by_hash: dict[str, list[VerifiedEvidenceInstance]] = {}
    document_evidence_by_id: dict[str, dict[str, VerifiedEvidenceInstance]] = {}
    taxonomy_plans_by_hash: dict[str, TaxonomyPlanResult] = {}
    evidence_link_by_id: dict[str, str] = {}

    def _action_total(actions: PagePlanActions) -> int:
        return len(actions.create) + len(actions.update) + len(actions.related)

    def _evidence_action_total(actions: EvidencePlanActions) -> int:
        return len(actions.create) + len(actions.update)

    with provider_env(provider, api_key):
        _emit_stage(stage_callback, "indexing-long-docs", "Materializing source artifacts")
        _emit_counter(
            counter_callback,
            "indexing-long-docs",
            0,
            len(documents),
            "documents",
            "documents",
        )
        for index, document in enumerate(documents, start=1):
            if document.requires_pageindex:
                materialized = _materialize_long_document(workspace, document, artifacts)
            else:
                materialized = _materialize_short_document(workspace, document)
            materialized_docs.append(materialized)
            _emit_counter(
                counter_callback,
                "indexing-long-docs",
                index,
                len(documents),
                "documents",
                "documents",
            )

        _emit_stage(stage_callback, "summarizing", "Generating typed summaries")
        _emit_counter(
            counter_callback,
            "summarizing",
            0,
            len(materialized_docs),
            "documents",
            "summaries",
        )
        for index, materialized in enumerate(materialized_docs, start=1):
            summary = _summarize_document(
                model=model,
                language=language,
                materialized=materialized,
                usage_callback=_stage_usage_reporter(usage_callback, "summarizing"),
            )
            summaries_by_hash[materialized.document.file_hash] = summary
            summary_slug_by_hash[materialized.document.file_hash] = materialized.summary_slug
            summary_page = _write_summary_page(
                workspace=workspace,
                materialized=materialized,
                summary=summary,
            )
            _append_unique(artifacts.summaries, summary_page)
            _emit_counter(
                counter_callback,
                "summarizing",
                index,
                len(materialized_docs),
                "documents",
                "summaries",
            )

        existing_evidence_pages = _existing_evidence_pages(workspace)
        _emit_stage(stage_callback, "planning-evidence", "Planning evidence pages")
        _emit_counter(
            counter_callback,
            "planning-evidence",
            0,
            len(materialized_docs),
            "documents",
            "evidence plans",
        )
        for index, materialized in enumerate(materialized_docs, start=1):
            summary = summaries_by_hash[materialized.document.file_hash]
            current_evidence_briefs = {
                slug: {
                    "page_slug": state.page_slug,
                    "claim_key": state.claim_key,
                    "claim": state.canonical_claim,
                    "title": state.title,
                    "brief": state.brief,
                }
                for slug, state in existing_evidence_pages.items()
            }
            evidence_plan = _plan_evidence(
                model=model,
                language=language,
                materialized=materialized,
                summary=summary,
                existing_evidence_briefs=current_evidence_briefs,
                existing_evidence_pages=existing_evidence_pages,
                usage_callback=_stage_usage_reporter(usage_callback, "planning-evidence"),
            )
            evidence_plans_by_hash[materialized.document.file_hash] = evidence_plan
            for item in [*evidence_plan.create, *evidence_plan.update]:
                existing_evidence_pages[item.page_slug] = _EvidencePageState(
                    page_slug=item.page_slug,
                    claim_key=_normalize_claim_key(item.claim),
                    canonical_claim=item.claim,
                    title=item.title,
                    brief=item.brief or _derive_brief(item.claim),
                )
            _emit_counter(
                counter_callback,
                "planning-evidence",
                index,
                len(materialized_docs),
                "documents",
                "evidence plans",
            )

        evidence_item_total = sum(
            _evidence_action_total(plan) for plan in evidence_plans_by_hash.values()
        )
        _emit_stage(stage_callback, "drafting-evidence", "Drafting evidence quotes")
        _emit_counter(
            counter_callback,
            "drafting-evidence",
            0,
            evidence_item_total,
            "items",
            "evidence items",
        )
        drafted_evidence_completed = 0
        for materialized in materialized_docs:
            doc_hash = materialized.document.file_hash
            evidence_drafts: list[tuple[EvidencePlanItem, EvidenceDraftOutput]] = []
            summary = summaries_by_hash[doc_hash]
            for item in [
                *evidence_plans_by_hash[doc_hash].create,
                *evidence_plans_by_hash[doc_hash].update,
            ]:
                evidence_drafts.append(
                    (
                        item,
                        asyncio.run(
                            _draft_evidence(
                                model=model,
                                language=language,
                                materialized=materialized,
                                summary=summary,
                                item=item,
                                usage_callback=_stage_usage_reporter(
                                    usage_callback, "drafting-evidence"
                                ),
                            )
                        ),
                    )
                )
                drafted_evidence_completed += 1
                _emit_counter(
                    counter_callback,
                    "drafting-evidence",
                    drafted_evidence_completed,
                    evidence_item_total,
                    "items",
                    "evidence items",
                )
            evidence_drafts_by_hash[doc_hash] = evidence_drafts

        _emit_stage(stage_callback, "verifying-evidence", "Verifying evidence against source")
        _emit_counter(
            counter_callback,
            "verifying-evidence",
            0,
            evidence_item_total,
            "items",
            "evidence items",
        )
        verified_evidence_completed = 0
        evidence_manifests: list[EvidenceDocumentManifest] = []
        for materialized in materialized_docs:
            doc_hash = materialized.document.file_hash
            verified_items: dict[str, VerifiedEvidenceInstance] = {}
            dropped_items: list[EvidenceValidationIssue] = []
            for plan_item, draft in evidence_drafts_by_hash.get(doc_hash, []):
                verified, dropped = _verify_evidence_output(
                    materialized=materialized,
                    plan_item=plan_item,
                    draft=draft,
                )
                for item in verified:
                    verified_items[item.evidence_id] = item
                dropped_items.extend(dropped)
                verified_evidence_completed += 1
                _emit_counter(
                    counter_callback,
                    "verifying-evidence",
                    verified_evidence_completed,
                    evidence_item_total,
                    "items",
                    "evidence items",
                )
            verified_list = [verified_items[key] for key in sorted(verified_items)]
            manifest = EvidenceDocumentManifest(
                document_hash=doc_hash,
                document_name=materialized.document.name,
                summary_slug=materialized.summary_slug,
                source_ref=materialized.downstream_source_ref,
                items=verified_list,
                dropped=dropped_items,
            )
            _write_evidence_manifest(workspace, manifest)
            evidence_manifests.append(manifest)
            verified_evidence_by_hash[doc_hash] = verified_list
            document_evidence_by_id[doc_hash] = {
                item.evidence_id: item for item in verified_list
            }

        _write_evidence_validation_report(workspace, evidence_manifests)

        authoritative_manifests = _load_evidence_manifests(workspace)
        authoritative_evidence_pages = _group_evidence_pages(
            authoritative_manifests, workspace
        )

        _emit_stage(stage_callback, "planning-taxonomy", "Planning taxonomy actions")
        existing_briefs = {
            "topics": _existing_page_briefs(workspace / "wiki", "topics"),
            "regulations": _existing_page_briefs(workspace / "wiki", "regulations"),
            "procedures": _existing_page_briefs(workspace / "wiki", "procedures"),
            "conflicts": _existing_page_briefs(workspace / "wiki", "conflicts"),
        }
        _emit_counter(
            counter_callback,
            "planning-taxonomy",
            0,
            len(materialized_docs),
            "documents",
            "plans",
        )
        for index, materialized in enumerate(materialized_docs, start=1):
            doc_hash = materialized.document.file_hash
            summary = summaries_by_hash[doc_hash]
            taxonomy_plan = _plan_taxonomy(
                model=model,
                language=language,
                materialized=materialized,
                summary=summary,
                existing_briefs=existing_briefs,
                document_evidence_briefs=_document_evidence_briefs(
                    verified_evidence_by_hash.get(doc_hash, [])
                ),
                document_evidence_ids=set(document_evidence_by_id.get(doc_hash, {})),
                usage_callback=_stage_usage_reporter(usage_callback, "planning-taxonomy"),
            )
            taxonomy_plans_by_hash[doc_hash] = taxonomy_plan
            _emit_counter(
                counter_callback,
                "planning-taxonomy",
                index,
                len(materialized_docs),
                "documents",
                "plans",
            )
        _emit_plan(
            plan_callback,
            _build_compile_plan_summary(
                materialized_docs,
                taxonomy_plans_by_hash,
                evidence_plans_by_hash,
            ),
        )

        _emit_stage(stage_callback, "writing-topics", "Drafting and writing topic pages")
        topic_total = sum(_action_total(plan.topics) for plan in taxonomy_plans_by_hash.values())
        topic_completed = 0
        _emit_counter(
            counter_callback,
            "writing-topics",
            0,
            topic_total,
            "pages",
            "pages",
        )
        for materialized in materialized_docs:
            doc_hash = materialized.document.file_hash
            topic_actions = taxonomy_plans_by_hash[doc_hash].topics
            topic_touched = _apply_actions(
                workspace=workspace,
                page_type="topics",
                model=model,
                language=language,
                materialized=materialized,
                actions=topic_actions,
                summary_slug=summary_slug_by_hash[doc_hash],
                summary=summaries_by_hash[doc_hash],
                document_evidence_by_id=document_evidence_by_id.get(doc_hash, {}),
                artifacts_bucket=artifacts.topics,
                summary_to_links=summary_to_links,
                summary_to_pages=summary_to_pages,
                draft_create_update=_draft_topic_page,
                render_markdown=_render_topic_page,
                usage_callback=_stage_usage_reporter(usage_callback, "writing-topics"),
            )
            backlink_candidate_pages.update(topic_touched)
            topic_completed += _action_total(topic_actions)
            _emit_counter(
                counter_callback,
                "writing-topics",
                topic_completed,
                topic_total,
                "pages",
                "pages",
            )

        _emit_stage(
            stage_callback,
            "writing-regulations",
            "Drafting and writing regulation pages",
        )
        touched_regulations: list[Path] = []
        regulation_total = sum(
            _action_total(plan.regulations) for plan in taxonomy_plans_by_hash.values()
        )
        regulation_completed = 0
        _emit_counter(
            counter_callback,
            "writing-regulations",
            0,
            regulation_total,
            "pages",
            "pages",
        )
        for materialized in materialized_docs:
            doc_hash = materialized.document.file_hash
            regulation_actions = taxonomy_plans_by_hash[doc_hash].regulations
            touched = _apply_actions(
                workspace=workspace,
                page_type="regulations",
                model=model,
                language=language,
                materialized=materialized,
                actions=regulation_actions,
                summary_slug=summary_slug_by_hash[doc_hash],
                summary=summaries_by_hash[doc_hash],
                document_evidence_by_id=document_evidence_by_id.get(doc_hash, {}),
                artifacts_bucket=artifacts.regulations,
                summary_to_links=summary_to_links,
                summary_to_pages=summary_to_pages,
                draft_create_update=_draft_regulation_page,
                render_markdown=_render_regulation_page,
                usage_callback=_stage_usage_reporter(usage_callback, "writing-regulations"),
            )
            touched_regulations.extend(touched)
            backlink_candidate_pages.update(touched)
            regulation_completed += _action_total(regulation_actions)
            _emit_counter(
                counter_callback,
                "writing-regulations",
                regulation_completed,
                regulation_total,
                "pages",
                "pages",
            )

        _emit_stage(
            stage_callback,
            "writing-procedures",
            "Drafting and writing procedure pages",
        )
        touched_procedures: list[Path] = []
        procedure_total = sum(
            _action_total(plan.procedures) for plan in taxonomy_plans_by_hash.values()
        )
        procedure_completed = 0
        _emit_counter(
            counter_callback,
            "writing-procedures",
            0,
            procedure_total,
            "pages",
            "pages",
        )
        for materialized in materialized_docs:
            doc_hash = materialized.document.file_hash
            procedure_actions = taxonomy_plans_by_hash[doc_hash].procedures
            touched = _apply_actions(
                workspace=workspace,
                page_type="procedures",
                model=model,
                language=language,
                materialized=materialized,
                actions=procedure_actions,
                summary_slug=summary_slug_by_hash[doc_hash],
                summary=summaries_by_hash[doc_hash],
                document_evidence_by_id=document_evidence_by_id.get(doc_hash, {}),
                artifacts_bucket=artifacts.procedures,
                summary_to_links=summary_to_links,
                summary_to_pages=summary_to_pages,
                draft_create_update=_draft_procedure_page,
                render_markdown=_render_procedure_page,
                usage_callback=_stage_usage_reporter(usage_callback, "writing-procedures"),
            )
            touched_procedures.extend(touched)
            backlink_candidate_pages.update(touched)
            procedure_completed += _action_total(procedure_actions)
            _emit_counter(
                counter_callback,
                "writing-procedures",
                procedure_completed,
                procedure_total,
                "pages",
                "pages",
            )

        _emit_stage(
            stage_callback,
            "writing-evidence",
            "Rendering claim-centric evidence pages from verified manifests",
        )
        evidence_instances_by_page: dict[str, list[VerifiedEvidenceInstance]] = defaultdict(list)
        for manifest in authoritative_manifests:
            for item in manifest.items:
                evidence_instances_by_page[item.page_slug].append(item)
        authoritative_evidence_slugs = set(evidence_instances_by_page)
        _emit_counter(
            counter_callback,
            "writing-evidence",
            0,
            len(evidence_instances_by_page),
            "pages",
            "evidence pages",
        )
        for index, page_slug in enumerate(sorted(evidence_instances_by_page), start=1):
            instances = evidence_instances_by_page[page_slug]
            page_state = authoritative_evidence_pages.get(page_slug)
            if page_state is None:
                sample = instances[0]
                page_state = _EvidencePageState(
                    page_slug=page_slug,
                    claim_key=sample.claim_key,
                    canonical_claim=sample.canonical_claim,
                    title=sample.title,
                    brief=sample.brief,
                )
            meta, body = _render_evidence_page(
                page_slug=page_slug,
                page_state=page_state,
                instances=instances,
            )
            written = _write_page(workspace / "wiki" / "evidence" / f"{page_slug}.md", meta, body)
            _append_unique(artifacts.evidence, written)
            evidence_link = f"[[evidence/{page_slug}]]"
            for item in instances:
                evidence_link_by_id[item.evidence_id] = evidence_link
            for source_link in _list_from_meta(meta.get("source_summaries")):
                match = re.search(r"\[\[summaries/([^\]]+)\]\]", source_link)
                if match:
                    summary_to_links[match.group(1)].add(evidence_link)
            _emit_counter(
                counter_callback,
                "writing-evidence",
                index,
                len(evidence_instances_by_page),
                "pages",
                "evidence pages",
            )
        _remove_stale_evidence_pages(workspace, authoritative_evidence_slugs)

        all_reg_proc = sorted(
            set((workspace / "wiki" / "regulations").glob("*.md"))
            | set((workspace / "wiki" / "procedures").glob("*.md")),
            key=str,
        )
        touched_for_detection = sorted(set(touched_regulations + touched_procedures), key=str)
        candidate_pairs: list[tuple[Path, Path]] = []
        checked_pairs: set[tuple[str, str]] = set()
        for left_path in touched_for_detection:
            left_meta, left_body = _read_page(left_path)
            left_title = _extract_title(left_meta, left_body, left_path.stem)
            left_tokens = _tokenize_subject(left_title)
            if not left_tokens:
                continue
            for right_path in all_reg_proc:
                if right_path == left_path:
                    continue
                left_key = str(left_path)
                right_key = str(right_path)
                candidate_pair_key: tuple[str, str] = (
                    (left_key, right_key) if left_key <= right_key else (right_key, left_key)
                )
                if candidate_pair_key in checked_pairs:
                    continue
                checked_pairs.add(candidate_pair_key)

                right_meta, right_body = _read_page(right_path)
                right_title = _extract_title(right_meta, right_body, right_path.stem)
                overlap = left_tokens & _tokenize_subject(right_title)
                if len(overlap) < 2:
                    continue
                candidate_pairs.append((left_path, right_path))

        _emit_stage(stage_callback, "writing-conflicts", "Drafting and writing conflict pages")
        planner_conflict_total = sum(
            _action_total(plan.conflicts) for plan in taxonomy_plans_by_hash.values()
        )
        conflict_total = planner_conflict_total + len(candidate_pairs)
        conflict_completed = 0
        _emit_counter(
            counter_callback,
            "writing-conflicts",
            0,
            conflict_total,
            "items",
            "conflict work items",
        )
        for materialized in materialized_docs:
            doc_hash = materialized.document.file_hash
            conflict_actions = taxonomy_plans_by_hash[doc_hash].conflicts
            touched_conflicts = _apply_actions(
                workspace=workspace,
                page_type="conflicts",
                model=model,
                language=language,
                materialized=materialized,
                actions=conflict_actions,
                summary_slug=summary_slug_by_hash[doc_hash],
                summary=summaries_by_hash[doc_hash],
                document_evidence_by_id=document_evidence_by_id.get(doc_hash, {}),
                artifacts_bucket=artifacts.conflicts,
                summary_to_links=summary_to_links,
                summary_to_pages=summary_to_pages,
                draft_create_update=_draft_conflict_page,
                render_markdown=_render_conflict_page,
                usage_callback=_stage_usage_reporter(usage_callback, "writing-conflicts"),
            )
            backlink_candidate_pages.update(touched_conflicts)
            for conflict_page in touched_conflicts:
                conflict_link = f"[[conflicts/{conflict_page.stem}]]"
                for derived in summary_to_pages[summary_slug_by_hash[doc_hash]]:
                    if derived != conflict_page:
                        related_conflicts_by_page[derived].add(conflict_link)
            conflict_completed += _action_total(conflict_actions)
            _emit_counter(
                counter_callback,
                "writing-conflicts",
                conflict_completed,
                conflict_total,
                "items",
                "conflict work items",
            )

        if candidate_pairs:
            _emit_stage(
                stage_callback,
                "writing-conflicts",
                "Checking regulation and procedure pairs for conflicts",
            )

        for left_path, right_path in candidate_pairs:
            left_meta, left_body = _read_page(left_path)
            left_title = _extract_title(left_meta, left_body, left_path.stem)
            right_meta, right_body = _read_page(right_path)
            right_title = _extract_title(right_meta, right_body, right_path.stem)
            left_key = str(left_path)
            right_key = str(right_path)
            conflict_pair_key: tuple[str, str] = (
                (left_key, right_key) if left_key <= right_key else (right_key, left_key)
            )

            decision = _confirm_conflict(
                model=model,
                language=language,
                left_title=left_title,
                right_title=right_title,
                left_text=left_body,
                right_text=right_body,
                usage_callback=_stage_usage_reporter(usage_callback, "writing-conflicts"),
            )
            if decision.is_conflict:
                conflict_title = decision.title.strip() or f"{left_title} vs {right_title}"
                suffix = hashlib.sha1(
                    (conflict_pair_key[0] + conflict_pair_key[1]).encode("utf-8")
                ).hexdigest()[:6]
                conflict_slug = f"{_slugify(conflict_title)}-{suffix}"
                conflict_path = workspace / "wiki" / "conflicts" / f"{conflict_slug}.md"
                left_target = _relative_ref(workspace / "wiki", left_path.with_suffix(""))
                right_target = _relative_ref(workspace / "wiki", right_path.with_suffix(""))
                impacted = [f"[[{left_target}]]", f"[[{right_target}]]"]
                source_links = sorted(
                    set(_list_from_meta(left_meta.get("source_summaries")))
                    | set(_list_from_meta(right_meta.get("source_summaries")))
                )
                used_evidence_ids = sorted(
                    set(_list_from_meta(left_meta.get("used_evidence_ids")))
                    | set(_list_from_meta(right_meta.get("used_evidence_ids")))
                )
                conflict_output = ConflictPageOutput(
                    title=conflict_title,
                    brief=decision.description.strip() or "Potential conflict identified.",
                    description_markdown=decision.description.strip()
                    or "Potential contradiction identified across compiled pages.",
                    impacted_pages=impacted,
                    used_evidence_ids=used_evidence_ids,
                )
                written = _upsert_typed_page(
                    path=conflict_path,
                    page_type="conflicts",
                    title=conflict_output.title,
                    brief=conflict_output.brief,
                    body=_render_conflict_page(conflict_output),
                    summary_link=source_links[0] if source_links else "",
                    used_evidence_ids=used_evidence_ids,
                )
                if source_links:
                    meta, body = _read_page(written)
                    merged_links = sorted(
                        set(_list_from_meta(meta.get("source_summaries")) + source_links)
                    )
                    meta["source_summaries"] = merged_links
                    body = _ensure_links_in_section(body, "Source Summaries", merged_links)
                    _write_page(written, meta, body)
                _append_unique(artifacts.conflicts, written)
                backlink_candidate_pages.add(written)

                conflict_link = f"[[conflicts/{written.stem}]]"
                related_conflicts_by_page[left_path].add(conflict_link)
                related_conflicts_by_page[right_path].add(conflict_link)
                for source_link in source_links:
                    match = re.search(r"\[\[summaries/([^\]]+)\]\]", source_link)
                    if match:
                        summary_to_links[match.group(1)].add(conflict_link)

            conflict_completed += 1
            _emit_counter(
                counter_callback,
                "writing-conflicts",
                conflict_completed,
                conflict_total,
                "items",
                "conflict work items",
            )

        _emit_stage(stage_callback, "backlinking", "Applying code-driven backlinks")
        related_evidence_by_page: dict[Path, set[str]] = defaultdict(set)
        for page_path in backlink_candidate_pages:
            if not page_path.exists():
                continue
            meta, _ = _read_page(page_path)
            for evidence_id in _list_from_meta(meta.get("used_evidence_ids")):
                evidence_link = evidence_link_by_id.get(evidence_id)
                if evidence_link:
                    related_evidence_by_page[page_path].add(evidence_link)
        summary_backlink_paths = {path for path in artifacts.summaries if path.exists()}
        for summary_slug in summary_to_links:
            summary_path = workspace / "wiki" / "summaries" / f"{summary_slug}.md"
            if summary_path.exists():
                summary_backlink_paths.add(summary_path)
        backlink_total = len(summary_backlink_paths | backlink_candidate_pages)
        backlinked_pages: set[Path] = set()
        _emit_counter(
            counter_callback,
            "backlinking",
            0,
            backlink_total,
            "pages",
            "touched pages",
        )
        for summary_path in sorted(summary_backlink_paths, key=str):
            summary_slug = summary_path.stem
            meta, body = _read_page(summary_path)
            links = sorted(summary_to_links.get(summary_slug, set()))
            body = _ensure_links_in_section(body, "Derived Pages", links)
            _write_page(summary_path, meta, body)
            backlinked_pages.add(summary_path)
            _emit_counter(
                counter_callback,
                "backlinking",
                len(backlinked_pages),
                backlink_total,
                "pages",
                "touched pages",
            )

        for page_path in sorted(backlink_candidate_pages, key=str):
            if not page_path.exists():
                continue
            meta, body = _read_page(page_path)
            conflict_links = sorted(related_conflicts_by_page.get(page_path, set()))
            evidence_links = sorted(related_evidence_by_page.get(page_path, set()))
            if conflict_links:
                body = _ensure_links_in_section(body, "Related Conflicts", conflict_links)
            if evidence_links:
                body = _ensure_links_in_section(body, "Related Evidence", evidence_links)
            _write_page(page_path, meta, body)
            backlinked_pages.add(page_path)
            _emit_counter(
                counter_callback,
                "backlinking",
                len(backlinked_pages),
                backlink_total,
                "pages",
                "touched pages",
            )

    return artifacts


def rebuild_index(workspace: Path, _artifacts: CompileArtifacts) -> Path:
    """Rebuild wiki/index.md with section-aware entries and short briefs."""
    wiki = workspace / "wiki"
    lines = ["# Evidence Brain Wiki Index", ""]
    for heading, folder in _SECTION_SPECS:
        lines.append(f"## {heading}")
        page_dir = wiki / folder
        pages = sorted(page_dir.glob("*.md")) if page_dir.exists() else []
        if not pages:
            lines.append("- (none)")
            lines.append("")
            continue

        for page in pages:
            rel = page.relative_to(wiki).with_suffix("")
            target = str(rel).replace("\\", "/")
            meta, body = _read_page(page)
            title = _extract_title(meta, body, page.stem.replace("-", " ").title())
            link = f"[[{target}|{title}]]" if title else f"[[{target}]]"
            brief = _brief_for_index(page)
            if brief:
                lines.append(f"- {link} - {brief}")
            else:
                lines.append(f"- {link}")
        lines.append("")

    output = wiki / "index.md"
    output.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    return output
