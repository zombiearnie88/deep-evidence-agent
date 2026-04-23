"""High-level retrieval helpers for tree-indexed documents."""

from __future__ import annotations

from pathlib import Path

from pageindex_adapter.client import load_indexed_document


def get_structure(artifact_path: Path) -> list[dict[str, object]]:
    """Return indexed structure nodes from artifact."""
    payload = load_indexed_document(artifact_path)
    structure = payload.get("structure", [])
    if isinstance(structure, list):
        return structure
    return []


def get_page_content(
    artifact_path: Path, start_page: int, end_page: int
) -> list[dict[str, object]]:
    """Return inclusive page-range content from artifact."""
    payload = load_indexed_document(artifact_path)
    pages = payload.get("pages", [])
    if not isinstance(pages, list):
        return []
    lower = max(1, start_page)
    upper = max(lower, end_page)
    result: list[dict[str, object]] = []
    for entry in pages:
        if not isinstance(entry, dict):
            continue
        page = int(entry.get("page", 0))
        if lower <= page <= upper:
            result.append({"page": page, "content": str(entry.get("content", ""))})
    return result
