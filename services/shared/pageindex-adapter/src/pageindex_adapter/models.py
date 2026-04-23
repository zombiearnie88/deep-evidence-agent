"""Typed data models returned by PageIndex adapter."""

from pydantic import BaseModel


class PageContent(BaseModel):
    page: int
    content: str


class IndexedArtifact(BaseModel):
    doc_id: str
    doc_name: str
    page_count: int
    artifact_path: str
