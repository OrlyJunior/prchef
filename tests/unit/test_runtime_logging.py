from __future__ import annotations

import base64

import pytest

from orchestrator.runtime_logging import redact_payload, redact_text, summarize_text


def _fake_token(prefix: str = "ghs_", body_length: int = 516) -> str:
    body = "FAKE_NOT_A_SECRET.AaZz09-_"
    return prefix + (body * (body_length // len(body) + 1))[:body_length]


@pytest.mark.parametrize("prefix", ["ghs_", "ghp_", "gho_", "ghu_", "ghr_", "github_pat_"])
@pytest.mark.parametrize("body_length", [1, 36, 516, 4096, 100_000])
def test_github_tokens_are_fully_redacted_without_a_length_limit(
    prefix: str, body_length: int,
) -> None:
    token = _fake_token(prefix, body_length)

    assert redact_text(f"Failed using ({token}); retry") == "Failed using ([REDACTED]); retry"
    assert redact_text(f'{{"message":"{token}"}}') == '{"message":"[REDACTED]"}'


@pytest.mark.parametrize("ending", [".", "-", "_"])
def test_github_token_nonword_endings_are_not_left_visible(ending: str) -> None:
    assert redact_text(_fake_token() + ending) == "[REDACTED]"


def test_generic_payload_fields_and_nested_values_redact_long_tokens() -> None:
    token = _fake_token()

    assert redact_payload({"message": token, "details": [token], "token": token}) == {
        "message": "[REDACTED]", "details": ["[REDACTED]"], "token": "[REDACTED]",
    }


def test_summaries_redact_before_truncating() -> None:
    token = _fake_token()

    assert summarize_text(f"Failed with {token}; retry", limit=40) == (
        "Failed with [REDACTED]; retry"
    )
    assert summarize_text(f"{token} " + "ordinary " * 20, limit=31) == (
        "[REDACTED] ordinary ordinary..."
    )


@pytest.mark.parametrize("scheme", ["Basic", "basic", "BASIC"])
@pytest.mark.parametrize("separator", [": ", "=", " :\t"])
@pytest.mark.parametrize("spacing", [" ", "\t", "\r\n\t"], ids=["space", "tab", "folded"])
def test_basic_authorization_masks_all_encoded_credentials(
    scheme: str, separator: str, spacing: str,
) -> None:
    encoded = base64.b64encode(f"x-access-token:{_fake_token()}".encode()).decode("ascii")

    assert redact_text(f"Authorization{separator}{scheme}{spacing}{encoded}; retry") == (
        "Authorization=[REDACTED] [REDACTED]; retry"
    )


def test_basic_authorization_in_json_text_preserves_quotes_and_masks_credentials() -> None:
    encoded = base64.b64encode(f"x-access-token:{_fake_token()}".encode()).decode("ascii")

    assert redact_text(f'{{"Authorization": "Basic {encoded}"}}') == (
        '{"Authorization": "Basic [REDACTED]"}'
    )


@pytest.mark.parametrize("scheme", ["Basic", "Bearer"])
def test_structured_authorization_fields_mask_the_whole_value(scheme: str) -> None:
    assert redact_payload({"authorization": f"{scheme} {_fake_token()}"}) == {
        "authorization": "[REDACTED]",
    }


def test_bearer_and_assignments_still_mask_long_tokens() -> None:
    token = _fake_token()

    assert redact_text(f"Authorization: Bearer {token}; retry") == (
        "Authorization=[REDACTED] [REDACTED]; retry"
    )
    assert redact_text(f"GH_TOKEN={token}; retry") == "GH_TOKEN=[REDACTED]; retry"
    assert redact_text(f"GITHUB_TOKEN = {token}; retry") == "GITHUB_TOKEN=[REDACTED]; retry"


@pytest.mark.parametrize(
    "text",
    [
        "A normal sentence about GitHub Apps and installation tokens.",
        "Basic authentication is described in docs/auth.md.",
        "/home/user/project/src/orchestrator/runtime_logging.py",
        r"C:\Git\project\src\orchestrator\runtime_logging.py",
        "Commit " + "0123456789abcdef" * 2 + "01234567",
        "The names ghs_, ghp_, and github_pat_ describe token prefixes.",
        "embeddedghs_example and github_pattern are ordinary identifiers",
    ],
)
def test_ordinary_text_remains_readable(text: str) -> None:
    assert redact_text(text) == text
    assert redact_payload({"message": text}) == {"message": text}
    assert summarize_text(text, limit=200) == text


@pytest.mark.parametrize(
    "text",
    [
        "a" * 100_000,
        "a" * 100_000 + "_api_key",
        "API_KEY=" + " " * 100_000,
        "Bearer " + " " * 100_000,
        "-----BEGIN PRIVATE KEY-----\n" * 5000,
    ],
    ids=["identifier", "missing-value", "assignment-spaces", "bearer-spaces", "unclosed-keys"],
)
def test_long_nonmatching_inputs_are_preserved(text: str) -> None:
    # No timing thresholds: these exercise failed matches and repeated markers
    # deterministically, where backtracking/rescanning used to be expensive.
    assert redact_text(text) == text


def test_private_key_blocks_still_redact_multiple_types_and_multiline_contents() -> None:
    text = (
        "before -----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY----- "
        "between -----BEGIN PRIVATE KEY-----\nfake\n-----END PRIVATE KEY----- after"
    )

    assert redact_text(text) == (
        "before [REDACTED PRIVATE KEY] between [REDACTED PRIVATE KEY] after"
    )
