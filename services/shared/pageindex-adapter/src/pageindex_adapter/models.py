"""Typed data models returned by PageIndex adapter."""

from pydantic import BaseModel


class PageContent(BaseModel):
    page: int
    content: str
