"""Google Vertex AI implementation of the provider-neutral agent contract."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Mapping
from time import monotonic
from typing import Any

from orchestrator.config.project_config import GeminiVertexProviderSettings
from orchestrator.models.agent import AgentRequest, AgentResult, AgentStartedCallback
from orchestrator.providers.agent_provider import AgentProviderError

logger = logging.getLogger("orchestrator")

GeminiClientFactory = Callable[..., Any]

_AUTHENTICATION_MARKERS = (
    "adc",
    "authentication",
    "credential",
    "default credentials",
    "forbidden",
    "permission denied",
    "permissiondenied",
    "unauthenticated",
)


class GeminiVertexAgentProvider:
    """Run one provider-neutral agent request with Gemini on Vertex AI.

    The SDK client is lazy so a configuration can be inspected without requiring
    Application Default Credentials until an agent execution is requested.
    """

    def __init__(
        self,
        name: str,
        settings: GeminiVertexProviderSettings,
        *,
        client_factory: GeminiClientFactory | None = None,
    ) -> None:
        self._name = name
        self._settings = settings
        self._client_factory = client_factory or _create_vertex_client
        self._client: Any | None = None

    def execute(
        self,
        request: AgentRequest,
        *,
        on_started: AgentStartedCallback | None = None,
    ) -> AgentResult:
        del on_started
        started_at = monotonic()
        structured = request.structured_output is not None
        logger.info(
            "[gemini-vertex] Starting provider=%s model=%s structured_output=%s",
            self._name,
            self._settings.model,
            structured,
        )
        try:
            response = self._client_or_raise().models.generate_content(
                model=self._settings.model,
                contents=request.instructions,
                config=self._generation_config(request),
            )
            output, structured_output = self._result_content(response, request)
        except AgentProviderError:
            raise
        except Exception as exc:
            raise self._request_error(exc) from exc

        elapsed = monotonic() - started_at
        logger.info(
            "[gemini-vertex] Completed provider=%s model=%s in %.1fs",
            self._name,
            self._settings.model,
            elapsed,
        )
        return AgentResult(
            output=output,
            structured_output=structured_output,
            metadata=self._metadata(response),
        )

    def _client_or_raise(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            self._client = self._client_factory(
                vertexai=True,
                project=self._settings.project,
                location=self._settings.location,
                timeout_seconds=self._settings.timeout_seconds,
            )
        except AgentProviderError:
            raise
        except ModuleNotFoundError as exc:
            raise AgentProviderError(
                "Gemini Vertex provider requires the google-genai package. "
                "Install the project's dependencies before running the workflow."
            ) from exc
        except Exception as exc:
            raise self._request_error(exc) from exc
        return self._client

    def _generation_config(self, request: AgentRequest) -> dict[str, Any]:
        config: dict[str, Any] = {"temperature": self._settings.generation.temperature}
        max_output_tokens = self._settings.generation.max_output_tokens
        if max_output_tokens is not None:
            config["max_output_tokens"] = max_output_tokens
        if request.structured_output is not None:
            config["response_mime_type"] = "application/json"
            config["response_json_schema"] = dict(request.structured_output.schema)
        return config

    def _result_content(
        self, response: Any, request: AgentRequest
    ) -> tuple[str, Mapping[str, Any] | None]:
        text = _response_text(response)
        if request.structured_output is None:
            if not text:
                raise AgentProviderError("Gemini Vertex returned an empty text response")
            return text, None

        structured_output = self._structured_output(response, text, request)
        if not text:
            text = json.dumps(structured_output, ensure_ascii=False)
        return text, structured_output

    def _structured_output(
        self,
        response: Any,
        text: str,
        request: AgentRequest,
    ) -> Mapping[str, Any]:
        parsed = getattr(response, "parsed", None)
        value: Any = parsed
        if value is None:
            try:
                value = json.loads(text)
            except json.JSONDecodeError as exc:
                raise AgentProviderError(
                    "Gemini Vertex returned malformed structured output; expected a JSON object"
                ) from exc
        if not isinstance(value, Mapping):
            raise AgentProviderError(
                "Gemini Vertex returned structured output with a non-object JSON value"
            )

        result = dict(value)
        expectation = request.structured_output
        if expectation is None:  # Defensive: this helper is called only for structured requests.
            return result
        _validate_schema(result, expectation.schema)
        return result

    def _metadata(self, response: Any) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "provider": self._name,
            "provider_type": "gemini-vertex",
            "model": self._settings.model,
        }
        usage = getattr(response, "usage_metadata", None)
        token_fields = {
            "input_tokens": "prompt_token_count",
            "output_tokens": "candidates_token_count",
            "total_tokens": "total_token_count",
        }
        for metadata_name, response_name in token_fields.items():
            value = getattr(usage, response_name, None)
            if isinstance(value, int):
                metadata[metadata_name] = value
        return metadata

    @staticmethod
    def _request_error(exc: Exception) -> AgentProviderError:
        details = f"{type(exc).__name__}: {exc}".casefold()
        if any(marker in details for marker in _AUTHENTICATION_MARKERS):
            return AgentProviderError(
                "Gemini Vertex authentication or authorization failed. Configure Google "
                "Application Default Credentials (ADC), for example with "
                "`gcloud auth application-default login`, and verify Vertex AI access."
            )
        return AgentProviderError(
            "Gemini Vertex request failed. Verify the configured project, location, model, "
            "and network connectivity."
        )


def _create_vertex_client(
    *,
    vertexai: bool,
    project: str,
    location: str,
    timeout_seconds: int,
) -> Any:
    """Import the optional SDK only at the infrastructure boundary."""
    from google import genai

    return genai.Client(
        vertexai=vertexai,
        project=project,
        location=location,
        http_options={"timeout": timeout_seconds * 1000},
    )


def _response_text(response: Any) -> str:
    text = getattr(response, "text", None)
    return text if isinstance(text, str) else ""


def _validate_schema(value: Any, schema: Mapping[str, Any], path: str = "$") -> None:
    """Validate the small JSON Schema subset exposed by the shared contract."""
    expected_type = schema.get("type")
    if expected_type == "object":
        if not isinstance(value, Mapping):
            _schema_error(path, "expected an object")
        required = schema.get("required", [])
        if isinstance(required, list):
            missing = [key for key in required if isinstance(key, str) and key not in value]
            if missing:
                raise AgentProviderError(
                    "Gemini Vertex returned structured output missing required fields: "
                    f"{', '.join(missing)}"
                )
        properties = schema.get("properties", {})
        if isinstance(properties, Mapping):
            if schema.get("additionalProperties") is False:
                unexpected = [key for key in value if key not in properties]
                if unexpected:
                    _schema_error(path, f"contains unexpected field {unexpected[0]!r}")
            for name, property_schema in properties.items():
                if name in value and isinstance(name, str) and isinstance(property_schema, Mapping):
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
        "Gemini Vertex returned structured output that does not match the requested schema "
        f"at {path}: {detail}"
    )
