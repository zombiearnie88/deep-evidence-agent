"""Wiki page schemas."""

from pydantic import BaseModel


class WikiPage(BaseModel):
    page_id: str
    page_type: str
    title: str
    path: str
