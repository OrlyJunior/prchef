from __future__ import annotations

import logging
import re
import sys
from collections.abc import Sequence

_SENSITIVE_VALUE = re.compile(
    r"(?i)(jules_api_key|orchestrator_jules_api_key|github_token|gh_token|authorization|token)"
    r"(\s*[=:]\s*|\s+)([^\s,;]+)"
)
_BEARER = re.compile(r"(?i)\bBearer\s+[^\s,;]+")
_GITHUB_TOKEN = re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9_]+|github_pat_[A-Za-z0-9_]+)\b")


def redact_text(value: str) -> str:
    """Remove common credential forms before text reaches a log handler."""
    redacted = _SENSITIVE_VALUE.sub(r"\1=[REDACTED]", value)
    redacted = _BEARER.sub("Bearer [REDACTED]", redacted)
    return _GITHUB_TOKEN.sub("[REDACTED]", redacted)


def summarize_text(value: str | None, *, limit: int = 240) -> str:
    compact = redact_text(" ".join((value or "").split()))
    if len(compact) <= limit:
        return compact
    return f"{compact[: limit - 3]}..."


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
        handler = logging.StreamHandler(sys.stderr)
        handler._orchestrator_handler = True  # type: ignore[attr-defined]
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
    handler.setLevel(logging.NOTSET)
    level = logging.DEBUG if verbose else getattr(
        logging, configured_level.upper(), logging.INFO
    )
    logger.setLevel(level)
    logger.disabled = False
    logger.propagate = False
    # Alembic's migration details are not workflow progress and obscure the CLI output.
    logging.getLogger("alembic").setLevel(logging.WARNING)
