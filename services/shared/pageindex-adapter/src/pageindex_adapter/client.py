"""Adapter interfaces for PageIndex client usage."""

from dataclasses import dataclass


@dataclass
class IndexedDocument:
    doc_id: str
    doc_name: str
    doc_description: str
