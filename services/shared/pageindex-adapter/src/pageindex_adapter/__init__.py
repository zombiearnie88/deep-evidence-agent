"""PageIndex adapter package."""

from pageindex_adapter.client import index_pdf, load_indexed_document
from pageindex_adapter.models import IndexedArtifact, PageContent
from pageindex_adapter.retrieval import get_page_content, get_structure

__all__ = [
    "PageContent",
    "IndexedArtifact",
    "index_pdf",
    "load_indexed_document",
    "get_structure",
    "get_page_content",
]
