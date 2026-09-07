"""LiteLLM implementation of the provider-neutral agent contract."""

from __future__ import annotations

import importlib
import json
import os
from collections.abc import Callable, Mapping
from typing import Any

from orchestrator.config.project_config import ConfigurationError, LiteLLMProviderSettings
from orchestrator.models.agent import AgentRequest, AgentResult, AgentStartedCallback
from orchestrator.providers.agent_provider import AgentProviderError

LiteLLMLoader = Callable[[], Any]


class LiteLLMAgentProvider:
    """Execute an :class:`AgentRequest` through LiteLLM without leaking its SDK.

    LiteLLM is loaded only for an execution. This preserves the standard
    installation for Codex, Jules, and Vertex-only users.
    """

    def __init__(
        self,
        name: str,
        settings: LiteLLMProviderSettings,
        *,
        litellm_loader: LiteLLMLoader | None = None,
        environ: Mapping[str, str] | None = None,
    ) -> None:
        self._name = name
        self._settings = settings
        self._litellm_loader = litellm_loader or _load_litellm
        self._environ = environ

    def execute(
        self,
        request: AgentRequest,
        *,
        on_started: AgentStartedCallback | None = None,
    ) -> AgentResult:
        del on_started
        kwargs = self._completion_kwargs(request)
        try:
            response = self._litellm_or_raise().completion(**kwargs)
            output = _response_content(response)
            if not output:
                raise AgentProviderError("LiteLLM returned an empty text response")
            structured_output = self._structured_output(output, request)
        except (AgentProviderError, ConfigurationError):
            raise
        except Exception as exc:
            # SDK exceptions can include request diagnostics. Do not preserve
            # them as an exception chain where credential-bearing details could
            # be emitted by generic exception logging.
            raise _normalize_error(exc) from None

        return AgentResult(
            output=output,
            structured_output=structured_output,
            metadata=self._metadata(response),
        )

    def _litellm_or_raise(self) -> Any:
        try:
            return self._litellm_loader()
        except ModuleNotFoundError as exc:
            raise ConfigurationError(
                "LiteLLM provider requires the optional 'litellm' dependency. "
                "Install it with `pip install autonomous-dev-orchestrator[litellm]`."
            ) from exc

    def _completion_kwargs(self, request: AgentRequest) -> dict[str, Any]:
        kwargs: dict[str, Any] = {
            "model": self._settings.model,
            "messages": _messages(request),
        }
        if self._settings.api_base is not None:
            kwargs["api_base"] = self._settings.api_base
        if self._settings.timeout_seconds is not None:
            kwargs["timeout"] = self._settings.timeout_seconds
        if self._settings.temperature is not None:
            kwargs["temperature"] = self._settings.temperature
        if self._settings.max_output_tokens is not None:
            kwargs["max_tokens"] = self._settings.max_output_tokens
        api_key = self._api_key()
        if api_key is not None:
            kwargs["api_key"] = api_key
        kwargs.update(self._settings.generation)
        if request.structured_output is not None:
            kwargs["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": request.structured_output.name,
                    "schema": dict(request.structured_output.schema),
                },
            }
        return kwargs

    def _api_key(self) -> str | None:
        variable = self._settings.api_key_env
        if variable is None:
            return None
        environment = os.environ if self._environ is None else self._environ
        value = environment.get(variable)
        if value:
            return value
        raise ConfigurationError(
            f"LiteLLM provider {self._name!r} requires environment variable {variable!r} "
            "for its API key, but it is not set."
        )

    def _structured_output(
        self, output: str, request: AgentRequest
    ) -> Mapping[str, Any] | None:
        expectation = request.structured_output
        if expectation is None:
            return None
        try:
            value = json.loads(output)
        except json.JSONDecodeError as exc:
            raise AgentProviderError(
                "LiteLLM returned malformed structured output; expected a JSON object",
                category="invalid_structured_output",
            ) from exc
        if not isinstance(value, Mapping):
            raise AgentProviderError(
                "LiteLLM returned structured output with a non-object JSON value",
                category="invalid_structured_output",
            )
        result = dict(value)
        _validate_schema(result, expectation.schema)
        return result

    def _metadata(self, response: Any) -> dict[str, Any]:
        response_model = _member(response, "model")
        metadata: dict[str, Any] = {
            "provider": self._name,
            "provider_type": "litellm",
            "model": response_model if isinstance(response_model, str) else self._settings.model,
        }
        usage = _member(response, "usage")
        for metadata_name, response_name in (
            ("input_tokens", "prompt_tokens"),
            ("output_tokens", "completion_tokens"),
            ("total_tokens", "total_tokens"),
        ):
            value = _member(usage, response_name)
            if isinstance(value, int):
                metadata[metadata_name] = value
        choices = _member(response, "choices")
        if isinstance(choices, list) and choices:
            finish_reason = _member(choices[0], "finish_reason")
            if isinstance(finish_reason, str):
                metadata["finish_reason"] = finish_reason
        return metadata


def _load_litellm() -> Any:
    return importlib.import_module("litellm")


def _messages(request: AgentRequest) -> list[dict[str, str]]:
    context = [f"Request title: {request.title}"]
    if request.repository is not None:
        context.append(f"Repository: {request.repository}")
    if request.revision is not None:
        context.append(f"Revision: {request.revision}")
    if request.workspace_path is not None:
        context.append(f"Workspace path: {request.workspace_path}")
    return [
        {
            "role": "system",
            "content": "You are executing an orchestrator agent request. "
            + "\n".join(context),
        },
        {"role": "user", "content": request.instructions},
    ]


def _response_content(response: Any) -> str:
    choices = _member(response, "choices")
    if not isinstance(choices, list) or not choices:
        return ""
    message = _member(choices[0], "message")
    content = _member(message, "content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [_member(item, "text") for item in content]
        return "".join(part for part in parts if isinstance(part, str))
    return ""


def _member(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _validate_schema(value: Any, schema: Mapping[str, Any], path: str = "$") -> None:
    expected_type = schema.get("type")
    if expected_type == "object":
        if not isinstance(value, Mapping):
            _schema_error(path, "expected an object")
        required = schema.get("required", [])
        if isinstance(required, list):
            missing = [key for key in required if isinstance(key, str) and key not in value]
            if missing:
                _schema_error(path, f"missing required field {missing[0]!r}")
        properties = schema.get("properties", {})
        if isinstance(properties, Mapping):
            if schema.get("additionalProperties") is False:
                unexpected = [key for key in value if key not in properties]
                if unexpected:
                    _schema_error(path, f"contains unexpected field {unexpected[0]!r}")
            for name, property_schema in properties.items():
                if (
                    name in value
                    and isinstance(name, str)
                    and isinstance(property_schema, Mapping)
                ):
                    _validate_schema(value[name], property_schema, f"{path}.{name}")
    elif expected_type == "array":
        if not isinstance(value, list):
            _schema_error(path, "expected an array")
        items = schema.get("items")
        if isinstance(items, Mapping):
            for index, item in enumerate(value):
                _validate_schema(item, items, f"{path}[{index}]")
    elif expected_type == "string" and not isinstance(value, str):
        _schema_error(path, "expected a string")
    elif expected_type == "boolean" and not isinstance(value, bool):
        _schema_error(path, "expected a boolean")
    elif expected_type == "integer" and (
        not isinstance(value, int) or isinstance(value, bool)
    ):
        _schema_error(path, "expected an integer")
    elif expected_type == "number" and (
        not isinstance(value, (int, float)) or isinstance(value, bool)
    ):
        _schema_error(path, "expected a number")
    elif expected_type == "null" and value is not None:
        _schema_error(path, "expected null")

    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        _schema_error(path, "contains a value outside the allowed enum")


def _schema_error(path: str, detail: str) -> None:
    raise AgentProviderError(
        "LiteLLM returned structured output that does not match the requested schema "
        f"at {path}: {detail}",
        category="invalid_structured_output",
    )


def _normalize_error(exc: Exception) -> AgentProviderError:
    details = f"{type(exc).__name__}: {exc}".casefold()
    status_code = _status_code(exc)
    retry_after = _retry_after(exc)
    if "quota" in details or "insufficient_quota" in details or status_code == 402:
        return AgentProviderError(
            "LiteLLM provider quota or usage limit was exhausted.", category="quota"
        )
    if status_code == 429 or "ratelimit" in details or "rate limit" in details:
        return AgentProviderError(
            "LiteLLM provider rate limit was reached.",
            category="rate_limit",
            retry_after_seconds=retry_after,
        )
    if status_code in {401, 403} or "authentication" in details or "unauthorized" in details:
        return AgentProviderError(
            "LiteLLM provider authentication or authorization failed.", category="authentication"
        )
    if "timeout" in details or "timed out" in details:
        return AgentProviderError("LiteLLM request timed out.", category="timeout")
    if "connection" in details or "network" in details or "apierror" in details:
        return AgentProviderError(
            "LiteLLM provider network request failed.",
            category="network",
            retry_after_seconds=retry_after,
        )
    if (
        status_code == 404
        or "notfound" in details
        or "badrequest" in details
        or "model" in details
    ):
        return AgentProviderError(
            "LiteLLM provider rejected the configured model or request.", category="invalid_model"
        )
    return AgentProviderError("LiteLLM provider request failed.", category="unknown")


def _status_code(exc: Exception) -> int | None:
    for value in (
        _member(exc, "status_code"),
        _member(_member(exc, "response"), "status_code"),
    ):
        if isinstance(value, int):
            return value
    return None


def _retry_after(exc: Exception) -> float | None:
    response = _member(exc, "response")
    headers = _member(response, "headers") or _member(exc, "headers")
    if not isinstance(headers, Mapping):
        return None
    value = headers.get("Retry-After") or headers.get("retry-after")
    if not isinstance(value, (str, int, float)):
        return None
    try:
        return float(value)
    except ValueError:
        return None
