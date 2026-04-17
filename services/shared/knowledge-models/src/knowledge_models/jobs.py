"""Background job schemas."""

from pydantic import BaseModel


class JobStatus(BaseModel):
    job_id: str
    status: str
