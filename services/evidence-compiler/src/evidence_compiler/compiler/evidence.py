"""Evidence drafting, verification, manifest I/O, and page rendering."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Awaitable, Callable
from pathlib import Path

from knowledge_models.compiler_api import TokenUsageSummary

from evidence_compiler.compiler.llm import _structured_acompletion as _default_acompletion
from evidence_compiler.compiler.models import (
    EvidenceDocumentManifest,
    EvidenceDraftOutput,
    EvidencePlanItem,
    EvidenceValidationIssue,
    SummaryStageResult,
    VerifiedEvidenceInstance,
    _EvidencePageState,
    _MaterializedDocument,
)
from evidence_compiler.compiler.pages import _read_page
from evidence_compiler.compiler.planning import (
    _downstream_messages,
    _json_blob,
    _normalize_claim_key,
)
from evidence_compiler.compiler.summaries import _derive_brief

_EVIDENCE_DRAFT_MAX_TOKENS = 1536


def _summary_link(summary_slug: str) -> str:
    """Return a deterministic wiki reference for one summary slug."""
    return f"[[summaries/{summary_slug}]]"


def _evidence_manifest_path(workspace: Path, document_hash: str) -> Path:
    """Return the authoritative manifest path for one document's verified evidence."""
    return workspace / ".brain" / "evidence" / "by-document" / f"{document_hash}.json"


def _write_evidence_manifest(
    workspace: Path, manifest: EvidenceDocumentManifest
) -> Path:
    """Persist one per-document verified evidence manifest."""
    path = _evidence_manifest_path(workspace, manifest.document_hash)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(manifest.model_dump_json(indent=2), encoding="utf-8")
    return path


def _load_evidence_manifests(workspace: Path) -> list[EvidenceDocumentManifest]:
    """Load all persisted evidence manifests used as compiler source of truth."""
    manifest_dir = workspace / ".brain" / "evidence" / "by-document"
    if not manifest_dir.exists():
        return []
    manifests: list[EvidenceDocumentManifest] = []
    for path in sorted(manifest_dir.glob("*.json")):
        manifests.append(
            EvidenceDocumentManifest.model_validate_json(path.read_text(encoding="utf-8"))
        )
    return manifests


def _extract_section(body: str, heading: str) -> str:
    """Extract a second-level markdown section body without its heading line."""
    match = re.search(
        rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)",
        body,
        re.MULTILINE | re.DOTALL,
    )
    if not match:
        return ""
    return match.group(1).strip()


def _bootstrap_evidence_pages_from_wiki(
    workspace: Path,
) -> dict[str, _EvidencePageState]:
    """Recover evidence page identity from rendered wiki pages when manifests are absent."""
    evidence_dir = workspace / "wiki" / "evidence"
    if not evidence_dir.exists():
        return {}
    pages: dict[str, _EvidencePageState] = {}
    for path in sorted(evidence_dir.glob("*.md")):
        meta, body = _read_page(path)
        title = str(meta.get("title") or "").strip()
        brief = str(meta.get("brief") or "").strip()
        canonical_claim = _extract_section(body, "Canonical Claim") or title or path.stem
        claim_key = str(meta.get("claim_key") or _normalize_claim_key(canonical_claim)).strip()
        pages[path.stem] = _EvidencePageState(
            page_slug=path.stem,
            claim_key=claim_key,
            canonical_claim=canonical_claim,
            title=title or canonical_claim,
            brief=brief or _derive_brief(canonical_claim),
        )
    return pages


def _group_evidence_pages(
    manifests: list[EvidenceDocumentManifest], workspace: Path
) -> dict[str, _EvidencePageState]:
    """Merge manifest-backed evidence items into one canonical page state per slug."""
    grouped: dict[str, _EvidencePageState] = {}
    existing_meta_by_slug = _bootstrap_evidence_pages_from_wiki(workspace)
    for manifest in manifests:
        for item in manifest.items:
            if item.page_slug in grouped:
                continue
            existing = existing_meta_by_slug.get(item.page_slug)
            grouped[item.page_slug] = _EvidencePageState(
                page_slug=item.page_slug,
                claim_key=existing.claim_key if existing is not None else item.claim_key,
                canonical_claim=(
                    existing.canonical_claim
                    if existing is not None
                    else item.canonical_claim
                ),
                title=existing.title if existing is not None else item.title,
                brief=existing.brief if existing is not None else item.brief,
            )
    return grouped


def _existing_evidence_pages(workspace: Path) -> dict[str, _EvidencePageState]:
    """Load current evidence page identity, preferring manifests over rendered markdown."""
    manifests = _load_evidence_manifests(workspace)
    grouped = _group_evidence_pages(manifests, workspace)
    if grouped:
        return grouped
    return _bootstrap_evidence_pages_from_wiki(workspace)


def _document_evidence_briefs(
    instances: list[VerifiedEvidenceInstance],
) -> list[dict[str, str]]:
    """Project verified quote instances into compact planner-facing evidence briefs."""
    briefs: list[dict[str, str]] = []
    for item in sorted(instances, key=lambda value: value.evidence_id):
        briefs.append(
            {
                "evidence_id": item.evidence_id,
                "page_slug": item.page_slug,
                "title": item.title,
                "claim": item.canonical_claim,
                "brief": item.brief,
                "quote": item.quote,
                "anchor": item.anchor,
                "page_ref": item.page_ref,
                "summary_link": item.summary_link,
                "source_ref": item.source_ref,
            }
        )
    return briefs


def _collapse_whitespace(value: str) -> str:
    """Normalize whitespace for quote matching and stable id generation."""
    return " ".join(value.split())


def _quote_search_pattern(quote: str) -> re.Pattern[str] | None:
    """Build a whitespace-tolerant regex for matching a drafted quote in source text."""
    parts = [re.escape(part) for part in re.split(r"\s+", quote.strip()) if part]
    if not parts:
        return None
    return re.compile(r"\s+".join(parts), re.IGNORECASE)


def _line_range_for_span(text: str, start: int, end: int) -> tuple[int, int]:
    """Translate a character span into 1-based line numbers for fallback anchors."""
    start_line = text.count("\n", 0, start) + 1
    end_line = text.count("\n", 0, end) + 1
    return start_line, end_line


def _anchor_exists(source_text: str, anchor: str) -> bool:
    """Check whether a drafted anchor matches an actual heading or marker in source text."""
    target = _collapse_whitespace(anchor).lower().strip("`")
    if not target:
        return False
    for raw_line in source_text.splitlines():
        stripped = raw_line.strip()
        if not stripped:
            continue
        candidates = [
            stripped,
            stripped.lstrip("#").strip(),
            stripped.lstrip("-").strip(),
        ]
        if any(
            _collapse_whitespace(candidate).lower() == target
            for candidate in candidates
        ):
            return True
    return False


def _infer_anchor_from_match(source_text: str, match: re.Match[str]) -> tuple[str, str]:
    """Infer a concrete heading, page marker, or line range for a verified quote match."""
    lines = source_text.splitlines()
    start_line, end_line = _line_range_for_span(source_text, match.start(), match.end())
    for index in range(start_line - 1, -1, -1):
        stripped = lines[index].strip() if index < len(lines) else ""
        if not stripped:
            continue
        if stripped.startswith("### Page "):
            page_heading = stripped.lstrip("#").strip()
            page_number = page_heading.removeprefix("Page ").strip()
            return page_heading, f"page:{page_number}"
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip(), ""
    if start_line == end_line:
        return f"line:{start_line}", ""
    return f"line:{start_line}-{end_line}", ""


def _stable_evidence_id(
    document_hash: str,
    page_slug: str,
    quote: str,
    anchor: str,
    page_ref: str,
) -> str:
    """Derive a stable quote-instance id so reruns keep precise backlinks."""
    digest = hashlib.sha1(
        "\n".join(
            [
                document_hash,
                page_slug,
                _collapse_whitespace(quote),
                _collapse_whitespace(anchor),
                _collapse_whitespace(page_ref),
            ]
        ).encode("utf-8")
    ).hexdigest()[:12]
    return f"evidence:{document_hash[:8]}:{digest}"


def _verify_evidence_output(
    *,
    materialized: _MaterializedDocument,
    plan_item: EvidencePlanItem,
    draft: EvidenceDraftOutput,
) -> tuple[list[VerifiedEvidenceInstance], list[EvidenceValidationIssue]]:
    """Keep only quotes that can be grounded back into the materialized source text."""
    verified: dict[str, VerifiedEvidenceInstance] = {}
    dropped: list[EvidenceValidationIssue] = []
    canonical_claim = draft.claim.strip() or plan_item.claim
    title = draft.title.strip() or plan_item.title
    brief = draft.brief.strip() or plan_item.brief or _derive_brief(canonical_claim)
    claim_key = _normalize_claim_key(canonical_claim)
    summary_link = _summary_link(materialized.summary_slug)
    for quote_item in draft.quotes:
        raw_quote = quote_item.quote.strip()
        raw_anchor = quote_item.anchor.strip()
        raw_page_ref = quote_item.page_ref.strip()
        if not raw_quote:
            dropped.append(
                EvidenceValidationIssue(
                    page_slug=plan_item.page_slug,
                    claim=canonical_claim,
                    title=title,
                    quote="",
                    anchor=raw_anchor,
                    page_ref=raw_page_ref,
                    reason="empty quote",
                )
            )
            continue
        pattern = _quote_search_pattern(raw_quote)
        match = (
            pattern.search(materialized.text_for_downstream)
            if pattern is not None
            else None
        )
        if match is None:
            dropped.append(
                EvidenceValidationIssue(
                    page_slug=plan_item.page_slug,
                    claim=canonical_claim,
                    title=title,
                    quote=raw_quote,
                    anchor=raw_anchor,
                    page_ref=raw_page_ref,
                    reason="quote not found in source text",
                )
            )
            continue
        resolved_anchor = raw_anchor
        resolved_page_ref = raw_page_ref
        if not resolved_anchor or resolved_anchor.lower() == "unknown":
            resolved_anchor, inferred_page_ref = _infer_anchor_from_match(
                materialized.text_for_downstream, match
            )
            resolved_page_ref = resolved_page_ref or inferred_page_ref
        elif not _anchor_exists(materialized.text_for_downstream, resolved_anchor):
            resolved_anchor, inferred_page_ref = _infer_anchor_from_match(
                materialized.text_for_downstream, match
            )
            resolved_page_ref = resolved_page_ref or inferred_page_ref
        matched_quote = _collapse_whitespace(
            materialized.text_for_downstream[match.start() : match.end()]
        )
        evidence_id = _stable_evidence_id(
            materialized.document.file_hash,
            plan_item.page_slug,
            matched_quote,
            resolved_anchor,
            resolved_page_ref,
        )
        verified[evidence_id] = VerifiedEvidenceInstance(
            evidence_id=evidence_id,
            page_slug=plan_item.page_slug,
            claim_key=claim_key,
            canonical_claim=canonical_claim,
            title=title,
            brief=brief,
            quote=matched_quote,
            anchor=resolved_anchor,
            page_ref=resolved_page_ref,
            source_ref=materialized.downstream_source_ref,
            summary_link=summary_link,
            document_hash=materialized.document.file_hash,
        )
    return [verified[key] for key in sorted(verified)], dropped


def _draft_evidence_messages(
    *,
    language: str,
    materialized: _MaterializedDocument,
    summary: SummaryStageResult,
    item: EvidencePlanItem,
) -> list[dict[str, str]]:
    """Build the evidence-drafting prompt for one planned claim-centric page."""
    return _downstream_messages(
        language=language,
        purpose="Draft quote-level evidence instances.",
        materialized=materialized,
        summary=summary,
        assistant_blocks=[
            (
                "Evidence plan item",
                _json_blob(
                    {
                        "page_slug": item.page_slug,
                        "claim": item.claim,
                        "title": item.title,
                        "brief": item.brief,
                    }
                ),
            )
        ],
        user_instruction=(
            "Draft evidence quote instances. Return {claim,title,brief,quotes:[{quote,anchor,page_ref}]}. "
            "Quotes must be verbatim from the provided source text. Prefer anchors that match actual headings or page markers from the source. Do not write markdown."
        ),
    )


async def _draft_evidence(
    *,
    model: str,
    language: str,
    materialized: _MaterializedDocument,
    summary: SummaryStageResult,
    item: EvidencePlanItem,
    usage_callback: Callable[[TokenUsageSummary], None] | None = None,
    structured_acompletion: Callable[..., Awaitable[object]] = _default_acompletion,
) -> EvidenceDraftOutput:
    """Draft quote instances for one planned evidence page, with safe fallback."""
    try:
        result = await structured_acompletion(
            model=model,
            messages=_draft_evidence_messages(
                language=language,
                materialized=materialized,
                summary=summary,
                item=item,
            ),
            response_model=EvidenceDraftOutput,
            max_tokens=_EVIDENCE_DRAFT_MAX_TOKENS,
            usage_callback=usage_callback,
        )
        return EvidenceDraftOutput.model_validate(result)
    except Exception:
        return EvidenceDraftOutput(
            claim=item.claim,
            title=item.title,
            brief=item.brief or _derive_brief(item.claim),
            quotes=[],
        )


def _render_evidence_page(
    *,
    page_slug: str,
    page_state: _EvidencePageState,
    instances: list[VerifiedEvidenceInstance],
) -> tuple[dict[str, object], str]:
    """Render one public claim-centric evidence page from verified quote instances."""
    source_summaries = sorted({item.summary_link for item in instances})
    lines = [
        f"# Evidence: {page_state.title}",
        "",
        "## Canonical Claim",
        page_state.canonical_claim,
        "",
        "## Supporting Quotes",
    ]
    for item in sorted(instances, key=lambda value: (value.summary_link, value.evidence_id)):
        lines.extend(
            [
                f"> {item.quote}",
                f"- source: {item.summary_link}",
                *([f"- page: `{item.page_ref}`"] if item.page_ref else []),
                f"- anchor: `{item.anchor}`",
                "",
            ]
        )
    lines.extend(["## Source Summaries"])
    if source_summaries:
        lines.extend(f"- {summary_link}" for summary_link in source_summaries)
    else:
        lines.append("- (none)")
    meta: dict[str, object] = {
        "page_id": f"evidence:{page_slug}",
        "page_type": "evidence",
        "title": page_state.title,
        "claim_key": page_state.claim_key,
        "brief": page_state.brief,
        "source_summaries": source_summaries,
    }
    return meta, "\n".join(lines).strip() + "\n"


def _write_evidence_validation_report(
    workspace: Path, manifests: list[EvidenceDocumentManifest]
) -> Path:
    """Summarize dropped or unverifiable evidence items for reviewer inspection."""
    lines = ["# Evidence Validation Report", ""]
    any_dropped = False
    for manifest in sorted(manifests, key=lambda item: item.document_name.lower()):
        lines.append(f"## {manifest.document_name}")
        if not manifest.dropped:
            lines.append("- No dropped evidence items.")
            lines.append("")
            continue
        any_dropped = True
        for issue in manifest.dropped:
            lines.append(
                f"- `{issue.page_slug}`: {issue.reason}; quote=`{issue.quote or '(empty)'}`; anchor=`{issue.anchor or '(missing)'}`"
            )
        lines.append("")
    if not any_dropped:
        lines.append("All drafted evidence quotes verified successfully.")
        lines.append("")
    path = workspace / "wiki" / "reports" / "evidence-validation.md"
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    return path


def _remove_stale_evidence_pages(
    workspace: Path, authoritative_slugs: set[str]
) -> None:
    """Delete compiler-managed evidence pages absent from the latest render set."""
    evidence_dir = workspace / "wiki" / "evidence"
    if not evidence_dir.exists():
        return
    for path in evidence_dir.glob("*.md"):
        if path.stem not in authoritative_slugs:
            path.unlink()


__all__ = [
    "_EVIDENCE_DRAFT_MAX_TOKENS",
    "_anchor_exists",
    "_bootstrap_evidence_pages_from_wiki",
    "_collapse_whitespace",
    "_document_evidence_briefs",
    "_draft_evidence",
    "_draft_evidence_messages",
    "_evidence_manifest_path",
    "_existing_evidence_pages",
    "_extract_section",
    "_group_evidence_pages",
    "_infer_anchor_from_match",
    "_line_range_for_span",
    "_load_evidence_manifests",
    "_quote_search_pattern",
    "_remove_stale_evidence_pages",
    "_render_evidence_page",
    "_stable_evidence_id",
    "_summary_link",
    "_verify_evidence_output",
    "_write_evidence_manifest",
    "_write_evidence_validation_report",
]
