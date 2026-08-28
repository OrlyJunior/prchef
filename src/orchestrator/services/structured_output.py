import json
from typing import Any

from orchestrator.services.exceptions import StructuredOutputError


def parse_json_object(text: str, source: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        first_newline = cleaned.find("\n")
        last_fence = cleaned.rfind("```")
        if first_newline >= 0 and last_fence > first_newline:
            cleaned = cleaned[first_newline + 1 : last_fence].strip()
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise StructuredOutputError(f"{source} did not return a JSON object") from None
        try:
            value = json.loads(cleaned[start : end + 1])
        except json.JSONDecodeError as exc:
            raise StructuredOutputError(f"Invalid JSON returned by {source}: {exc}") from exc
    if not isinstance(value, dict):
        raise StructuredOutputError(f"{source} response must be a JSON object")
    return value


def string_list(value: Any, field: str, source: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise StructuredOutputError(f"{source}.{field} must be an array of strings")
    return value

