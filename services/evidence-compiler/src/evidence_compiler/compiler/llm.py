"""LiteLLM structured-output helpers for compiler stages."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TypeVar

import litellm
from json_repair import repair_json
from pydantic import BaseModel, ValidationError

from knowledge_models.compiler_api import TokenUsageSummary

ModelT = TypeVar("ModelT", bound=BaseModel)


def _to_int(value: object, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return default
    return default


def _safe_json(value: str) -> dict[str, object]:
    text = value.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        text = text[first_newline + 1 :] if first_newline != -1 else text[3:]
        if text.endswith("```"):
            text = text[:-3]
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        if _looks_like_truncated_json(text):
            raise ValueError(
                "LLM output appears truncated before JSON completed"
            ) from error
        parsed = json.loads(repair_json(text))
    if isinstance(parsed, dict):
        return parsed
    raise ValueError("LLM output is not a JSON object")


def _looks_like_truncated_json(text: str) -> bool:
    """Heuristically detect obviously cut-off JSON before attempting repair."""
    stripped = text.rstrip()
    if not stripped:
        return False
    if stripped[-1] not in {"}", "]", '"'}:
        return True
    return stripped.count("{") > stripped.count("}") or stripped.count("[") > stripped.count(
        "]"
    )


def _preview_text(value: str, *, limit: int = 200) -> str:
    """Return a single-line preview for response diagnostics."""
    preview = value.strip().replace("\n", "\\n")
    if len(preview) <= limit:
        return preview
    return f"{preview[:limit]}..."


class _StructuredResponseTruncatedError(ValueError):
    """Raised when a provider returns incomplete JSON for a structured response."""


def _add_completion_error_note(
    error: BaseException,
    *,
    response_model: type[BaseModel],
    content: str,
    payload: dict[str, object] | None = None,
    finish_reason: str | None = None,
) -> None:
    """Attach compact response diagnostics to parsing and validation failures."""
    payload_keys = ""
    if payload is not None:
        payload_keys = f", payload_keys={sorted(payload.keys())!r}"
    finish_reason_note = f", finish_reason={finish_reason!r}" if finish_reason else ""
    error.add_note(
        f"{response_model.__name__} response length={len(content)}, "
        f"truncated_hint={_looks_like_truncated_json(content)}{finish_reason_note}{payload_keys}, "
        f"preview={_preview_text(content)!r}"
    )


def _response_field(payload: object, key: str) -> object | None:
    if isinstance(payload, dict):
        return payload.get(key)
    return getattr(payload, key, None)


def _extract_first_choice(response: object) -> object:
    choices = _response_field(response, "choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("LiteLLM response has no choices")
    return choices[0]


def _extract_completion_content(response: object) -> str:
    choice = _extract_first_choice(response)
    message = _response_field(choice, "message")
    if message is None:
        raise ValueError("LiteLLM response choice has no message")
    content = _response_field(message, "content")
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False)


def _extract_finish_reason(response: object) -> str | None:
    choice = _extract_first_choice(response)
    for key in ("finish_reason", "stop_reason"):
        value = _response_field(choice, key)
        if value is not None:
            return str(value)
    return None


def _extract_usage(response: object) -> TokenUsageSummary:
    usage = _response_field(response, "usage")
    if usage is None:
        return TokenUsageSummary(calls=1, available=False)

    prompt_tokens_raw = _response_field(usage, "prompt_tokens")
    completion_tokens_raw = _response_field(usage, "completion_tokens")
    total_tokens_raw = _response_field(usage, "total_tokens")
    prompt_tokens = _to_int(prompt_tokens_raw)
    completion_tokens = _to_int(completion_tokens_raw)
    total_tokens = _to_int(total_tokens_raw, prompt_tokens + completion_tokens)
    available = any(
        value is not None
        for value in [prompt_tokens_raw, completion_tokens_raw, total_tokens_raw]
    )
    return TokenUsageSummary(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens,
        calls=1,
        available=available,
    )


def _should_retry_without_structured_output(error: Exception) -> bool:
    """Retry without `response_format` only for provider/schema capability failures."""
    retryable_error_types = tuple(
        error_type
        for error_type in (
            getattr(litellm, "UnsupportedParamsError", None),
            getattr(litellm, "JSONSchemaValidationError", None),
            getattr(litellm, "APIResponseValidationError", None),
        )
        if isinstance(error_type, type)
    )
    if retryable_error_types and isinstance(error, retryable_error_types):
        return True
    message = str(error).lower()
    markers = (
        "response_format",
        "json_schema",
        "json schema",
        "structured output",
        "structured outputs",
        "not supported",
        "unsupported",
    )
    return any(marker in message for marker in markers)


def _is_json_invalid_validation(error: ValidationError) -> bool:
    """Return True when validation failed before JSON fully parsed."""
    return any(item.get("type") == "json_invalid" for item in error.errors())


def _is_truncated_structured_validation(
    error: ValidationError, *, content: str, finish_reason: str | None
) -> bool:
    """Detect incomplete structured JSON that warrants a single retry."""
    if not _is_json_invalid_validation(error):
        return False
    if finish_reason is not None and finish_reason.lower() in {"length", "max_tokens"}:
        return True
    if not _looks_like_truncated_json(content):
        return False
    message = str(error).lower()
    markers = (
        "eof while parsing",
        "unexpected end",
        "unterminated string",
        "json_invalid",
    )
    return any(marker in message for marker in markers)


def _validate_structured_response(
    response: object, response_model: type[ModelT]
) -> ModelT:
    """Validate a structured-output response without silently downgrading schema errors."""
    content = _extract_completion_content(response)
    finish_reason = _extract_finish_reason(response)
    try:
        return response_model.model_validate_json(content)
    except ValidationError as error:
        _add_completion_error_note(
            error,
            response_model=response_model,
            content=content,
            finish_reason=finish_reason,
        )
        if _is_truncated_structured_validation(
            error, content=content, finish_reason=finish_reason
        ):
            suffix = f" (finish_reason={finish_reason})" if finish_reason else ""
            raise _StructuredResponseTruncatedError(
                f"{response_model.__name__} structured response truncated before JSON completed{suffix}"
            ) from error
        raise


def _validate_unstructured_response(
    response: object, response_model: type[ModelT]
) -> ModelT:
    """Validate a plain-JSON fallback response after safe parsing."""
    content = _extract_completion_content(response)
    finish_reason = _extract_finish_reason(response)
    try:
        payload = _safe_json(content)
    except ValueError as error:
        _add_completion_error_note(
            error,
            response_model=response_model,
            content=content,
            finish_reason=finish_reason,
        )
        raise
    try:
        return response_model.model_validate(payload)
    except ValidationError as error:
        _add_completion_error_note(
            error,
            response_model=response_model,
            content=content,
            payload=payload,
            finish_reason=finish_reason,
        )
        raise


def _structured_completion(
    *,
    model: str,
    messages: list[dict[str, str]],
    response_model: type[ModelT],
    max_tokens: int | None = None,
    usage_callback: Callable[[TokenUsageSummary], None] | None = None,
) -> ModelT:
    truncated_attempts = 0
    while True:
        try:
            if max_tokens is None:
                response = litellm.completion(
                    model=model,
                    messages=messages,
                    temperature=0,
                    response_format=response_model,
                )
            else:
                response = litellm.completion(
                    model=model,
                    messages=messages,
                    temperature=0,
                    response_format=response_model,
                    max_tokens=max_tokens,
                )
        except Exception as error:
            if not _should_retry_without_structured_output(error):
                raise
            if max_tokens is None:
                response = litellm.completion(
                    model=model,
                    messages=messages,
                    temperature=0,
                )
            else:
                response = litellm.completion(
                    model=model,
                    messages=messages,
                    temperature=0,
                    max_tokens=max_tokens,
                )
            if usage_callback is not None:
                usage_callback(_extract_usage(response))
            return _validate_unstructured_response(response, response_model)
        if usage_callback is not None:
            usage_callback(_extract_usage(response))
        try:
            return _validate_structured_response(response, response_model)
        except _StructuredResponseTruncatedError:
            if truncated_attempts >= 1:
                raise
            truncated_attempts += 1


async def _structured_acompletion(
    *,
    model: str,
    messages: list[dict[str, str]],
    response_model: type[ModelT],
    max_tokens: int | None = None,
    usage_callback: Callable[[TokenUsageSummary], None] | None = None,
) -> ModelT:
    truncated_attempts = 0
    while True:
        try:
            if max_tokens is None:
                response = await litellm.acompletion(
                    model=model,
                    messages=messages,
                    temperature=0,
                    response_format=response_model,
                )
            else:
                response = await litellm.acompletion(
                    model=model,
                    messages=messages,
                    temperature=0,
                    response_format=response_model,
                    max_tokens=max_tokens,
                )
        except Exception as error:
            if not _should_retry_without_structured_output(error):
                raise
            if max_tokens is None:
                response = await litellm.acompletion(
                    model=model,
                    messages=messages,
                    temperature=0,
                )
            else:
                response = await litellm.acompletion(
                    model=model,
                    messages=messages,
                    temperature=0,
                    max_tokens=max_tokens,
                )
            if usage_callback is not None:
                usage_callback(_extract_usage(response))
            return _validate_unstructured_response(response, response_model)
        if usage_callback is not None:
            usage_callback(_extract_usage(response))
        try:
            return _validate_structured_response(response, response_model)
        except _StructuredResponseTruncatedError:
            if truncated_attempts >= 1:
                raise
            truncated_attempts += 1


__all__ = [
    "_StructuredResponseTruncatedError",
    "_add_completion_error_note",
    "_extract_completion_content",
    "_extract_finish_reason",
    "_extract_first_choice",
    "_extract_usage",
    "_is_json_invalid_validation",
    "_is_truncated_structured_validation",
    "_looks_like_truncated_json",
    "_preview_text",
    "_response_field",
    "_safe_json",
    "_should_retry_without_structured_output",
    "_structured_acompletion",
    "_structured_completion",
    "_validate_structured_response",
    "_validate_unstructured_response",
]
