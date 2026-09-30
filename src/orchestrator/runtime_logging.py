from __future__ import annotations

import logging
import re
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import TYPE_CHECKING, TextIO

if TYPE_CHECKING:
    from orchestrator.models.code_review import CodeReview
    from orchestrator.models.repository_spec import RepositorySpec

_SENSITIVE_VALUE = re.compile(
    r"(?i)(?<![a-z0-9_-])(jules_api_key|orchestrator_jules_api_key|github_token|gh_token|"
    r"x-api-key|[a-z0-9_-]*(?:api[_-]?key|secret|password))"
    r"(\s*+[=:]\s*+|\s++)([^\s,;]++)"
)
_SENSITIVE_HEADER_VALUE = re.compile(
    r"(?i)(authorization|token)(\s*+[=:]\s*+)([^\s,;]++)"
)
_BEARER = re.compile(r"(?i)\bBearer\s++[^\s,;]++")
_BASIC_AUTHORIZATION = re.compile(
    r"(?i)(\bauthorization[\"']?\s*+[=:]\s*+[\"']?)Basic\s++[^\s,;\"']++"
)
# Tokens are opaque: consume the entire allowed character run, including a
# final dot/hyphen. A trailing word boundary would leave those characters behind.
_GITHUB_TOKEN = re.compile(
    r"(?<![A-Za-z0-9_])(?:gh[pousr]_|github_pat_)[A-Za-z0-9._-]++"
)
_PRIVATE_KEY_MARKER = re.compile(
    r"-----(BEGIN|END) (?:[A-Z0-9 ]+ )?PRIVATE KEY-----"
)
_SENSITIVE_FIELD_NAME = re.compile(
    r"(?i)(?:^|[_-])(?:api[_-]?key|token|secret|password|authorization|private[_-]?key)(?:$|[_-])"
)


class UtcTimestampFormatter(logging.Formatter):
    """Format runtime log timestamps as compact ISO 8601 UTC values."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        del datefmt
        timestamp = datetime.fromtimestamp(record.created, UTC)
        return timestamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


class CurrentStderrHandler(logging.StreamHandler[TextIO]):
    """Write to the stderr stream that is active when the record is emitted.

    Test runners and CLI adapters can temporarily replace and then close
    ``sys.stderr``. Holding their former stream would make later configuration
    warnings fail to emit, even though stderr is available again.
    """

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stderr
        super().emit(record)


def _redact_private_keys(value: str) -> str:
    # Visit each marker once, even when many BEGIN markers have no matching END.
    # A repeated lazy search for the end of each block would rescan the same tail.
    parts: list[str] = []
    start: int | None = None
    cursor = 0
    for marker in _PRIVATE_KEY_MARKER.finditer(value):
        if marker.group(1) == "BEGIN":
            if start is None:
                start = marker.start()
        elif start is not None:
            parts.extend((value[cursor:start], "[REDACTED PRIVATE KEY]"))
            cursor = marker.end()
            start = None
    parts.append(value[cursor:])
    return "".join(parts)


def redact_text(value: str) -> str:
    """Remove common credential forms before text reaches a log handler."""
    # Redact authorization schemes first. Otherwise ``Authorization: Basic ...``
    # would have only ``Basic`` consumed by _SENSITIVE_HEADER_VALUE, leaving
    # the credential itself in the log message.
    redacted = _redact_private_keys(value)
    redacted = _BASIC_AUTHORIZATION.sub(r"\1Basic [REDACTED]", redacted)
    redacted = _BEARER.sub("Bearer [REDACTED]", redacted)
    redacted = _SENSITIVE_VALUE.sub(r"\1=[REDACTED]", redacted)
    redacted = _SENSITIVE_HEADER_VALUE.sub(r"\1=[REDACTED]", redacted)
    return _GITHUB_TOKEN.sub("[REDACTED]", redacted)


def redact_payload(value: object) -> object:
    """Return a JSON-safe copy with credentials removed at every nesting level."""
    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        result: dict[str, object] = {}
        for key, item in value.items():
            normalized = str(key)
            if _SENSITIVE_FIELD_NAME.search(normalized):
                result[normalized] = "[REDACTED]"
            else:
                result[normalized] = redact_payload(item)
        return result
    if isinstance(value, (list, tuple, set)):
        return [redact_payload(item) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(str(value))


def summarize_text(value: str | None, *, limit: int = 240) -> str:
    compact = redact_text(" ".join((value or "").split()))
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 3]}..."


def _log_text_items(
    logger: logging.Logger, *, scope: str, label: str, values: Sequence[str]
) -> None:
    """Log a bounded list of already-parsed structured text values."""
    for value in values[:3]:
        logger.debug("[%s] %s: %s", scope, label, summarize_text(value, limit=160))
    remaining = len(values) - 3
    if remaining > 0:
        logger.debug("[%s] additional_%s: %s", scope, label, remaining)


def log_spec_summary(logger: logging.Logger, spec: RepositorySpec) -> None:
    """Emit a compact, provider-neutral summary of a parsed repository spec."""
    logger.debug("[spec] safe_to_implement=%s", str(spec.safe_to_implement).lower())
    logger.debug("[spec] proposal: %s", summarize_text(spec.implementation_proposal))
    logger.debug("[spec] artifacts: %s", len(spec.involved_artifacts))
    logger.debug("[spec] acceptance_criteria: %s", len(spec.acceptance_criteria))
    logger.debug("[spec] expected_tests: %s", len(spec.expected_tests))
    logger.debug("[spec] ambiguities: %s", len(spec.ambiguities))
    if spec.ambiguities:
        _log_text_items(logger, scope="spec", label="ambiguity", values=spec.ambiguities)


def log_review_summary(logger: logging.Logger, review: CodeReview) -> None:
    """Emit a compact, provider-neutral summary of a parsed code review."""
    logger.debug("[review] result=%s", review.result)
    logger.debug("[review] summary: %s", summarize_text(review.summary))
    logger.debug("[review] blocking_findings: %s", len(review.blocking_findings))
    logger.debug("[review] suggestions: %s", len(review.suggestions))
    if review.blocking_findings:
        _log_text_items(
            logger,
            scope="review",
            label="blocking_finding",
            values=review.blocking_findings,
        )
    if review.suggestions:
        _log_text_items(logger, scope="review", label="suggestion", values=review.suggestions)


def format_duration(seconds: float) -> str:
    total_seconds = max(0, int(seconds))
    minutes, remaining = divmod(total_seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {remaining}s"
    return f"{remaining}s"


def format_command(command: Sequence[str]) -> str:
    """Format a command without echoing prompt/body-like argument values."""
    parts: list[str] = []
    redact_next = False
    for part in command:
        if redact_next:
            parts.append("[REDACTED]")
            redact_next = False
            continue
        if part.casefold() in {"--body", "--prompt", "--input", "--token", "--password"}:
            parts.append(part)
            redact_next = True
        elif part.casefold().startswith("body="):
            parts.append("body=[REDACTED]")
        else:
            parts.append(redact_text(part))
    return " ".join(parts)


def configure_logging(*, verbose: bool = False, configured_level: str = "INFO") -> None:
    """Configure concise application logs on stderr, without touching stdout JSON."""
    logger = logging.getLogger("orchestrator")
    handler = next(
        (item for item in logger.handlers if getattr(item, "_orchestrator_handler", False)),
        None,
    )
    if (
        handler is not None
        and isinstance(handler, logging.StreamHandler)
        and (
            handler.stream is not sys.stderr
            or bool(getattr(handler.stream, "closed", False))
        )
    ):
        logger.removeHandler(handler)
        handler.close()
        handler = None
    if handler is None:
        handler = CurrentStderrHandler(sys.stderr)
        handler._orchestrator_handler = True  # type: ignore[attr-defined]
        handler.setFormatter(UtcTimestampFormatter("%(asctime)s %(message)s"))
        logger.addHandler(handler)
    handler.setLevel(logging.NOTSET)
    level = logging.DEBUG if verbose else getattr(
        logging, configured_level.upper(), logging.INFO
    )
    logger.setLevel(level)
    logger.disabled = False
    # Keep records observable by standard logging handlers (including callers
    # that surface configuration warnings) while retaining the CLI stderr
    # handler above.
    logger.propagate = True
    # Alembic's migration details are not workflow progress and obscure the CLI output.
    logging.getLogger("alembic").setLevel(logging.WARNING)
