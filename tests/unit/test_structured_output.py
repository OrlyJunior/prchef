import pytest

from orchestrator.services.exceptions import StructuredOutputError
from orchestrator.services.structured_output import parse_json_object

REAL_SESSION_1835452957112904409_FINAL_MESSAGE = (
    "I have completed the read-only technical specification and outputted the requested "
    "JSON. No files were modified, and the codebase tests continue to pass."
)


def test_accepts_a_single_fenced_json_object() -> None:
    assert parse_json_object('```json\n{"safe_to_implement": true}\n```', "Jules spec") == {
        "safe_to_implement": True
    }


def test_accepts_one_json_object_with_surrounding_prose() -> None:
    assert parse_json_object(
        'Here is the requested output:\n{"safe_to_implement": true}\nThank you.',
        "Jules spec",
    ) == {"safe_to_implement": True}


def test_rejects_multiple_json_objects_in_prose() -> None:
    with pytest.raises(StructuredOutputError, match="did not return a JSON object"):
        parse_json_object('{"first": true}\n{"second": true}', "Jules spec")


def test_rejects_malformed_json_even_when_a_later_object_is_valid() -> None:
    with pytest.raises(StructuredOutputError, match="did not return a single JSON object"):
        parse_json_object('Broken {json}\n{"safe_to_implement": true}', "Jules spec")


def test_rejects_malformed_fenced_json() -> None:
    with pytest.raises(StructuredOutputError, match="Invalid JSON returned by Jules spec"):
        parse_json_object('```json\n{"safe_to_implement": }\n```', "Jules spec")


def test_real_session_1835452957112904409_prose_response_is_not_accepted_as_json() -> None:
    with pytest.raises(StructuredOutputError, match="Jules spec did not return a JSON object"):
        parse_json_object(REAL_SESSION_1835452957112904409_FINAL_MESSAGE, "Jules spec")
