"""Milestone 2 compilation pipeline for taxonomy-native wiki generation."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import unicodedata
from collections import defaultdict
from collections.abc import Awaitable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol, TypeVar

import litellm
import yaml
from json_repair import repair_json
from pageindex_adapter import get_page_content, get_structure, index_pdf
from pydantic import BaseModel, Field, ValidationError

from evidence_compiler.credentials import provider_env
from evidence_compiler.models import DocumentRecord

StageCallback = Callable[[str, str], None]
ModelT = TypeVar("ModelT", bound=BaseModel)
RenderModelT = TypeVar("RenderModelT", bound=BaseModel)
DraftModelT = TypeVar("DraftModelT", bound=BaseModel, covariant=True)


class DraftCreateUpdateFn(Protocol[DraftModelT]):
    def __call__(
        self,
        *,
        model: str,
        language: str,
        document_name: str,
        summary: SummaryStageResult,
        item: PagePlanItem,
        is_update: bool,
        existing_body: str,
    ) -> Awaitable[DraftModelT]: ...


@dataclass
class CompileArtifacts:
    """Generated wiki pages grouped by taxonomy."""

    summaries: list[Path] = field(default_factory=list)
    topics: list[Path] = field(default_factory=list)
    regulations: list[Path] = field(default_factory=list)
    procedures: list[Path] = field(default_factory=list)
    conflicts: list[Path] = field(default_factory=list)
    evidence: list[Path] = field(default_factory=list)
    pageindex_artifacts: dict[str, str] = field(default_factory=dict)

    @property
    def total_pages(self) -> int:
        unique = {
            *self.summaries,
            *self.topics,
            *self.regulations,
            *self.procedures,
            *self.conflicts,
            *self.evidence,
        }
        return len(unique)


@dataclass
class _MaterializedDocument:
    document: DocumentRecord
    summary_slug: str
    source_ref: str
    text_for_summary: str
    summary_seed_ref: str | None = None


class SummaryStageResult(BaseModel):
    document_brief: str = Field(min_length=1, max_length=220)
    summary_markdown: str = Field(min_length=1)


class PagePlanItem(BaseModel):
    slug: str
    title: str
    brief: str = ""


class PagePlanActions(BaseModel):
    create: list[PagePlanItem] = Field(default_factory=list)
    update: list[PagePlanItem] = Field(default_factory=list)
    related: list[str] = Field(default_factory=list)


class EvidencePlanItem(BaseModel):
    claim: str
    quote: str = ""
    anchor: str = ""
    normalized_claim: str = ""


class TaxonomyPlanResult(BaseModel):
    topics: PagePlanActions = Field(default_factory=PagePlanActions)
    regulations: PagePlanActions = Field(default_factory=PagePlanActions)
    procedures: PagePlanActions = Field(default_factory=PagePlanActions)
    conflicts: PagePlanActions = Field(default_factory=PagePlanActions)
    evidence: list[EvidencePlanItem] = Field(default_factory=list)


class TopicPageOutput(BaseModel):
    title: str
    brief: str
    context_markdown: str


class RegulationPageOutput(BaseModel):
    title: str
    brief: str
    requirement_markdown: str
    applicability_markdown: str
    authority_markdown: str


class ProcedurePageOutput(BaseModel):
    title: str
    brief: str
    steps: list[str] = Field(default_factory=list)


class ConflictPageOutput(BaseModel):
    title: str
    brief: str
    description_markdown: str
    impacted_pages: list[str] = Field(default_factory=list)


class ConflictCheckResult(BaseModel):
    is_conflict: bool
    title: str = ""
    description: str = ""


@dataclass
class _EvidenceQuote:
    quote: str
    anchor: str
    summary_link: str


@dataclass
class _EvidenceAggregate:
    claim: str
    quotes: list[_EvidenceQuote] = field(default_factory=list)
    source_summaries: set[str] = field(default_factory=set)


_SECTION_SPECS: list[tuple[str, str]] = [
    ("Summaries", "summaries"),
    ("Topics", "topics"),
    ("Regulations", "regulations"),
    ("Procedures", "procedures"),
    ("Conflicts", "conflicts"),
    ("Evidence", "evidence"),
]

_STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "from",
    "this",
    "that",
    "must",
    "shall",
    "into",
    "under",
    "over",
    "what",
    "why",
    "how",
    "where",
    "when",
    "who",
}

_DRAFT_CONCURRENCY = 5


def _emit_stage(callback: StageCallback | None, stage: str, message: str) -> None:
    if callback is not None:
        callback(stage, message)


def _slugify(value: str) -> str:
    normalized = (
        unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    )
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", normalized.lower()).strip("-")
    return slug or "item"


def _normalize_claim_key(value: str) -> str:
    base = _slugify(value)
    return base[:90] if base else "claim"


def _to_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _relative_ref(workspace: Path, path: Path) -> str:
    try:
        return str(path.relative_to(workspace)).replace("\\", "/")
    except ValueError:
        return str(path)


def _safe_json(value: str) -> dict[str, object]:
    text = value.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        text = text[first_newline + 1 :] if first_newline != -1 else text[3:]
        if text.endswith("```"):
            text = text[:-3]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        parsed = json.loads(repair_json(text))
    if isinstance(parsed, dict):
        return parsed
    raise ValueError("LLM output is not a JSON object")


def _extract_completion_content(response: object) -> str:
    choices = getattr(response, "choices", None)
    if not isinstance(choices, list) or not choices:
        raise ValueError("LiteLLM response has no choices")
    message = getattr(choices[0], "message", None)
    if message is None:
        raise ValueError("LiteLLM response choice has no message")
    content = getattr(message, "content", "")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False)


def _structured_completion(
    *,
    model: str,
    messages: list[dict[str, str]],
    response_model: type[ModelT],
) -> ModelT:
    try:
        response = litellm.completion(
            model=model,
            messages=messages,
            temperature=0,
            response_format=response_model,
        )
        content = _extract_completion_content(response)
        return response_model.model_validate_json(content)
    except Exception:
        response = litellm.completion(model=model, messages=messages, temperature=0)
        content = _extract_completion_content(response)
        payload = _safe_json(content)
        return response_model.model_validate(payload)


async def _structured_acompletion(
    *,
    model: str,
    messages: list[dict[str, str]],
    response_model: type[ModelT],
) -> ModelT:
    try:
        response = await litellm.acompletion(
            model=model,
            messages=messages,
            temperature=0,
            response_format=response_model,
        )
        content = _extract_completion_content(response)
        return response_model.model_validate_json(content)
    except Exception:
        response = await litellm.acompletion(
            model=model,
            messages=messages,
            temperature=0,
        )
        content = _extract_completion_content(response)
        payload = _safe_json(content)
        return response_model.model_validate(payload)


def _derive_brief(markdown: str) -> str:
    lines = [line.strip() for line in markdown.splitlines() if line.strip()]
    for line in lines:
        if line.startswith("#"):
            continue
        sentence = re.split(r"[.!?]\s", line, maxsplit=1)[0].strip()
        if sentence:
            return sentence[:180]
    return "Compiled summary"


def _split_frontmatter(text: str) -> tuple[dict[str, object], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end == -1:
        return {}, text
    raw_meta = text[4:end]
    body = text[end + 5 :]
    payload = yaml.safe_load(raw_meta) or {}
    if not isinstance(payload, dict):
        payload = {}
    normalized = {str(key): value for key, value in payload.items()}
    return normalized, body


def _render_frontmatter(meta: dict[str, object]) -> str:
    content = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False).strip()
    return f"---\n{content}\n---\n\n"


def _read_page(path: Path) -> tuple[dict[str, object], str]:
    if not path.exists():
        return {}, ""
    text = path.read_text(encoding="utf-8")
    return _split_frontmatter(text)


def _write_page(path: Path, meta: dict[str, object], body: str) -> Path:
    """Persist markdown + YAML frontmatter for a wiki page."""
    path.parent.mkdir(parents=True, exist_ok=True)
    # Keep file shape deterministic so page diffs stay reviewable across runs.
    content = _render_frontmatter(meta) + body.strip() + "\n"
    path.write_text(content, encoding="utf-8")
    return path


def _ensure_links_in_section(body: str, heading: str, links: list[str]) -> str:
    """Upsert a heading section with a stable, sorted link list."""
    clean_links = sorted({link for link in links if link})
    rendered = (
        "\n".join(f"- {link}" for link in clean_links) if clean_links else "- (none)"
    )
    # Replacing existing section keeps markdown idempotent when called repeatedly.
    replacement = f"## {heading}\n{rendered}\n"
    pattern = re.compile(
        rf"^## {re.escape(heading)}\n.*?(?=^## |\Z)", re.MULTILINE | re.DOTALL
    )
    if pattern.search(body):
        updated = pattern.sub(replacement, body).strip()
        return updated + "\n"
    suffix = body.rstrip()
    if suffix:
        return suffix + "\n\n" + replacement + "\n"
    return replacement + "\n"


def _list_from_meta(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _append_unique(paths: list[Path], path: Path) -> None:
    if path not in paths:
        paths.append(path)


def _existing_page_briefs(wiki: Path, folder: str) -> dict[str, str]:
    page_dir = wiki / folder
    if not page_dir.exists():
        return {}
    briefs: dict[str, str] = {}
    for page in sorted(page_dir.glob("*.md")):
        meta, body = _read_page(page)
        brief = str(meta.get("brief") or "").strip()
        if not brief:
            brief = _derive_brief(body)
        briefs[page.stem] = brief
    return briefs


def _materialize_short_document(
    workspace: Path, document: DocumentRecord
) -> _MaterializedDocument:
    if document.source_path and document.source_path.exists():
        text = document.source_path.read_text(encoding="utf-8", errors="ignore")
        source_ref = _relative_ref(workspace, document.source_path)
    else:
        text = document.raw_path.read_text(encoding="utf-8", errors="ignore")
        source_ref = _relative_ref(workspace, document.raw_path)
    summary_slug = f"{_slugify(document.name)}-{document.file_hash[:8]}"
    return _MaterializedDocument(
        document=document,
        summary_slug=summary_slug,
        source_ref=source_ref,
        text_for_summary=text,
    )


def _materialize_long_document(
    workspace: Path, document: DocumentRecord, artifacts: CompileArtifacts
) -> _MaterializedDocument:
    artifact_dir = workspace / ".brain" / "pageindex" / document.file_hash
    indexed = index_pdf(document.raw_path, artifact_dir)
    artifacts.pageindex_artifacts[document.file_hash] = indexed.artifact_path

    summary_slug = f"{_slugify(document.name)}-{document.file_hash[:8]}"
    structure = get_structure(Path(indexed.artifact_path))
    pages = get_page_content(
        Path(indexed.artifact_path), 1, min(indexed.page_count, 20)
    )

    source_artifact = workspace / "wiki" / "sources" / f"{summary_slug}-pageindex.md"
    source_lines = [
        f"# PageIndex Source: {document.name}",
        "",
        f"- page_count: {indexed.page_count}",
        f"- artifact: `{_relative_ref(workspace, Path(indexed.artifact_path))}`",
        "",
        "## Structure",
    ]
    for node in structure:
        title = str(node.get("title") or "section")
        start = _to_int(node.get("start_page"), 0)
        end = _to_int(node.get("end_page"), 0)
        source_lines.append(f"- {title} (pages {start}-{end})")
    source_lines.append("")
    source_lines.append("## Page Excerpts")
    for item in pages:
        page_no = _to_int(item.get("page"), 0)
        excerpt = str(item.get("content") or "").strip()[:1200]
        source_lines.append(f"### Page {page_no}")
        source_lines.append(excerpt or "(no text)")
        source_lines.append("")
    source_artifact.parent.mkdir(parents=True, exist_ok=True)
    source_artifact.write_text("\n".join(source_lines).strip() + "\n", encoding="utf-8")

    seed_path = workspace / "wiki" / "sources" / f"{summary_slug}-summary-seed.md"
    seed_lines = [
        f"# Long Document Summary Seed: {document.name}",
        "",
        "## Structural Overview",
    ]
    for node in structure:
        summary = str(node.get("summary") or "").strip()
        title = str(node.get("title") or "section")
        if summary:
            seed_lines.append(f"- {title}: {summary}")
        else:
            seed_lines.append(f"- {title}")
    seed_lines.append("")
    seed_lines.append("## Representative Excerpts")
    for item in pages[:8]:
        page_no = _to_int(item.get("page"), 0)
        excerpt = str(item.get("content") or "").strip()[:700]
        if excerpt:
            seed_lines.append(f"- page {page_no}: {excerpt}")
    seed_path.write_text("\n".join(seed_lines).strip() + "\n", encoding="utf-8")

    return _MaterializedDocument(
        document=document,
        summary_slug=summary_slug,
        source_ref=_relative_ref(workspace, source_artifact),
        text_for_summary=seed_path.read_text(encoding="utf-8", errors="ignore"),
        summary_seed_ref=_relative_ref(workspace, seed_path),
    )


def _summary_messages(doc_name: str, text: str, language: str) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "You are a compliance wiki compiler for a taxonomy-native knowledge base. "
                f"Write in {language}. Return JSON only."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Document name: {doc_name}\n\n"
                "Generate a concise summary result for this document.\n"
                "Fields:\n"
                "- document_brief: one sentence under 180 chars\n"
                "- summary_markdown: markdown summary suitable for wiki/summaries page\n\n"
                f"Source:\n{text}"
            ),
        },
    ]


def _summarize_document(
    *, model: str, language: str, materialized: _MaterializedDocument
) -> SummaryStageResult:
    return _structured_completion(
        model=model,
        messages=_summary_messages(
            doc_name=materialized.document.name,
            text=materialized.text_for_summary,
            language=language,
        ),
        response_model=SummaryStageResult,
    )


def _planner_messages(
    *,
    language: str,
    document_name: str,
    summary: SummaryStageResult,
    existing_briefs: dict[str, dict[str, str]],
) -> list[dict[str, str]]:
    existing_blob = json.dumps(existing_briefs, ensure_ascii=False)
    return [
        {
            "role": "system",
            "content": (
                "You produce taxonomy-native wiki action plans. "
                "No concept layer is allowed. "
                f"Write in {language}. Return JSON only."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Document: {document_name}\n"
                f"Brief: {summary.document_brief}\n\n"
                "Summary markdown:\n"
                f"{summary.summary_markdown}\n\n"
                "Existing wiki briefs by taxonomy:\n"
                f"{existing_blob}\n\n"
                "Return a JSON object with keys topics/regulations/procedures/conflicts/evidence.\n"
                "Each of topics/regulations/procedures/conflicts uses:\n"
                "{create:[{slug,title,brief}], update:[{slug,title,brief}], related:[slug]}.\n"
                "Evidence is a list of {claim, quote, anchor, normalized_claim}.\n"
                "Keep actions concise and deterministic."
            ),
        },
    ]


def _normalize_plan_item(item: PagePlanItem) -> PagePlanItem:
    slug = _slugify(item.slug or item.title)
    title = item.title.strip() or slug.replace("-", " ").title()
    brief = item.brief.strip()
    return PagePlanItem(slug=slug, title=title, brief=brief)


def _sanitize_plan(plan: TaxonomyPlanResult) -> TaxonomyPlanResult:
    for actions in [plan.topics, plan.regulations, plan.procedures, plan.conflicts]:
        actions.create = [_normalize_plan_item(item) for item in actions.create]
        actions.update = [_normalize_plan_item(item) for item in actions.update]
        actions.related = [
            _slugify(str(item)) for item in actions.related if str(item).strip()
        ]
    normalized_evidence: list[EvidencePlanItem] = []
    for item in plan.evidence:
        claim = item.claim.strip()
        if not claim:
            continue
        normalized_evidence.append(
            EvidencePlanItem(
                claim=claim,
                quote=item.quote.strip(),
                anchor=item.anchor.strip(),
                normalized_claim=_normalize_claim_key(item.normalized_claim or claim),
            )
        )
    plan.evidence = normalized_evidence
    return plan


def _plan_taxonomy(
    *,
    model: str,
    language: str,
    materialized: _MaterializedDocument,
    summary: SummaryStageResult,
    existing_briefs: dict[str, dict[str, str]],
) -> TaxonomyPlanResult:
    """Build taxonomy actions for one summary while avoiding duplicate slugs."""
    plan = _structured_completion(
        model=model,
        messages=_planner_messages(
            language=language,
            document_name=materialized.document.name,
            summary=summary,
            existing_briefs=existing_briefs,
        ),
        response_model=TaxonomyPlanResult,
    )
    return _sanitize_plan(plan)


_MANAGED_PAGE_SECTION_HEADINGS = [
    "Source Summaries",
    "Related Conflicts",
    "Related Evidence",
]


def _strip_managed_sections(body: str) -> str:
    """Remove compiler-managed backlink/provenance sections before LLM rewrites."""
    cleaned = body
    for heading in _MANAGED_PAGE_SECTION_HEADINGS:
        pattern = re.compile(
            rf"^## {re.escape(heading)}\n.*?(?=^## |\Z)",
            re.MULTILINE | re.DOTALL,
        )
        cleaned = pattern.sub("", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned + "\n" if cleaned else ""


def _page_draft_messages(
    *,
    language: str,
    page_type: str,
    document_name: str,
    summary: SummaryStageResult,
    item: PagePlanItem,
    is_update: bool,
    existing_body: str,
    body_guidance: str,
    field_guide: str,
) -> list[dict[str, str]]:
    action = "Rewrite" if is_update else "Draft"
    existing_block = ""
    if is_update:
        existing_block = (
            "Current page body "
            "(compiler-managed backlink/provenance sections removed):\n"
            f"{existing_body or '(page missing - draft from scratch)'}\n\n"
        )
    return [
        {
            "role": "system",
            "content": (
                "You write taxonomy-native compliance wiki pages. "
                f"Write in {language}. Return JSON only."
            ),
        },
        {
            "role": "user",
            "content": (
                f"{action} a {page_type} page.\n"
                f"Document: {document_name}\n"
                f"Target slug: {item.slug}\n"
                f"Target title: {item.title}\n"
                f"Planner brief: {item.brief or summary.document_brief}\n"
                f"Source summary brief: {summary.document_brief}\n\n"
                "Source summary markdown:\n"
                f"{summary.summary_markdown}\n\n"
                f"{existing_block}"
                f"{body_guidance}\n\n"
                "Rules:\n"
                "- Do not include YAML frontmatter.\n"
                "- Do not include the top-level page title heading in body fields.\n"
                "- Do not write Source Summaries, Related Conflicts, or Related Evidence sections; code manages those.\n"
                "- Keep the page grounded in the provided summary and planner intent.\n\n"
                "Return a JSON object with these fields:\n"
                f"{field_guide}"
            ),
        },
    ]


def _summary_link(summary_slug: str) -> str:
    """Return a deterministic wiki reference for one summary slug."""
    return f"[[summaries/{summary_slug}]]"


def _render_topic_page(output: TopicPageOutput) -> str:
    """Render a deterministic topic page markdown body."""
    return f"# Topic: {output.title}\n\n{output.context_markdown.strip()}\n"


def _render_regulation_page(output: RegulationPageOutput) -> str:
    """Render a deterministic regulation page markdown body."""
    return (
        f"# Regulation: {output.title}\n\n"
        "## Requirement\n"
        f"{output.requirement_markdown.strip()}\n\n"
        "## Applicability\n"
        f"{output.applicability_markdown.strip()}\n\n"
        "## Authority and Provenance\n"
        f"{output.authority_markdown.strip()}\n"
    )


def _render_procedure_page(output: ProcedurePageOutput) -> str:
    """Render procedure steps in ordered markdown list form."""
    lines = [f"# Procedure: {output.title}", "", output.brief.strip(), "", "## Steps"]
    for index, step in enumerate(output.steps, start=1):
        lines.append(f"{index}. {step}")
    return "\n".join(lines).strip() + "\n"


def _render_conflict_page(output: ConflictPageOutput) -> str:
    """Render conflict details with stable impacted-page rendering."""
    lines = [
        f"# Conflict: {output.title}",
        "",
        output.description_markdown.strip(),
    ]
    if output.impacted_pages:
        lines.extend(["", "## Impacted Pages"])
        lines.extend(f"- {item}" for item in sorted(set(output.impacted_pages)))
    return "\n".join(lines).strip() + "\n"


def _draft_topic(summary: SummaryStageResult, item: PagePlanItem) -> TopicPageOutput:
    """Draft topic content from planner context and source summary."""
    brief = item.brief or summary.document_brief
    context = summary.summary_markdown.strip()
    return TopicPageOutput(title=item.title, brief=brief, context_markdown=context)


def _draft_regulation(
    summary: SummaryStageResult, item: PagePlanItem
) -> RegulationPageOutput:
    """Draft regulation content from planner item and fallback summary values."""
    brief = item.brief or summary.document_brief
    return RegulationPageOutput(
        title=item.title,
        brief=brief,
        requirement_markdown=brief,
        applicability_markdown="Applies to contexts described in the source summary.",
        authority_markdown="Derived from source document evidence and summary synthesis.",
    )


def _draft_procedure(
    summary: SummaryStageResult, item: PagePlanItem
) -> ProcedurePageOutput:
    """Draft procedure content from planner intent and summary."""
    brief = item.brief or summary.document_brief
    steps = [
        "Review requirement and scope from linked regulations.",
        "Execute ordered actions according to source summary.",
        "Record completion evidence and escalate any conflict.",
    ]
    return ProcedurePageOutput(title=item.title, brief=brief, steps=steps)


def _draft_conflict(
    summary: SummaryStageResult, item: PagePlanItem
) -> ConflictPageOutput:
    """Draft conflict content from planner intent and source summary."""
    brief = item.brief or summary.document_brief
    return ConflictPageOutput(
        title=item.title,
        brief=brief,
        description_markdown=brief,
        impacted_pages=[],
    )


async def _draft_topic_page(
    *,
    model: str,
    language: str,
    document_name: str,
    summary: SummaryStageResult,
    item: PagePlanItem,
    is_update: bool,
    existing_body: str,
) -> TopicPageOutput:
    """Draft or rewrite one topic page with structured LLM output."""
    try:
        return await _structured_acompletion(
            model=model,
            messages=_page_draft_messages(
                language=language,
                page_type="topic",
                document_name=document_name,
                summary=summary,
                item=item,
                is_update=is_update,
                existing_body=existing_body,
                body_guidance=(
                    "Focus on explanatory synthesis: what the topic is, why it "
                    "matters, and the context from the source summary."
                ),
                field_guide=(
                    "- title: page title\n"
                    "- brief: one-sentence definition under 180 chars\n"
                    "- context_markdown: the topic body in markdown, without a top-level heading"
                ),
            ),
            response_model=TopicPageOutput,
        )
    except Exception:
        return _draft_topic(summary, item)


async def _draft_regulation_page(
    *,
    model: str,
    language: str,
    document_name: str,
    summary: SummaryStageResult,
    item: PagePlanItem,
    is_update: bool,
    existing_body: str,
) -> RegulationPageOutput:
    """Draft or rewrite one regulation page with structured LLM output."""
    try:
        return await _structured_acompletion(
            model=model,
            messages=_page_draft_messages(
                language=language,
                page_type="regulation",
                document_name=document_name,
                summary=summary,
                item=item,
                is_update=is_update,
                existing_body=existing_body,
                body_guidance=(
                    "Focus on binding requirements, applicability, and authority "
                    "or provenance from the source summary."
                ),
                field_guide=(
                    "- title: page title\n"
                    "- brief: one-sentence requirement summary under 180 chars\n"
                    "- requirement_markdown: normative requirement details\n"
                    "- applicability_markdown: who or what contexts the regulation applies to\n"
                    "- authority_markdown: authority, exceptions, or provenance details"
                ),
            ),
            response_model=RegulationPageOutput,
        )
    except Exception:
        return _draft_regulation(summary, item)


async def _draft_procedure_page(
    *,
    model: str,
    language: str,
    document_name: str,
    summary: SummaryStageResult,
    item: PagePlanItem,
    is_update: bool,
    existing_body: str,
) -> ProcedurePageOutput:
    """Draft or rewrite one procedure page with structured LLM output."""
    try:
        return await _structured_acompletion(
            model=model,
            messages=_page_draft_messages(
                language=language,
                page_type="procedure",
                document_name=document_name,
                summary=summary,
                item=item,
                is_update=is_update,
                existing_body=existing_body,
                body_guidance=(
                    "Focus on operational execution. Return clear ordered steps as "
                    "plain step strings, not numbered markdown."
                ),
                field_guide=(
                    "- title: page title\n"
                    "- brief: one-sentence workflow summary under 180 chars\n"
                    "- steps: ordered list of action strings"
                ),
            ),
            response_model=ProcedurePageOutput,
        )
    except Exception:
        return _draft_procedure(summary, item)


async def _draft_conflict_page(
    *,
    model: str,
    language: str,
    document_name: str,
    summary: SummaryStageResult,
    item: PagePlanItem,
    is_update: bool,
    existing_body: str,
) -> ConflictPageOutput:
    """Draft or rewrite one planner-generated conflict page with structured LLM output."""
    try:
        return await _structured_acompletion(
            model=model,
            messages=_page_draft_messages(
                language=language,
                page_type="conflict",
                document_name=document_name,
                summary=summary,
                item=item,
                is_update=is_update,
                existing_body=existing_body,
                body_guidance=(
                    "Describe the mismatch clearly. Include impacted wiki links in "
                    "impacted_pages only when they are explicit from the context; "
                    "otherwise return an empty list."
                ),
                field_guide=(
                    "- title: page title\n"
                    "- brief: one-sentence conflict summary under 180 chars\n"
                    "- description_markdown: conflict explanation in markdown\n"
                    "- impacted_pages: list of wiki links such as [[regulations/foo]], or []"
                ),
            ),
            response_model=ConflictPageOutput,
        )
    except Exception:
        return _draft_conflict(summary, item)


def _upsert_typed_page(
    *,
    path: Path,
    page_type: str,
    title: str,
    brief: str,
    body: str,
    summary_link: str,
) -> Path:
    """Create or update a typed wiki page while preserving source-summary links.

    Existing metadata is merged so repeated runs do not lose prior provenance.
    """
    existing_meta, _ = _read_page(path)
    summary_candidates = _list_from_meta(existing_meta.get("source_summaries"))
    if summary_link.strip():
        summary_candidates.append(summary_link)
    summaries = sorted(set(summary_candidates))
    meta: dict[str, object] = {
        "page_id": str(existing_meta.get("page_id") or f"{page_type}:{path.stem}"),
        "page_type": page_type,
        "title": str(existing_meta.get("title") or title),
        "brief": brief,
        "source_summaries": summaries,
    }
    with_links = _ensure_links_in_section(body, "Source Summaries", summaries)
    return _write_page(path, meta, with_links)


def _add_related_summary(path: Path, summary_link: str) -> None:
    """Append one source-summary backlink onto an existing page."""
    if not path.exists():
        return
    meta, body = _read_page(path)
    summary_candidates = _list_from_meta(meta.get("source_summaries"))
    if summary_link.strip():
        summary_candidates.append(summary_link)
    summaries = sorted(set(summary_candidates))
    meta["source_summaries"] = summaries
    body = _ensure_links_in_section(body, "Source Summaries", summaries)
    _write_page(path, meta, body)


def _tokenize_subject(value: str) -> set[str]:
    tokens = {
        token
        for token in re.findall(r"[a-zA-Z0-9]{3,}", value.lower())
        if token not in _STOPWORDS
    }
    return tokens


def _extract_title(meta: dict[str, object], body: str, fallback: str) -> str:
    title = str(meta.get("title") or "").strip()
    if title:
        return title
    for line in body.splitlines():
        if line.startswith("#"):
            stripped = line.lstrip("#").strip()
            if stripped:
                return stripped
    return fallback


def _confirm_conflict(
    *,
    model: str,
    language: str,
    left_title: str,
    right_title: str,
    left_text: str,
    right_text: str,
) -> ConflictCheckResult:
    messages = [
        {
            "role": "system",
            "content": (
                "You confirm whether two compliance statements conflict. "
                f"Write in {language}. Return JSON only."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Page A: {left_title}\n{left_text[:1200]}\n\n"
                f"Page B: {right_title}\n{right_text[:1200]}\n\n"
                "Return {is_conflict:boolean,title:string,description:string}."
            ),
        },
    ]
    try:
        return _structured_completion(
            model=model,
            messages=messages,
            response_model=ConflictCheckResult,
        )
    except ValidationError:
        left_lower = left_text.lower()
        right_lower = right_text.lower()
        mismatch = (
            ("must not" in left_lower and "must" in right_lower)
            or ("must" in left_lower and "must not" in right_lower)
            or ("prohibited" in left_lower and "required" in right_lower)
            or ("required" in left_lower and "prohibited" in right_lower)
        )
        if mismatch:
            return ConflictCheckResult(
                is_conflict=True,
                title=f"{left_title} vs {right_title}",
                description="Potential opposing requirements detected.",
            )
        return ConflictCheckResult(is_conflict=False)


def _brief_for_index(path: Path) -> str:
    """Return the short index snippet for a wiki page.

    We prefer explicit frontmatter `brief` metadata because it stays stable even
    when the markdown body is long or changes shape.
    """
    meta, body = _read_page(path)
    brief = str(meta.get("brief") or meta.get("document_brief") or "").strip()
    if brief:
        return brief[:180]
    return _derive_brief(body)


def _write_summary_page(
    *,
    workspace: Path,
    materialized: _MaterializedDocument,
    summary: SummaryStageResult,
) -> Path:
    """Write a deterministic summary page for one materialized document."""
    path = workspace / "wiki" / "summaries" / f"{materialized.summary_slug}.md"
    meta: dict[str, object] = {
        "page_id": f"summary:{materialized.document.file_hash}",
        "page_type": "summary",
        "document_hash": materialized.document.file_hash,
        "document_name": materialized.document.name,
        "document_type": materialized.document.file_type,
        "source_location": materialized.source_ref,
        "summary_seed": materialized.summary_seed_ref,
        "brief": summary.document_brief,
    }
    body = (
        f"# Summary: {materialized.document.name}\n\n"
        f"{summary.summary_markdown.strip()}\n"
    )
    # Create an empty Derived Pages section now so later backlink steps can safely
    # replace that section without needing to handle missing anchors.
    body = _ensure_links_in_section(body, "Derived Pages", [])
    return _write_page(path, meta, body)


def _apply_actions(
    *,
    workspace: Path,
    page_type: str,
    actions: PagePlanActions,
    model: str,
    language: str,
    document_name: str,
    summary_slug: str,
    summary: SummaryStageResult,
    artifacts_bucket: list[Path],
    summary_to_links: dict[str, set[str]],
    summary_to_pages: dict[str, set[Path]],
    draft_create_update: DraftCreateUpdateFn[RenderModelT],
    render_markdown: Callable[[RenderModelT], str],
) -> list[Path]:
    """Apply one page-type action batch generated by taxonomy planning.

    `create` and `update` draft or rewrite typed pages from this summary,
    while `related` only appends provenance onto existing pages.
    """
    # Centralized writer path so artifact tracking and backlink maps are updated
    # consistently for any page type.
    touched: list[Path] = []
    summary_link = _summary_link(summary_slug)
    draft_specs: list[tuple[PagePlanItem, bool, Path, str]] = []

    # 1) Materialize explicit create/update actions for this page type.
    for is_update, items in ((False, actions.create), (True, actions.update)):
        for item in items:
            slug = _slugify(item.slug or item.title)
            path = workspace / "wiki" / page_type / f"{slug}.md"
            existing_body = ""
            if is_update:
                _, current_body = _read_page(path)
                existing_body = _strip_managed_sections(current_body)
            draft_specs.append((item, is_update, path, existing_body))

    drafted_outputs: list[RenderModelT] = []
    if draft_specs:

        async def _draft_batch() -> list[RenderModelT]:
            semaphore = asyncio.Semaphore(_DRAFT_CONCURRENCY)

            async def _draft_one(
                item: PagePlanItem,
                is_update: bool,
                existing_body: str,
            ) -> RenderModelT:
                async with semaphore:
                    return await draft_create_update(
                        model=model,
                        language=language,
                        document_name=document_name,
                        summary=summary,
                        item=item,
                        is_update=is_update,
                        existing_body=existing_body,
                    )

            return await asyncio.gather(
                *[
                    _draft_one(item, is_update, existing_body)
                    for item, is_update, _, existing_body in draft_specs
                ]
            )

        drafted_outputs = asyncio.run(_draft_batch())

    for (item, _is_update, path, _existing_body), drafted in zip(
        draft_specs, drafted_outputs
    ):
        slug = _slugify(item.slug or item.title)
        # Drafts are generated from structured models so frontmatter/body shapes stay
        # consistent across re-runs and different LLM outputs.
        body = render_markdown(drafted)
        brief = str(getattr(drafted, "brief", item.brief or summary.document_brief))
        title = str(getattr(drafted, "title", item.title))
        # Upsert keeps provenance by accumulating all source summaries that touched
        # the same page.
        written = _upsert_typed_page(
            path=path,
            page_type=page_type,
            title=title,
            brief=brief,
            body=body,
            summary_link=summary_link,
        )
        _append_unique(artifacts_bucket, written)
        touched.append(written)
        # Track reverse-linking targets for final backlink patching.
        summary_to_links[summary_slug].add(f"[[{page_type}/{slug}]]")
        summary_to_pages[summary_slug].add(written)

    # 2) `related` entries are existing canonical pages to associate with this
    #    document, without creating duplicate new files.
    for slug in actions.related:
        path = workspace / "wiki" / page_type / f"{slug}.md"
        if not path.exists():
            continue
        _add_related_summary(path, summary_link)
        touched.append(path)
        summary_to_links[summary_slug].add(f"[[{page_type}/{slug}]]")
        summary_to_pages[summary_slug].add(path)

    return touched


def compile_documents(
    workspace: Path,
    documents: list[DocumentRecord],
    *,
    provider: str,
    model: str,
    api_key: str,
    language: str,
    stage_callback: StageCallback | None = None,
) -> CompileArtifacts:
    """Compile documents into taxonomy-native wiki pages for Milestone 2."""
    # ASCII pipeline snapshot (for reviewer at one glance):
    #
    # docs
    #  |
    #  v
    # materialize
    #  |
    #  v
    # summarize -> write summaries
    #  |
    #  v
    # plan taxonomy actions
    #  |
    #  +-- topics
    #  +-- regulations
    #  +-- procedures
    #  +-- conflicts (planner + cross-doc detection)
    #  +-- evidence (claim merge)
    #        |
    #        v
    # backlink synthesis (summaries/derived/conflicts/evidence)
    #        |
    #        v
    # return artifacts
    #
    artifacts = CompileArtifacts()
    materialized_docs: list[_MaterializedDocument] = []
    summaries_by_hash: dict[str, SummaryStageResult] = {}
    summary_slug_by_hash: dict[str, str] = {}
    summary_to_links: dict[str, set[str]] = defaultdict(set)
    summary_to_pages: dict[str, set[Path]] = defaultdict(set)
    related_conflicts_by_page: dict[Path, set[str]] = defaultdict(set)
    related_evidence_by_page: dict[Path, set[str]] = defaultdict(set)

    with provider_env(provider, api_key):
        # Step 1: materialize source artifacts for both short and long documents.
        _emit_stage(
            stage_callback, "indexing-long-docs", "Materializing source artifacts"
        )
        for document in documents:
            if document.requires_pageindex:
                materialized = _materialize_long_document(
                    workspace, document, artifacts
                )
            else:
                materialized = _materialize_short_document(workspace, document)
            materialized_docs.append(materialized)

        # Step 2: generate typed summaries and persist summary pages.
        _emit_stage(stage_callback, "summarizing", "Generating typed summaries")
        for materialized in materialized_docs:
            summary = _summarize_document(
                model=model,
                language=language,
                materialized=materialized,
            )
            summaries_by_hash[materialized.document.file_hash] = summary
            summary_slug_by_hash[materialized.document.file_hash] = (
                materialized.summary_slug
            )
            summary_page = _write_summary_page(
                workspace=workspace,
                materialized=materialized,
                summary=summary,
            )
            _append_unique(artifacts.summaries, summary_page)

        # Step 3: create taxonomy-native plans from summaries.
        _emit_stage(stage_callback, "planning-taxonomy", "Planning taxonomy actions")
        plans_by_hash: dict[str, TaxonomyPlanResult] = {}
        existing_briefs = {
            "topics": _existing_page_briefs(workspace / "wiki", "topics"),
            "regulations": _existing_page_briefs(workspace / "wiki", "regulations"),
            "procedures": _existing_page_briefs(workspace / "wiki", "procedures"),
            "conflicts": _existing_page_briefs(workspace / "wiki", "conflicts"),
            "evidence": _existing_page_briefs(workspace / "wiki", "evidence"),
        }
        for materialized in materialized_docs:
            summary = summaries_by_hash[materialized.document.file_hash]
            plan = _plan_taxonomy(
                model=model,
                language=language,
                materialized=materialized,
                summary=summary,
                existing_briefs=existing_briefs,
            )
            plans_by_hash[materialized.document.file_hash] = plan

        # Step 4: draft and write topic pages.
        _emit_stage(
            stage_callback, "writing-topics", "Drafting and writing topic pages"
        )
        for materialized in materialized_docs:
            doc_hash = materialized.document.file_hash
            _apply_actions(
                workspace=workspace,
                page_type="topics",
                model=model,
                language=language,
                document_name=materialized.document.name,
                actions=plans_by_hash[doc_hash].topics,
                summary_slug=summary_slug_by_hash[doc_hash],
                summary=summaries_by_hash[doc_hash],
                artifacts_bucket=artifacts.topics,
                summary_to_links=summary_to_links,
                summary_to_pages=summary_to_pages,
                draft_create_update=_draft_topic_page,
                render_markdown=_render_topic_page,
            )

        # Step 5: draft and write regulation pages.
        _emit_stage(
            stage_callback,
            "writing-regulations",
            "Drafting and writing regulation pages",
        )
        touched_regulations: list[Path] = []
        for materialized in materialized_docs:
            doc_hash = materialized.document.file_hash
            touched = _apply_actions(
                workspace=workspace,
                page_type="regulations",
                model=model,
                language=language,
                document_name=materialized.document.name,
                actions=plans_by_hash[doc_hash].regulations,
                summary_slug=summary_slug_by_hash[doc_hash],
                summary=summaries_by_hash[doc_hash],
                artifacts_bucket=artifacts.regulations,
                summary_to_links=summary_to_links,
                summary_to_pages=summary_to_pages,
                draft_create_update=_draft_regulation_page,
                render_markdown=_render_regulation_page,
            )
            touched_regulations.extend(touched)

        # Step 6: draft and write procedure pages.
        _emit_stage(
            stage_callback,
            "writing-procedures",
            "Drafting and writing procedure pages",
        )
        touched_procedures: list[Path] = []
        for materialized in materialized_docs:
            doc_hash = materialized.document.file_hash
            touched = _apply_actions(
                workspace=workspace,
                page_type="procedures",
                model=model,
                language=language,
                document_name=materialized.document.name,
                actions=plans_by_hash[doc_hash].procedures,
                summary_slug=summary_slug_by_hash[doc_hash],
                summary=summaries_by_hash[doc_hash],
                artifacts_bucket=artifacts.procedures,
                summary_to_links=summary_to_links,
                summary_to_pages=summary_to_pages,
                draft_create_update=_draft_procedure_page,
                render_markdown=_render_procedure_page,
            )
            touched_procedures.extend(touched)

        # Step 7: draft planner conflicts and run incremental cross-document checks.
        _emit_stage(
            stage_callback,
            "writing-conflicts",
            "Drafting and writing conflict pages",
        )
        for materialized in materialized_docs:
            doc_hash = materialized.document.file_hash
            touched_conflicts = _apply_actions(
                workspace=workspace,
                page_type="conflicts",
                model=model,
                language=language,
                document_name=materialized.document.name,
                actions=plans_by_hash[doc_hash].conflicts,
                summary_slug=summary_slug_by_hash[doc_hash],
                summary=summaries_by_hash[doc_hash],
                artifacts_bucket=artifacts.conflicts,
                summary_to_links=summary_to_links,
                summary_to_pages=summary_to_pages,
                draft_create_update=_draft_conflict_page,
                render_markdown=_render_conflict_page,
            )
            for conflict_page in touched_conflicts:
                conflict_link = f"[[conflicts/{conflict_page.stem}]]"
                for derived in summary_to_pages[summary_slug_by_hash[doc_hash]]:
                    related_conflicts_by_page[derived].add(conflict_link)

        # Candidate pool for incremental conflict detection:
        # - compare only regulations/procedures pages (M2 scope)
        # - prioritize pages touched in this compile run as the left side
        all_reg_proc = sorted(
            set((workspace / "wiki" / "regulations").glob("*.md"))
            | set((workspace / "wiki" / "procedures").glob("*.md"))
        )
        touched_for_detection = sorted(set(touched_regulations + touched_procedures))
        # Use canonical ordered pairs so (A,B) and (B,A) are evaluated once.
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
                pair: tuple[str, str] = (
                    (left_key, right_key)
                    if left_key <= right_key
                    else (right_key, left_key)
                )
                if pair in checked_pairs:
                    continue
                checked_pairs.add(pair)

                right_meta, right_body = _read_page(right_path)
                right_title = _extract_title(right_meta, right_body, right_path.stem)
                # Cheap code-side narrowing before calling the LLM:
                # require at least 2 overlapping subject tokens.
                overlap = left_tokens & _tokenize_subject(right_title)
                if len(overlap) < 2:
                    continue

                # LLM confirms whether this shortlisted pair is a real conflict.
                decision = _confirm_conflict(
                    model=model,
                    language=language,
                    left_title=left_title,
                    right_title=right_title,
                    left_text=left_body,
                    right_text=right_body,
                )
                if not decision.is_conflict:
                    continue

                conflict_title = (
                    decision.title.strip() or f"{left_title} vs {right_title}"
                )
                suffix = hashlib.sha1((pair[0] + pair[1]).encode("utf-8")).hexdigest()[
                    :6
                ]
                conflict_slug = f"{_slugify(conflict_title)}-{suffix}"
                conflict_path = workspace / "wiki" / "conflicts" / f"{conflict_slug}.md"
                left_target = _relative_ref(
                    workspace / "wiki", left_path.with_suffix("")
                )
                right_target = _relative_ref(
                    workspace / "wiki", right_path.with_suffix("")
                )
                impacted = [
                    f"[[{left_target}]]",
                    f"[[{right_target}]]",
                ]
                source_links = sorted(
                    set(_list_from_meta(left_meta.get("source_summaries")))
                    | set(_list_from_meta(right_meta.get("source_summaries")))
                )
                # Write/update one conflict page for this pair, then merge all
                # source summary links so provenance remains complete.
                conflict_output = ConflictPageOutput(
                    title=conflict_title,
                    brief=decision.description.strip()
                    or "Potential conflict identified.",
                    description_markdown=decision.description.strip()
                    or "Potential contradiction identified across compiled pages.",
                    impacted_pages=impacted,
                )
                written = _upsert_typed_page(
                    path=conflict_path,
                    page_type="conflicts",
                    title=conflict_output.title,
                    brief=conflict_output.brief,
                    body=_render_conflict_page(conflict_output),
                    summary_link=source_links[0] if source_links else "",
                )
                if source_links:
                    meta, body = _read_page(written)
                    merged_links = sorted(
                        set(
                            _list_from_meta(meta.get("source_summaries")) + source_links
                        )
                    )
                    meta["source_summaries"] = merged_links
                    body = _ensure_links_in_section(
                        body, "Source Summaries", merged_links
                    )
                    _write_page(written, meta, body)
                _append_unique(artifacts.conflicts, written)

                # Backlink bookkeeping:
                # - mark both compared pages as related to this conflict
                # - add conflict link to each involved summary's Derived Pages
                conflict_link = f"[[conflicts/{written.stem}]]"
                related_conflicts_by_page[left_path].add(conflict_link)
                related_conflicts_by_page[right_path].add(conflict_link)
                for source_link in source_links:
                    match = re.search(r"\[\[summaries/([^\]]+)\]\]", source_link)
                    if match:
                        summary_to_links[match.group(1)].add(conflict_link)

        # Step 8: merge evidence entries by normalized claim key.
        # Planner outputs can emit equivalent evidence claims phrased differently.
        # Hashing by a canonical key collapses those duplicates before writing.
        _emit_stage(
            stage_callback, "writing-evidence", "Merging claim-centric evidence"
        )
        evidence_map: dict[str, _EvidenceAggregate] = {}
        for materialized in materialized_docs:
            doc_hash = materialized.document.file_hash
            summary_slug = summary_slug_by_hash[doc_hash]
            summary_link = _summary_link(summary_slug)
            plan = plans_by_hash[doc_hash]
            # Attach each evidence item back to its source summary for later
            # provenance links, then group all evidence for the same claim.
            for evidence_item in plan.evidence:
                claim = evidence_item.claim.strip()
                if not claim:
                    continue
                key = _normalize_claim_key(evidence_item.normalized_claim or claim)
                aggregate = evidence_map.setdefault(
                    key,
                    _EvidenceAggregate(claim=claim),
                )
                aggregate.source_summaries.add(summary_link)
                aggregate.quotes.append(
                    _EvidenceQuote(
                        quote=evidence_item.quote.strip(),
                        anchor=evidence_item.anchor.strip(),
                        summary_link=summary_link,
                    )
                )

        # Sort by claim key so reruns produce deterministic file names/order.
        for claim_key, aggregate in sorted(evidence_map.items()):
            claim = aggregate.claim
            source_links = sorted(aggregate.source_summaries)
            quotes = aggregate.quotes
            lines = [
                f"# Evidence: {claim}",
                "",
                "## Canonical Claim",
                claim,
                "",
                "## Supporting Quotes",
            ]
            # Keep a concise evidence section even when extractors return
            # empty quotes, which is easier to inspect and keeps downstream
            # rendering stable.
            if quotes:
                for entry in quotes:
                    quote = entry.quote.strip() or "(quote unavailable)"
                    anchor = entry.anchor.strip() or "unknown"
                    link = entry.summary_link
                    lines.extend(
                        [
                            f"> {quote}",
                            f"- source: {link}",
                            f"- anchor: `{anchor}`",
                            "",
                        ]
                    )
            else:
                lines.append("- (no supporting quotes)")

            evidence_path = workspace / "wiki" / "evidence" / f"{claim_key}.md"
            meta: dict[str, object] = {
                "page_id": f"evidence:{claim_key}",
                "page_type": "evidence",
                "claim_key": claim_key,
                "brief": _derive_brief(claim),
                "source_summaries": source_links,
            }
            body = _ensure_links_in_section(
                "\n".join(lines).strip() + "\n", "Source Summaries", source_links
            )
            written = _write_page(evidence_path, meta, body)
            _append_unique(artifacts.evidence, written)
            evidence_link = f"[[evidence/{claim_key}]]"
            # Add reverse backlinks so summary and derived pages can render both
            # "Derived Pages" and "Related Evidence" sections from this claim.
            for source_link in source_links:
                match = re.search(r"\[\[summaries/([^\]]+)\]\]", source_link)
                if match:
                    summary_slug = match.group(1)
                    summary_to_links[summary_slug].add(evidence_link)
                    for derived in summary_to_pages[summary_slug]:
                        related_evidence_by_page[derived].add(evidence_link)

        # Step 9: apply backlink sections on summaries and derived pages.
        _emit_stage(stage_callback, "backlinking", "Applying code-driven backlinks")
        for summary_path in artifacts.summaries:
            summary_slug = summary_path.stem
            meta, body = _read_page(summary_path)
            links = sorted(summary_to_links.get(summary_slug, set()))
            body = _ensure_links_in_section(body, "Derived Pages", links)
            _write_page(summary_path, meta, body)

        for page_path, links in related_conflicts_by_page.items():
            if not page_path.exists():
                continue
            meta, body = _read_page(page_path)
            body = _ensure_links_in_section(body, "Related Conflicts", sorted(links))
            _write_page(page_path, meta, body)

        for page_path, links in related_evidence_by_page.items():
            if not page_path.exists():
                continue
            meta, body = _read_page(page_path)
            body = _ensure_links_in_section(body, "Related Evidence", sorted(links))
            _write_page(page_path, meta, body)

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
            brief = _brief_for_index(page)
            if brief:
                lines.append(f"- [[{target}]] - {brief}")
            else:
                lines.append(f"- [[{target}]]")
        lines.append("")

    output = wiki / "index.md"
    output.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    return output
