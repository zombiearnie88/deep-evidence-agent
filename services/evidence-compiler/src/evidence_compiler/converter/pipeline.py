"""Document conversion and normalization pipeline.

The converter prepares source artifacts for ingestion by copying originals into
`raw/`, deduplicating with file hashes, and generating normalized markdown for
supported short-document formats.
"""

from __future__ import annotations

import shutil
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import pymupdf

from evidence_compiler.state import HashRegistry

SUPPORTED_EXTENSIONS = {
    ".pdf",
    ".md",
    ".markdown",
    ".docx",
    ".pptx",
    ".xlsx",
    ".html",
    ".htm",
    ".txt",
    ".csv",
}


@dataclass
class ConvertResult:
    """Result of converting a single input document."""

    raw_path: Path
    source_path: Path | None
    file_hash: str
    is_long_doc: bool
    skipped: bool
    page_count: int | None


def get_pdf_page_count(path: Path) -> int:
    """Return number of pages in a PDF document.

    Args:
        path: Path to PDF file.

    Returns:
        Total page count.
    """
    with pymupdf.open(str(path)) as doc:
        return doc.page_count


def normalize_slug(value: str) -> str:
    """Normalize arbitrary document title into a filesystem-safe slug.

    Args:
        value: Raw document name without extension.

    Returns:
        Lowercase ASCII slug; falls back to `document` when empty.
    """
    normalized = (
        unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    )
    slug = "".join(
        char if char.isalnum() else "-" for char in normalized.lower()
    ).strip("-")
    return slug or "document"


def _unique_path(path: Path) -> Path:
    """Return a collision-free path by appending numeric suffix if needed."""
    if not path.exists():
        return path
    index = 1
    while True:
        candidate = path.with_name(f"{path.stem}-{index}{path.suffix}")
        if not candidate.exists():
            return candidate
        index += 1


def _convert_to_markdown(src: Path) -> str:
    """Convert a document to markdown text.

    Uses direct text loading for text-like files and MarkItDown for rich formats.
    If conversion fails, returns a minimal placeholder markdown.
    """
    suffix = src.suffix.lower()
    if suffix in {".md", ".markdown", ".txt", ".csv"}:
        return src.read_text(encoding="utf-8", errors="ignore")

    try:
        from markitdown import MarkItDown

        result = MarkItDown().convert(str(src))
        return result.text_content
    except Exception:
        return (
            f"# Source: {src.name}\n\n"
            "Automatic rich conversion is currently unavailable for this file type.\n"
            "The original file is preserved in raw/.\n"
        )


def convert_document(
    src: Path, workspace: Path, pageindex_threshold: int, registry: HashRegistry
) -> ConvertResult:
    """Convert and stage one source document for workspace ingestion.

    Workflow:
        1. Compute file hash and skip if already indexed.
        2. Copy file into `raw/` unless already there.
        3. If PDF is above threshold, mark as long doc without markdown source.
        4. Otherwise convert to markdown and write into `wiki/sources/`.

    Args:
        src: Input document file path.
        workspace: Workspace root path.
        pageindex_threshold: Minimum PDF page count for long-doc routing.
        registry: Hash registry used for deduplication checks.

    Returns:
        Conversion result with staged paths and long-doc metadata.
    """
    file_hash = HashRegistry.hash_file(src)
    if registry.is_known(file_hash):
        return ConvertResult(
            raw_path=workspace / "raw" / src.name,
            source_path=None,
            file_hash=file_hash,
            is_long_doc=False,
            skipped=True,
            page_count=None,
        )

    raw_dir = workspace / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    raw_target = raw_dir / src.name
    if src.resolve().parent == raw_dir.resolve():
        raw_path = src.resolve()
    else:
        raw_path = _unique_path(raw_target)
        shutil.copy2(src, raw_path)

    page_count: int | None = None
    if src.suffix.lower() == ".pdf":
        page_count = get_pdf_page_count(src)
        if page_count >= pageindex_threshold:
            return ConvertResult(
                raw_path=raw_path,
                source_path=None,
                file_hash=file_hash,
                is_long_doc=True,
                skipped=False,
                page_count=page_count,
            )

    doc_slug = f"{normalize_slug(src.stem)}-{file_hash[:8]}"
    sources_dir = workspace / "wiki" / "sources"
    sources_dir.mkdir(parents=True, exist_ok=True)
    source_path = sources_dir / f"{doc_slug}.md"
    source_path.write_text(_convert_to_markdown(src), encoding="utf-8")

    return ConvertResult(
        raw_path=raw_path,
        source_path=source_path,
        file_hash=file_hash,
        is_long_doc=False,
        skipped=False,
        page_count=page_count,
    )
