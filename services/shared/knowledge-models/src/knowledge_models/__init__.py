"""Shared domain schemas for services and compiler."""

from knowledge_models.compiler_api import (
    AddResult,
    CompileResult,
    CredentialStatus,
    DocumentRecord,
    DocumentsResponse,
    JobRecord,
    ProviderOption,
    ProvidersResponse,
    WorkspaceCreatedResponse,
    WorkspaceInitResult,
    WorkspaceListItem,
    WorkspacesResponse,
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
    "WorkspaceCreatedResponse",
    "WorkspaceListItem",
    "WorkspacesResponse",
    "ProvidersResponse",
    "DocumentsResponse",
]
