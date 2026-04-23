"""Workspace credential storage backed by OS keychain."""

from __future__ import annotations

import json
import os
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

import keyring
import litellm
from pydantic import BaseModel
from keyring.errors import PasswordDeleteError

from evidence_compiler.models import CredentialStatus
from evidence_compiler.providers import PROVIDER_ENV_VARS, normalize_provider
from evidence_compiler.state import now_iso

KEYCHAIN_SERVICE = "evidence-brain"
KEYCHAIN_ACCOUNT_PREFIX = "workspace"


class _CredentialValidationResult(BaseModel):
    status: str


def _extract_completion_content(response: object) -> str:
    choices = getattr(response, "choices", None)
    if not isinstance(choices, list) or not choices:
        raise ValueError("LiteLLM response has no choices")
    message = getattr(choices[0], "message", None)
    if message is None:
        raise ValueError("LiteLLM response choice has no message")
    content = getattr(message, "content", "")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False)


def _account_name(workspace: Path) -> str:
    return f"{KEYCHAIN_ACCOUNT_PREFIX}:{workspace.resolve().name}:compiler"


def _load_payload(workspace: Path) -> dict[str, object] | None:
    raw = keyring.get_password(KEYCHAIN_SERVICE, _account_name(workspace))
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("Stored workspace credentials are corrupted") from error
    if not isinstance(payload, dict):
        raise ValueError("Stored workspace credentials are invalid")
    return payload


def get_workspace_credential_status(workspace: Path) -> CredentialStatus:
    """Return credential status without exposing raw secret."""
    payload = _load_payload(workspace)
    if payload is None:
        return CredentialStatus(
            workspace=workspace,
            provider=None,
            model=None,
            has_api_key=False,
            validated=False,
            validated_at=None,
        )

    provider = str(payload.get("provider") or "openai")
    model = str(payload.get("model") or "")
    api_key = str(payload.get("api_key") or "")
    return CredentialStatus(
        workspace=workspace,
        provider=provider,
        model=model or None,
        has_api_key=bool(api_key),
        validated=bool(payload.get("validated", False)),
        validated_at=str(payload.get("validated_at") or "") or None,
    )


def save_workspace_credentials(
    workspace: Path,
    provider: str,
    model: str,
    api_key: str,
    *,
    validated: bool = False,
    validated_at: str | None = None,
) -> CredentialStatus:
    """Persist provider/model/api_key in OS keychain for one workspace."""
    provider_id = normalize_provider(provider)
    model_name = model.strip()
    secret = api_key.strip()
    if not model_name:
        raise ValueError("Model cannot be empty")
    if not secret:
        raise ValueError("API key cannot be empty")

    payload = {
        "provider": provider_id,
        "model": model_name,
        "api_key": secret,
        "validated": validated,
        "validated_at": validated_at,
        "updated_at": now_iso(),
    }
    keyring.set_password(
        KEYCHAIN_SERVICE, _account_name(workspace), json.dumps(payload)
    )
    return get_workspace_credential_status(workspace)


def delete_workspace_credentials(workspace: Path) -> None:
    """Delete workspace credentials if present."""
    account = _account_name(workspace)
    try:
        keyring.delete_password(KEYCHAIN_SERVICE, account)
    except PasswordDeleteError:
        return


def resolve_workspace_credentials(workspace: Path) -> tuple[str, str, str]:
    """Return provider/model/api_key tuple for workspace compile jobs."""
    payload = _load_payload(workspace)
    if not payload:
        raise ValueError("Missing workspace credentials")
    provider = normalize_provider(str(payload.get("provider") or "openai"))
    model = str(payload.get("model") or "").strip()
    api_key = str(payload.get("api_key") or "").strip()
    if not model or not api_key:
        raise ValueError("Incomplete workspace credentials")
    return provider, model, api_key


@contextmanager
def provider_env(provider: str, api_key: str) -> Generator[None, None, None]:
    """Temporarily inject provider api key into expected environment variables."""
    env_vars = PROVIDER_ENV_VARS.get(provider, ())
    previous = {name: os.environ.get(name) for name in env_vars}
    for name in env_vars:
        os.environ[name] = api_key
    try:
        yield
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def validate_credentials(provider: str, model: str, api_key: str) -> None:
    """Run a tiny LiteLLM call to validate provider/model/api key."""
    provider_id = normalize_provider(provider)
    with provider_env(provider_id, api_key):
        response = litellm.completion(
            model=model,
            messages=[
                {
                    "role": "user",
                    "content": "Return JSON status OK",
                }
            ],
            max_tokens=3,
            temperature=0,
            response_format=_CredentialValidationResult,
        )
    parsed = _CredentialValidationResult.model_validate_json(
        _extract_completion_content(response)
    )
    if not parsed.status.strip():
        raise ValueError("Credential validation did not return a status")
