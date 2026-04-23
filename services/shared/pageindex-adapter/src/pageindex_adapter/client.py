"""Local adapter interfaces for PageIndex-compatible indexing."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from PyPDF2 import PdfReader

from pageindex_adapter.models import IndexedArtifact, PageContent


def _extract_pdf_pages(pdf_path: Path) -> list[PageContent]:
    reader = PdfReader(str(pdf_path))
    pages: list[PageContent] = []
    for index, page in enumerate(reader.pages, start=1):
        pages.append(PageContent(page=index, content=page.extract_text() or ""))
    return pages


def index_pdf(pdf_path: Path, artifact_dir: Path) -> IndexedArtifact:
    """Index PDF content into a stable JSON artifact.

    This adapter preserves a minimal page-level structure for runtime retrieval,
    while keeping the integration surface stable for future PageIndex upgrades.
    """
    artifact_dir.mkdir(parents=True, exist_ok=True)
    pages = _extract_pdf_pages(pdf_path)
    doc_id = str(uuid4())
    payload = {
        "doc_id": doc_id,
        "doc_name": pdf_path.stem,
        "page_count": len(pages),
        "structure": [
            {
                "title": pdf_path.stem,
                "start_page": 1,
                "end_page": len(pages),
                "summary": f"Auto-indexed document with {len(pages)} page(s).",
            }
        ],
        "pages": [page.model_dump() for page in pages],
    }
    artifact_path = artifact_dir / "document.json"
    artifact_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return IndexedArtifact(
        doc_id=doc_id,
        doc_name=pdf_path.stem,
        page_count=len(pages),
        artifact_path=str(artifact_path),
    )


def load_indexed_document(artifact_path: Path) -> dict[str, object]:
    """Load one indexed artifact JSON payload."""
    with artifact_path.open("r", encoding="utf-8") as fh:
        return json.load(fh)
