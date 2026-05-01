"""Compile-local schemas and dataclasses for the Milestone 2 pipeline."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, TypeVar

from pydantic import BaseModel, Field

from knowledge_models.compiler_api import DocumentRecord, TokenUsageSummary

DraftModelT = TypeVar("DraftModelT", bound=BaseModel, covariant=True)


class DraftCreateUpdateFn(Protocol[DraftModelT]):
    def __call__(
        self,
        *,
        model: str,
        language: str,
        materialized: _MaterializedDocument,
        summary: SummaryStageResult,
        item: PagePlanItem,
        evidence_pack: list[VerifiedEvidenceInstance],
        is_update: bool,
        existing_body: str,
        usage_callback: Callable[[TokenUsageSummary], None] | None = None,
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
    text_for_downstream: str
    downstream_source_ref: str
    summary_seed_ref: str | None = None


class SummaryStageResult(BaseModel):
    """Structured summary payload for one source document."""

    document_brief: str = Field(
        min_length=1,
        max_length=220,
        description=(
            "Write one concise sentence describing the source document for summary "
            "frontmatter and downstream planner context."
        ),
    )

    summary_markdown: str = Field(
        min_length=1,
        description=(
            "The full summary in Markdown. Include key concepts, findings, ideas."
        ),
    )


class PagePlanItem(BaseModel):
    """Planner-proposed create or update target for one taxonomy page."""

    slug: str = Field(
        description=(
            "Use the canonical wiki page slug for this target. Return lowercase "
            "kebab-case without any folder prefix."
        )
    )
    title: str = Field(description="Write the reader-facing title for this wiki page.")
    brief: str = Field(
        default="",
        description=(
            "Write one short sentence under 180 characters summarizing what the page "
            "covers. Return an empty string when no concise brief is justified."
        ),
    )
    candidate_evidence_ids: list[str] = Field(
        default_factory=list,
        description=(
            "List verified evidence ids that directly support this page. Use only ids "
            "provided in context and return [] when none apply."
        ),
    )


class PagePlanActions(BaseModel):
    """Planner actions for one taxonomy bucket."""

    create: list[PagePlanItem] = Field(
        default_factory=list,
        description="Pages that should be created as new wiki entries in this bucket.",
    )
    update: list[PagePlanItem] = Field(
        default_factory=list,
        description=(
            "Existing wiki pages that should be updated instead of recreated."
        ),
    )
    related: list[str] = Field(
        default_factory=list,
        description=(
            "Existing page slugs related to this document but not requiring create or "
            "update work."
        ),
    )


class EvidencePlanItem(BaseModel):
    """Claim-centric evidence page plan item."""

    page_slug: str = Field(
        default="",
        description=(
            "Use the canonical evidence page slug for this claim page. Return "
            "lowercase kebab-case without any folder prefix."
        ),
    )
    claim: str = Field(
        description="State the canonical claim this evidence page should support."
    )
    title: str = Field(
        description="Write the reader-facing title for the evidence page."
    )
    brief: str = Field(
        default="",
        description=(
            "Write one short sentence under 180 characters summarizing the claim. "
            "Return an empty string when no concise brief is justified."
        ),
    )


class EvidencePlanActions(BaseModel):
    """Planner actions for claim-centric evidence pages."""

    create: list[EvidencePlanItem] = Field(
        default_factory=list,
        description="Evidence pages that should be created as new claim records.",
    )
    update: list[EvidencePlanItem] = Field(
        default_factory=list,
        description="Existing evidence pages that should be updated for this compile.",
    )


class EvidenceDraftQuote(BaseModel):
    """One verbatim quote candidate for an evidence page."""

    quote: str = Field(
        description=(
            "Copy a verbatim supporting quote from the source text. Do not paraphrase "
            "or invent wording."
        )
    )
    anchor: str = Field(
        description=(
            "Name the closest source heading, section label, or page marker that helps "
            "a reviewer locate the quote."
        )
    )
    page_ref: str = Field(
        default="",
        description=(
            "Provide the explicit page reference when the source exposes one; "
            "otherwise return an empty string."
        ),
    )


class EvidenceDraftOutput(BaseModel):
    """Structured quote draft for one evidence page."""

    claim: str = Field(
        description="Carry forward the canonical claim for the evidence page being drafted."
    )
    title: str = Field(
        description="Carry forward the reader-facing title for the evidence page."
    )
    brief: str = Field(
        description=(
            "Carry forward a concise one-sentence brief for the evidence page under "
            "180 characters."
        )
    )
    quotes: list[EvidenceDraftQuote] = Field(
        default_factory=list,
        description=(
            "Provide verbatim supporting quote records for this claim. Return [] when "
            "no reliable quote can be extracted."
        ),
    )


class EvidenceValidationIssue(BaseModel):
    page_slug: str = Field(
        description="Evidence page slug that the dropped quote belonged to."
    )
    claim: str = Field(description="Canonical claim associated with the dropped quote.")
    title: str = Field(description="Reader-facing evidence page title.")
    quote: str = Field(description="Dropped quote text, or an empty string when absent.")
    anchor: str = Field(description="Drafted anchor associated with the dropped quote.")
    page_ref: str = Field(
        default="",
        description="Explicit source page reference when one was provided by the draft.",
    )
    reason: str = Field(description="Short reason explaining why verification dropped it.")


class VerifiedEvidenceInstance(BaseModel):
    evidence_id: str = Field(description="Stable quote-instance identifier for backlinks.")
    page_slug: str = Field(description="Canonical evidence page slug owning this quote.")
    claim_key: str = Field(description="Normalized claim key used to stabilize page identity.")
    canonical_claim: str = Field(description="Canonical claim that this quote supports.")
    title: str = Field(description="Reader-facing evidence page title.")
    brief: str = Field(description="Concise claim summary for frontmatter and planners.")
    quote: str = Field(description="Verified verbatim quote text found in the source.")
    anchor: str = Field(description="Verified anchor or inferred locator for the quote.")
    page_ref: str = Field(
        default="",
        description="Explicit page reference when the source provides one.",
    )
    source_ref: str = Field(description="Workspace-relative source artifact reference.")
    summary_link: str = Field(description="Wiki link back to the source summary page.")
    document_hash: str = Field(description="Document hash of the source artifact.")


class EvidenceDocumentManifest(BaseModel):
    document_hash: str = Field(description="Document hash that owns this evidence manifest.")
    document_name: str = Field(description="Original source document filename.")
    summary_slug: str = Field(description="Summary page slug for the source document.")
    source_ref: str = Field(description="Workspace-relative source artifact reference.")
    items: list[VerifiedEvidenceInstance] = Field(
        default_factory=list,
        description="Verified evidence instances retained for this document.",
    )
    dropped: list[EvidenceValidationIssue] = Field(
        default_factory=list,
        description="Dropped evidence draft items with concise verification reasons.",
    )


class TaxonomyPlanResult(BaseModel):
    """Structured taxonomy planning output grouped by page type."""

    topics: PagePlanActions = Field(
        default_factory=PagePlanActions,
        description=(
            "Topic page create, update, and related actions for stable descriptive "
            "subjects."
        ),
    )
    regulations: PagePlanActions = Field(
        default_factory=PagePlanActions,
        description=(
            "Regulation page create, update, and related actions for normative rules "
            "or restrictions."
        ),
    )
    procedures: PagePlanActions = Field(
        default_factory=PagePlanActions,
        description=(
            "Procedure page create, update, and related actions for explicit workflows "
            "or operational steps."
        ),
    )
    conflicts: PagePlanActions = Field(
        default_factory=PagePlanActions,
        description=(
            "Conflict page create, update, and related actions for real mismatches or "
            "disagreements."
        ),
    )


class TopicPageOutput(BaseModel):
    """Drafted content for a topic wiki page."""

    title: str = Field(
        description=(
            "Use the target topic title unless the source clearly supports a better "
            "canonical name."
        )
    )
    brief: str = Field(
        description=(
            "Write one concise sentence under 180 characters defining the stable "
            "subject of the page."
        )
    )
    context_markdown: str = Field(
        description=(
            "Write the topic body as multiline markdown that explains scope, key "
            "facts, and durable context. Do not include YAML frontmatter or a top-level "
            "# heading."
        )
    )
    used_evidence_ids: list[str] = Field(
        default_factory=list,
        description=(
            "List candidate evidence ids actually relied on in the drafted page. "
            "Return [] when none were used."
        ),
    )


class RegulationPageOutput(BaseModel):
    """Drafted content for a regulation wiki page."""

    title: str = Field(
        description=(
            "Use the target regulation title unless the source clearly supports a "
            "better canonical rule name."
        )
    )
    brief: str = Field(
        description=(
            "Write one concise sentence under 180 characters summarizing the binding "
            "rule or restriction."
        )
    )
    requirement_markdown: str = Field(
        description=(
            "Write the normative requirement itself as multiline markdown. Do not turn "
            "this field into procedural steps or YAML frontmatter."
        )
    )
    applicability_markdown: str = Field(
        description=(
            "Write multiline markdown explaining who, when, or what contexts trigger "
            "the rule, including explicit exceptions when present."
        )
    )
    authority_markdown: str = Field(
        description=(
            "Write multiline markdown covering authority, provenance, caveats, or "
            "guideline basis for the rule."
        )
    )
    used_evidence_ids: list[str] = Field(
        default_factory=list,
        description=(
            "List candidate evidence ids actually relied on in the drafted page. "
            "Return [] when none were used."
        ),
    )


class ProcedurePageOutput(BaseModel):
    """Drafted content for a procedure wiki page."""

    title: str = Field(
        description=(
            "Use the target procedure title unless the source clearly supports a "
            "better canonical workflow name."
        )
    )
    brief: str = Field(
        description=(
            "Write one concise sentence under 180 characters summarizing the workflow "
            "outcome."
        )
    )
    steps: list[str] = Field(
        default_factory=list,
        description=(
            "Return 3-7 concise imperative step strings in execution order. Do not "
            "embed numbering, bullets, or markdown formatting inside the strings."
        ),
    )
    used_evidence_ids: list[str] = Field(
        default_factory=list,
        description=(
            "List candidate evidence ids actually relied on in the drafted page. "
            "Return [] when none were used."
        ),
    )


class ConflictPageOutput(BaseModel):
    """Drafted content for a conflict wiki page."""

    title: str = Field(
        description=(
            "Use the target conflict title unless the source clearly supports a better "
            "canonical mismatch name."
        )
    )
    brief: str = Field(
        description=(
            "Write one concise sentence under 180 characters summarizing the mismatch."
        )
    )
    description_markdown: str = Field(
        description=(
            "Write the conflict body as multiline markdown naming the conflicting "
            "positions, context, and consequence. Do not include YAML frontmatter or a "
            "top-level # heading."
        )
    )
    impacted_pages: list[str] = Field(
        default_factory=list,
        description=(
            "List explicit wiki links such as [[regulations/foo]] only when they are "
            "supported by context; otherwise return []."
        ),
    )
    used_evidence_ids: list[str] = Field(
        default_factory=list,
        description=(
            "List candidate evidence ids actually relied on in the drafted page. "
            "Return [] when none were used."
        ),
    )


class ConflictCheckResult(BaseModel):
    """Structured decision about whether two compliance pages truly conflict."""

    is_conflict: bool = Field(
        description=(
            "Return true only when the two pages contain a real conflict or opposing "
            "requirement."
        )
    )
    title: str = Field(
        default="",
        description=(
            "If is_conflict is true, write a concise conflict title; otherwise return "
            "an empty string."
        ),
    )
    description: str = Field(
        default="",
        description=(
            "If is_conflict is true, briefly describe the mismatch and why it matters; "
            "otherwise return an empty string."
        ),
    )


@dataclass
class _EvidencePageState:
    page_slug: str
    claim_key: str
    canonical_claim: str
    title: str
    brief: str


__all__ = [
    "CompileArtifacts",
    "ConflictCheckResult",
    "ConflictPageOutput",
    "DraftCreateUpdateFn",
    "EvidenceDocumentManifest",
    "EvidenceDraftOutput",
    "EvidenceDraftQuote",
    "EvidencePlanActions",
    "EvidencePlanItem",
    "EvidenceValidationIssue",
    "PagePlanActions",
    "PagePlanItem",
    "ProcedurePageOutput",
    "RegulationPageOutput",
    "SummaryStageResult",
    "TaxonomyPlanResult",
    "TopicPageOutput",
    "VerifiedEvidenceInstance",
    "_EvidencePageState",
    "_MaterializedDocument",
]
