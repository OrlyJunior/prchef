from dataclasses import dataclass, field

from orchestrator.models.workflow_status import ReviewDecision


@dataclass(slots=True)
class CodeReview:
    result: ReviewDecision
    requirements: list[str] = field(default_factory=list)
    issues: list[str] = field(default_factory=list)
    scope_violations: list[str] = field(default_factory=list)
    test_gaps: list[str] = field(default_factory=list)
    raw_text: str = ""

