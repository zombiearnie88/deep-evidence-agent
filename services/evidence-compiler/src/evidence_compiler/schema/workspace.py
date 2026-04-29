"""Workspace layout and schema templates."""

from __future__ import annotations

from pathlib import Path

WORKSPACE_DIRS = [
    "raw",
    "wiki/sources",
    "wiki/summaries",
    "wiki/topics",
    "wiki/regulations",
    "wiki/procedures",
    "wiki/conflicts",
    "wiki/evidence",
    "wiki/reports",
    ".brain/evidence/by-document",
    ".brain/jobs",
]

AGENTS_MD = """# Evidence Brain Wiki Schema

## Directory Structure
- sources/ - normalized source content derived from raw documents
- summaries/ - per-document summary pages
- topics/ - operational topic synthesis pages
- regulations/ - requirement and applicability pages
- procedures/ - execution workflow pages by role/department
- conflicts/ - explicit conflict records between sources and policies
- evidence/ - claim-centric evidence pages rendered from verified manifests
- reports/ - generated quality and lint reports

## Internal Compiler State
- .brain/evidence/by-document/ - authoritative per-document verified evidence manifests

## Special Files
- index.md - catalog of curated wiki pages
- log.md - append-only operation log for ingest and compile jobs

## Authoring Rules
- Use markdown headings and concise sections.
- Prefer explicit source anchors for factual claims.
- Keep one central purpose per page.
"""


def index_md_template() -> str:
    """Return initial template content for `wiki/index.md`."""
    return """# Evidence Brain Wiki Index

## Summaries

## Topics

## Regulations

## Procedures

## Conflicts

## Evidence
"""


def log_md_template() -> str:
    """Return initial template content for `wiki/log.md`."""
    return "# Operations Log\n\n"


def required_markers(workspace: Path) -> list[Path]:
    """Return workspace marker files required for initialization checks.

    Args:
        workspace: Workspace root path.

    Returns:
        Marker file paths that must exist for a valid workspace.
    """
    return [workspace / ".brain" / "config.yaml", workspace / ".brain" / "hashes.json"]
