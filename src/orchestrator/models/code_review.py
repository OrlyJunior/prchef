from dataclasses import dataclass, field

from orchestrator.models.workflow_status import ReviewDecision


@dataclass(slots=True)
class CodeReview:
    """Provider-neutral review feedback with blockers distinct from suggestions.

    ``findings`` is retained for downstream compatibility. Workflow decisions are driven by
    ``blocking_findings``: an approval has none and a change request has at least one.
    """

    result: ReviewDecision
    summary: str = ""
    findings: list[str] = field(default_factory=list)
    blocking_findings: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    raw_text: str = ""
