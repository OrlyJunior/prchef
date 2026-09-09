"""Presentation helpers for sanitized execution diagnostics."""

from __future__ import annotations

import json
from collections.abc import Sequence

from orchestrator.models.execution_diagnostic import ExecutionDiagnostic
from orchestrator.runtime_logging import redact_payload


def format_diagnostics_for_export(diagnostics: Sequence[ExecutionDiagnostic]) -> str:
    """Return complete, readable, defensively redacted diagnostic text.

    Diagnostics are redacted before they are persisted and delivered to the
    TUI. Redacting once more here makes the clipboard boundary safe if a
    diagnostic was supplied by a legacy repository or test double.
    """

    rendered_diagnostics: list[str] = []
    for diagnostic in diagnostics:
        metadata = [f"Timestamp: {diagnostic.occurred_at.isoformat()}"]
        if diagnostic.stage is not None:
            metadata.append(f"Stage: {diagnostic.stage.value}")
        if diagnostic.role is not None:
            metadata.append(f"Role: {diagnostic.role.value}")
        if diagnostic.id is not None:
            metadata.append(f"Diagnostic ID: {diagnostic.id}")
        payload = json.dumps(
            redact_payload(diagnostic.payload),
            ensure_ascii=False,
            indent=2,
            default=str,
        )
        section = diagnostic.category.value.replace("_", " ")
        rendered_diagnostics.append(
            f"=== {section} ===\n" + "\n".join(metadata) + f"\n\n{payload}"
        )
    return "\n\n".join(rendered_diagnostics)
