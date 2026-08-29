from dataclasses import dataclass, field

from orchestrator.models.workflow_status import ReviewDecision


@dataclass(slots=True)
class CodeReview:
    result: ReviewDecision
    summary: str = ""
    findings: list[str] = field(default_factory=list)
    blocking_findings: list[str] = field(default_factory=list)
    suggestions: list[str] = field(default_factory=list)
    raw_text: str = ""
