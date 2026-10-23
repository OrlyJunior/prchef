import json
import re
from typing import Any

from prchef.services.exceptions import StructuredOutputError

_SINGLE_JSON_FENCE = re.compile(
    r"\A\s*```(?:json)?[ \t]*\r?\n(?P<body>.*?)\r?\n?```\s*\Z",
    re.IGNORECASE | re.DOTALL,
)


def parse_json_object(text: str, source: str) -> dict[str, Any]:
    cleaned = text.strip()
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        value = _extract_json_object(cleaned, source)
    if not isinstance(value, dict):
        raise StructuredOutputError(f"{source} response must be a JSON object")
    return value


def _extract_json_object(text: str, source: str) -> dict[str, Any]:
    fenced = _SINGLE_JSON_FENCE.fullmatch(text)
    if fenced:
        return _decode_json_object(fenced.group("body"), source)
    if "```" in text:
        raise StructuredOutputError(
            f"{source} must return a single JSON object, not mixed fenced content"
        )

    decoder = json.JSONDecoder()
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    position = 0
    while (start := text.find("{", position)) >= 0:
        try:
            value, end = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            position = start + 1
            continue
        if isinstance(value, dict):
            candidates.append((start, start + end, value))
        position = start + max(end, 1)

    if len(candidates) != 1:
        raise StructuredOutputError(f"{source} did not return a JSON object")
    start, end, value = candidates[0]
    surrounding_text = f"{text[:start]}{text[end:]}"
    if "{" in surrounding_text or "}" in surrounding_text:
        raise StructuredOutputError(f"{source} did not return a single JSON object")
    return value


def _decode_json_object(text: str, source: str) -> dict[str, Any]:
    try:
        value = json.loads(text.strip())
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
