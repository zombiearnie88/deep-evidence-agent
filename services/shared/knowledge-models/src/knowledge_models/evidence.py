"""Evidence block schemas."""

from pydantic import BaseModel


class EvidenceBlock(BaseModel):
    claim: str
    source_id: str
    anchor: str
