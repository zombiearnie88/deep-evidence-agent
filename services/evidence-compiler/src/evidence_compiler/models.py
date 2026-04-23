"""Compatibility exports for shared knowledge models."""

from knowledge_models.compiler_api import (
    AddResult,
    CompileResult,
    CredentialStatus,
    DocumentRecord,
    JobRecord,
    ProviderOption,
    WorkspaceInitResult,
    WorkspaceStatus,
)

__all__ = [
    "WorkspaceInitResult",
    "DocumentRecord",
    "AddResult",
    "JobRecord",
    "WorkspaceStatus",
    "CompileResult",
    "CredentialStatus",
    "ProviderOption",
]
