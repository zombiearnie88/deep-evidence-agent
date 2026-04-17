"""Conflict tracking schemas."""

from pydantic import BaseModel


class ConflictRecord(BaseModel):
    conflict_id: str
    topic: str
    summary: str
