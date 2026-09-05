from __future__ import annotations

from pathlib import Path
from typing import Any

from orchestrator.models import (
    AgentRequest,
    AgentResult,
    CodeReview,
    DevelopmentTask,
    PullRequest,
    RepositorySpec,
    ReviewDecision,
    ValidationResult,
)
from orchestrator.services.implementation_service import ImplementationService
from orchestrator.services.review_service import ReviewService
from orchestrator.services.spec_service import SpecService


class RecordingProvider:
    def __init__(self, result: AgentResult) -> None:
        self.result = result
        self.requests: list[AgentRequest] = []

    def execute(self, request: AgentRequest, *, on_started: Any = None) -> AgentResult:
        del on_started
        self.requests.append(request)
        return self.result


class ChangedFilesGit:
    def changed_files(self) -> list[str]:
        return ["src/example.py"]


def _task() -> DevelopmentTask:
    return DevelopmentTask(
        id="5",
        source="github",
        title="Provider contracts",
        description="Decouple roles from providers",
        url="https://github.example/issues/5",
        repository="owner/repo",
    )


def _spec() -> RepositorySpec:
    return RepositorySpec(
        technical_context="services",
        current_behavior="concrete clients",
        implementation_proposal="provider contract",
    )


def _write_project_context(path: Path) -> None:
    (path / "PROJECT.md").write_text(
        "# Durable context\nStable architecture rule.", encoding="utf-8"
    )


def test_planner_depends_only_on_common_agent_provider(tmp_path: Path) -> None:
    _write_project_context(tmp_path)
    provider = RecordingProvider(
        AgentResult(
            output="provider-native structured output",
            structured_output={
                "technical_context": "services",
                "current_behavior": "concrete clients",
                "involved_artifacts": [],
                "implementation_proposal": "provider contract",
                "acceptance_criteria": [],
                "expected_tests": [],
                "risks": [],
                "ambiguities": [],
                "safe_to_implement": True,
            },
        )
    )

    spec = SpecService(provider).generate(
        _task(), tmp_path, "owner/repo", "develop", lambda reference: None
    )

    assert spec.safe_to_implement
    assert provider.requests[0].structured_output is not None
    assert provider.requests[0].structured_output.name == "repository-spec"
    assert "Stable architecture rule." in provider.requests[0].instructions
    assert "keep\nPROJECT.md accurate" in provider.requests[0].instructions


def test_implementer_depends_only_on_common_agent_provider(tmp_path: Path) -> None:
    _write_project_context(tmp_path)
    provider = RecordingProvider(AgentResult(output="implemented"))

    result = ImplementationService(
        provider, ChangedFilesGit()  # type: ignore[arg-type]
    ).implement(_task(), _spec(), tmp_path)

    assert result.success
    assert result.changed_files == ["src/example.py"]
    assert provider.requests[0].workspace_path == tmp_path
    assert "Stable architecture rule." in provider.requests[0].instructions
    assert "Keep PROJECT.md accurate" in provider.requests[0].instructions


def test_review_correction_prompt_includes_structured_review_feedback(tmp_path: Path) -> None:
    _write_project_context(tmp_path)
    provider = RecordingProvider(AgentResult(output="corrected"))

    result = ImplementationService(
        provider, ChangedFilesGit()  # type: ignore[arg-type]
    ).correct_review_findings(
        _task(),
        _spec(),
        CodeReview(
            result=ReviewDecision.CHANGES_REQUESTED,
            summary="Handle the empty input case.",
            findings=["Missing empty input behavior"],
            blocking_findings=["Return a useful error for empty input"],
            suggestions=["Add a focused unit test"],
        ),
        tmp_path,
    )

    assert result.success
    prompt = provider.requests[0].instructions
    assert "REVIEWER CHANGE REQUESTS FOR THE CURRENT IMPLEMENTATION" in prompt
    assert "Handle the empty input case." in prompt
    assert "Return a useful error for empty input" in prompt
    assert "Add a focused unit test" in prompt


def test_reviewer_depends_only_on_common_agent_provider(tmp_path: Path) -> None:
    _write_project_context(tmp_path)
    provider = RecordingProvider(
        AgentResult(
            output="provider-native structured output",
            structured_output={
                "decision": "APPROVED",
                "summary": "Looks good",
                "findings": [],
                "blocking_findings": [],
                "suggestions": [],
            },
        )
    )

    review = ReviewService(provider).review(
        task=_task(),
        spec=_spec(),
        pull_request=PullRequest(
            number=5,
            url="https://github.example/pull/5",
            title="Provider contracts",
            base_branch="develop",
            head_branch="feature/provider-contracts",
        ),
        diff="diff --git a/a b/a",
        validation=ValidationResult(success=True),
        repository_path=tmp_path,
        repository_slug="owner/repo",
        base_branch="develop",
        feature_branch="feature/provider-contracts",
        on_execution_started=lambda reference: None,
    )

    assert review.result == "APPROVED"
    assert provider.requests[0].structured_output is not None
    assert provider.requests[0].structured_output.name == "code-review"
    assert "Stable architecture rule." in provider.requests[0].instructions
