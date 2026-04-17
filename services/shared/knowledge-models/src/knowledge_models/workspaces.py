"""Workspace schemas."""

from pydantic import BaseModel


class Workspace(BaseModel):
    workspace_id: str
    name: str
    root_path: str
