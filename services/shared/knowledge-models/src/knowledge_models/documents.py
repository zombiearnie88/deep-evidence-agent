"""Source document schemas."""

from pydantic import BaseModel


class SourceDocument(BaseModel):
    document_id: str
    name: str
    source_type: str
